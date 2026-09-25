"""[Gateway 适配器] CTP 交易网关：绑定、认证登录、报单 / 撤单与回调入队 (S5-01, FR-LIVE-01/02/04, FR-RISK-07).

职责边界（04 §3、§6、ADR-X1/X2）：

1. **绑定**：CTP API 由 ``openctp-ctp`` 提供 (06 S0-02 候选之一)。绑定按需导入；未安装时明确失败，
   不静默回退到模拟件。字段常量取自 ``docs/simnow`` 归档的 6.7.13 官方头文件，不凭记忆拼写标志。
2. **回调只入队**：CTP 回调运行在 C++ 线程，只做字段校验 / 转换 / 标识关联与 ``put_nowait``，
   绝不改状态、绝不写库、绝不阻塞 (ADR-X2)；转换失败入死信并关闭发送门禁。
3. **代次复核在最后一刻**：``submit`` / ``cancel`` 在真正调用 ``ReqOrderInsert`` / ``ReqOrderAction``
   之前复核控制代次，不一致即返回明确 ``NOT_SENT`` 且不调用接口 (ADR-X1、FR-RISK-07)。
4. **不猜未核验能力**：今昨仓映射、市价单等柜台能力未经核验时禁用，不以默认标志发送
   (GAP-S0-05；未核验能力禁用而非猜测)。校验失败返回 ``NOT_SENT`` 而不是抛异常：抛异常会被执行
   服务按"可能已发出"记成 ``SENT_UNKNOWN``，而本地校验失败可以证明没有发送。
5. **结果保守**：接口返回值只有 0 才能证明本地调用被受理，且仍只是 ``SENT_UNKNOWN``；
   非零返回值能证明"未发送"的条件须先按柜台实测核验，未核验前一律保守记为 ``SENT_UNKNOWN``。
"""

from __future__ import annotations

import hashlib
import importlib
import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol, runtime_checkable

from qh_trader.core.constants import Exchange, Offset, OrderType, SendState, Side
from qh_trader.core.event import CanonicalEvent
from qh_trader.core.objects import (
    CapabilityProfile,
    ControlEpoch,
    InstrumentId,
    LocalSendResult,
    OrderIdentity,
    OrderIntent,
    VersionedValue,
    require_text,
)
from qh_trader.core.ports import ExecutionPort, FeedbackNormalizerPort

LOGGER = logging.getLogger(__name__)

# 交易所本地时间固定为 UTC+8（无夏令时）；柜台的日期 / 时间字段按此时区还原为 UTC 事件时刻，
# 不使用机器本地时区 (ADR-X3)。
EXCHANGE_TZ = timezone(timedelta(hours=8))

# 以下标志取 docs/simnow/6.7.13_apidemo/demo/ThostFtdcUserApiDataType.h (THOST_FTDC_*)。
DIRECTION_BUY = "0"
DIRECTION_SELL = "1"
OFFSET_OPEN = "0"
OFFSET_CLOSE = "1"
OFFSET_CLOSE_TODAY = "3"
OFFSET_CLOSE_YESTERDAY = "4"
PRICE_LIMIT = "2"
PRICE_ANY = "1"
TIME_GFD = "3"
VOLUME_ANY = "1"
CONTINGENT_IMMEDIATELY = "1"
HEDGE_SPECULATION = "1"
FORCE_CLOSE_NOT = "0"
ACTION_DELETE = "0"
# 报单状态 (TThostFtdcOrderStatusType)
ORDER_STATUS_ALL_TRADED = "0"
ORDER_STATUS_PART_TRADED_QUEUEING = "1"
ORDER_STATUS_PART_TRADED_NOT_QUEUEING = "2"
ORDER_STATUS_NO_TRADE_QUEUEING = "3"
ORDER_STATUS_NO_TRADE_NOT_QUEUEING = "4"
ORDER_STATUS_CANCELED = "5"
# 报单提交状态 (TThostFtdcOrderSubmitStatusType)
SUBMIT_STATUS_INSERT_SUBMITTED = "0"
SUBMIT_STATUS_CANCEL_SUBMITTED = "1"
SUBMIT_STATUS_MODIFY_SUBMITTED = "2"
SUBMIT_STATUS_ACCEPTED = "3"
SUBMIT_STATUS_INSERT_REJECTED = "4"
SUBMIT_STATUS_CANCEL_REJECTED = "5"
SUBMIT_STATUS_MODIFY_REJECTED = "6"
# 私有流 / 公有流订阅模式 (THOST_TERT_*)
SUBSCRIBE_RESTART = 0
SUBSCRIBE_RESUME = 1
SUBSCRIBE_QUICK = 2

# 本地拒发原因在本层用固定小整数标注，只用于审计与报告，不与柜台错误码混用。
CODE_UNSUPPORTED_CAPABILITY = -2
CODE_NOT_READY = -3
CODE_FENCED = -4
CODE_BROKER_REJECTED = -5
CODE_INVALID_IDENTITY = -6
CODE_PLAN_REJECTED = -7

CTP_EXTRA_HINT = "uv sync --extra ctp (openctp-ctp)"


class CtpBindingUnavailableError(RuntimeError):
    """CTP 绑定未安装或不可导入；不做静默回退。"""


class CtpHandshakeError(RuntimeError):
    """连接 / 认证 / 登录 / 结算确认中的某一步未在期限内完成。"""


class CtpQueryError(RuntimeError):
    """查询请求未能得到完整应答。"""


@dataclass(frozen=True, slots=True)
class CtpSettings:
    """柜台接入参数；口令与 AuthCode 只能来自本地秘密配置，不写入仓库 (FR-LIVE-03)。"""

    front_trade: str
    broker_id: str
    investor_id: str
    user_id: str
    password: str = field(repr=False)
    app_id: str | None = None
    auth_code: str | None = field(default=None, repr=False)
    product_info: str = "qh_trader"
    flow_dir: str = "runs/live/ctp_flow"
    ctp_version: str | None = None
    connect_timeout_s: float = 20.0
    login_timeout_s: float = 20.0
    query_timeout_s: float = 15.0
    local_reject_codes: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        for name in ("front_trade", "broker_id", "investor_id", "user_id", "password"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required to contact a broker")
        if not self.front_trade.startswith("tcp://"):
            raise ValueError("a CTP front address must be a tcp:// URI")
        for name in ("connect_timeout_s", "login_timeout_s", "query_timeout_s"):
            if not isinstance(getattr(self, name), (int, float)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive number of seconds")
        if (self.app_id is None) != (self.auth_code is None):
            raise ValueError("AppID and AuthCode are authenticated together or not at all")
        object.__setattr__(self, "local_reject_codes", frozenset(self.local_reject_codes))

    @property
    def authenticated(self) -> bool:
        return self.app_id is not None


@dataclass(frozen=True, slots=True)
class CtpOffsetMapping:
    """某一交易所的开平标志登记；未核验即禁用对应平仓标志 (GAP-S0-05)."""

    exchange: Exchange
    flags: Mapping[Offset, str]
    verified: bool
    evidence_ref: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.exchange, Exchange):
            raise TypeError("offset mapping requires an exchange")
        for offset, flag in self.flags.items():
            if not isinstance(offset, Offset):
                raise TypeError("offset mapping keys must be Offset members")
            if not flag or len(flag) > 1:
                raise ValueError("a CTP offset flag is a single character")
        if self.verified and not self.evidence_ref:
            raise ValueError("a verified offset mapping needs an evidence reference")


@dataclass(frozen=True, slots=True)
class CtpOrderRef:
    """柜台原会话三元组：CTP 撤单与回报归属都按它唯一识别一笔委托 (FR-ORD-05)."""

    front_id: int
    session_id: int
    order_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.front_id, int) or not isinstance(self.session_id, int):
            raise TypeError("front_id and session_id are integers")
        if not self.order_ref:
            raise ValueError("order_ref is required")
        if len(self.order_ref) > 12:
            raise ValueError("CTP OrderRef is limited to 12 characters by the API type")

    @property
    def triple(self) -> tuple[int, int, str]:
        return (self.front_id, self.session_id, self.order_ref)


class CtpOrderRefBook:
    """本会话 OrderRef 分配与归属；重启时用已持久化的发送结果恢复 (FR-REC-02).

    同时记录本地委托对应的合约：撤单请求必须带合约代码（CTP 演示与 SimNow 实测都如此），
    而 ``OrderIdentity`` 本身不含合约，所以归属信息必须由这里提供。
    """

    def __init__(
        self,
        *,
        restored: Iterable[tuple[int, int, str, str]] = (),
        instruments: Mapping[str, InstrumentId] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._owned: dict[tuple[int, int, str], str] = {}
        self._instruments: dict[str, InstrumentId] = dict(instruments or {})
        self._session: tuple[int, int] | None = None
        self._next_ref = 1
        self.restored_count = 0
        for front_id, session_id, order_ref, client_order_id in restored:
            self._owned[(front_id, session_id, order_ref)] = client_order_id
            self.restored_count += 1

    @property
    def session(self) -> tuple[int, int] | None:
        return self._session

    @property
    def restored(self) -> int:
        return self.restored_count

    @property
    def allocated(self) -> int:
        with self._lock:
            return len({key for key in self._owned if key[:2] == self._session})

    def open_session(self, front_id: int, session_id: int, max_order_ref: str | None = None) -> None:
        """登录后用柜台返回的 ``MaxOrderRef`` 作为起点，避免复用同用户已用过的委托号."""
        start = 1
        if max_order_ref:
            prefix = max_order_ref.strip().lstrip("0")
            if prefix.isdigit():
                start = int(prefix) + 1
        with self._lock:
            self._session = (front_id, session_id)
            self._next_ref = max(start, self._next_ref)

    def allocate(self, client_order_id: str, instrument: InstrumentId | None = None) -> CtpOrderRef:
        if not client_order_id:
            raise ValueError("client_order_id is required to allocate an order reference")
        with self._lock:
            if self._session is None:
                raise CtpHandshakeError("no logged-in CTP session owns this order reference")
            front_id, session_id = self._session
            ref = str(self._next_ref)
            self._next_ref += 1
            if len(ref) > 12:
                raise CtpHandshakeError("order reference exhausted for this CTP session")
            self._owned[(front_id, session_id, ref)] = client_order_id
            if instrument is not None:
                self._instruments[client_order_id] = instrument
        return CtpOrderRef(front_id, session_id, ref)

    def instrument_for(self, client_order_id: str | None) -> InstrumentId | None:
        if not client_order_id:
            return None
        with self._lock:
            return self._instruments.get(client_order_id)

    def remember_instrument(self, client_order_id: str, instrument: InstrumentId) -> None:
        with self._lock:
            self._instruments[client_order_id] = instrument

    def resolve(self, front_id: int, session_id: int, order_ref: str) -> str | None:
        with self._lock:
            return self._owned.get((front_id, session_id, order_ref))

    def snapshot(self) -> dict[tuple[int, int, str], str]:
        with self._lock:
            return dict(self._owned)


def order_ref_evidence(ref: CtpOrderRef) -> str:
    """把分配到的原会话三元组写进本地发送结果证据，供重启后重建归属 (FR-REC-02)."""
    return f"ctp-order-ref front={ref.front_id} session={ref.session_id} order_ref={ref.order_ref}"


def parse_order_ref_evidence(evidence: str) -> tuple[int, int, str] | None:
    """解析 :func:`order_ref_evidence` 写出的三元组；格式不符返回 None，不做猜测."""
    if not isinstance(evidence, str) or "ctp-order-ref" not in evidence:
        return None
    marker = next((part for part in evidence.split(";") if "ctp-order-ref" in part), "")
    parts = dict(item.split("=", 1) for item in marker.split() if "=" in item)
    try:
        return (int(parts["front"]), int(parts["session"]), parts["order_ref"])
    except (KeyError, ValueError):
        return None


def restore_order_refs(
    facts: Iterable[Mapping[str, object]], client_order_ids: Mapping[str, str]
) -> list[tuple[int, int, str, str]]:
    """从已持久化的账户事实重建 ``(front, session, order_ref) -> client_order_id``.

    现场只承认"本地已分配并落盘"的三元组：证据解析失败的条目直接跳过，不生成任何推测归属。
    """
    restored: list[tuple[int, int, str, str]] = []
    for fact in facts:
        if not isinstance(fact, Mapping) or fact.get("kind") not in ("send_result", "cancel_result"):
            continue
        result = fact.get("result")
        evidence = getattr(result, "evidence", None)
        if not isinstance(evidence, str):
            continue
        parsed = parse_order_ref_evidence(evidence)
        if parsed is None:
            continue
        command_id = fact.get("command_id")
        client_order_id = fact.get("client_order_id")
        if client_order_id is None and isinstance(command_id, str):
            client_order_id = client_order_ids.get(command_id)
        if not isinstance(client_order_id, str) or not client_order_id:
            continue
        restored.append((parsed[0], parsed[1], parsed[2], client_order_id))
    return restored


def restore_local_instruments(facts: Iterable[Mapping[str, object]]) -> dict[str, InstrumentId]:
    """从已持久化的委托事实恢复 ``client_order_id -> 合约``，供重启后的撤单使用."""
    restored: dict[str, InstrumentId] = {}
    for fact in facts:
        if not isinstance(fact, Mapping) or fact.get("kind") != "intent":
            continue
        intent = fact.get("intent")
        instrument = getattr(intent, "instrument", None)
        client_order_id = getattr(intent, "client_order_id", None)
        if isinstance(client_order_id, str) and isinstance(instrument, InstrumentId):
            restored[client_order_id] = instrument
    return restored


@runtime_checkable
class CtpEventSink(Protocol):
    """回调汇入出口：执行服务的 ``enqueue`` / ``enqueue_callback_error`` 即满足."""

    def enqueue(self, event: CanonicalEvent) -> bool: ...
    def enqueue_callback_error(self, source_id: str, error: Exception) -> None: ...


@runtime_checkable
class CtpQuerySink(Protocol):
    """查询应答出口；由查询适配器实现，按 request_id 与完成标志收口."""

    def on_query_response(
        self,
        *,
        kind: str,
        request_id: int,
        records: tuple[Mapping[str, object], ...],
        is_last: bool,
        error_code: int | None,
        error_message: str | None,
    ) -> None: ...
    def on_query_error(self, *, request_id: int, error_code: int, error_message: str) -> None: ...


@runtime_checkable
class CtpBinding(Protocol):
    """CTP API 绑定；测试注入等价假件，生产用 ``openctp-ctp``."""

    name: str
    version: str

    def create_trader_api(self, flow_dir: str) -> Any: ...
    def trader_spi_base(self) -> Any: ...
    def field(self, type_name: str) -> Any: ...
    def api_version(self, api: Any) -> str: ...
    def dll_hashes(self) -> Mapping[str, str]: ...


class OpenCtpBinding:
    """``openctp-ctp`` 绑定；模块导入推迟到创建连接时，未安装不阻塞其他层次."""

    name = "openctp-ctp"
    version = "unavailable"

    def __init__(self) -> None:
        self._module: ModuleType | None = None

    def _trader(self) -> ModuleType:
        """openctp-ctp 把交易接口模块以 ``tdapi`` 暴露 (对应 ``thosttraderapi``)."""
        if self._module is None:
            try:
                package = importlib.import_module("openctp_ctp")
                self._module = package.tdapi
            except (ImportError, OSError, AttributeError) as exc:
                raise CtpBindingUnavailableError(
                    f"CTP binding openctp-ctp is unavailable ({type(exc).__name__}); install it with {CTP_EXTRA_HINT}"
                ) from exc
            self.version = str(getattr(package, "__version__", "unknown"))
        return self._module

    def create_trader_api(self, flow_dir: str) -> Any:
        return self._trader().CThostFtdcTraderApi.CreateFtdcTraderApi(flow_dir)

    def trader_spi_base(self) -> Any:
        return self._trader().CThostFtdcTraderSpi

    def field(self, type_name: str) -> Any:
        return getattr(self._trader(), type_name)()

    def api_version(self, api: Any) -> str:
        getter = getattr(api, "GetApiVersion", None)
        return str(getter()) if getter is not None else "unknown"

    def dll_hashes(self) -> Mapping[str, str]:
        """登记实际加载的原生库哈希 (S0-02 / GAP-S0-01 关闭条件)."""
        module = importlib.import_module("openctp_ctp")
        if not module.__file__:
            return {}
        root = Path(module.__file__).resolve().parent
        digests: dict[str, str] = {}
        for path in sorted(list(root.glob("*.pyd")) + list(root.parent.glob("openctp_ctp.libs/*.dll"))):
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            digests[path.name] = digest.hexdigest()
        return digests


def load_ctp_binding() -> CtpBinding:
    return OpenCtpBinding()


# --------------------------------------------------------------------------------------- 序列化辅助


def _read_fields(source: Any, names: Iterable[str]) -> dict[str, object]:
    """按声明字段读取；缺失或解码失败的字段记入 ``__field_errors``，不中断其余字段."""
    values: dict[str, object] = {}
    errors: list[str] = []
    for name in names:
        try:
            value = getattr(source, name)
        except Exception as exc:  # 绑定层解码异常必须被记录，不能吞掉整条回报
            errors.append(f"{name}:{type(exc).__name__}")
            continue
        if value is None or value == "":
            continue
        values[name] = value
    if errors:
        values["__field_errors"] = tuple(errors)
    return values


TRADER_ORDER_FIELDS = (
    "BrokerID",
    "InvestorID",
    "UserID",
    "InstrumentID",
    "ExchangeID",
    "OrderRef",
    "OrderSysID",
    "OrderLocalID",
    "FrontID",
    "SessionID",
    "Direction",
    "CombOffsetFlag",
    "OrderStatus",
    "OrderSubmitStatus",
    "VolumeTotalOriginal",
    "VolumeTraded",
    "LimitPrice",
    "InsertDate",
    "InsertTime",
    "UpdateTime",
    "CancelTime",
    "TradingDay",
    "StatusMsg",
)
TRADER_TRADE_FIELDS = (
    "BrokerID",
    "InvestorID",
    "UserID",
    "InstrumentID",
    "ExchangeID",
    "OrderRef",
    "OrderSysID",
    "TradeID",
    "Direction",
    "OffsetFlag",
    "HedgeFlag",
    "TradeType",
    "Price",
    "Volume",
    "TradeDate",
    "TradeTime",
    "TradingDay",
)
TRADER_INPUT_ORDER_FIELDS = (
    "BrokerID",
    "InvestorID",
    "UserID",
    "InstrumentID",
    "ExchangeID",
    "OrderRef",
    "RequestID",
    "Direction",
    "CombOffsetFlag",
    "LimitPrice",
    "VolumeTotalOriginal",
)
TRADER_INPUT_ACTION_FIELDS = (
    "BrokerID",
    "InvestorID",
    "UserID",
    "InstrumentID",
    "ExchangeID",
    "OrderRef",
    "OrderSysID",
    "FrontID",
    "SessionID",
    "ActionFlag",
    "RequestID",
)
TRADER_RSP_INFO_FIELDS = ("ErrorID", "ErrorMsg")
TRADER_LOGIN_FIELDS = (
    "BrokerID",
    "UserID",
    "FrontID",
    "SessionID",
    "MaxOrderRef",
    "TradingDay",
    "LoginTime",
    "SystemName",
    "SysVersion",
)
TRADER_AUTH_FIELDS = ("BrokerID", "UserID", "AppID", "AppType", "UserProductInfo")
TRADER_SETTLEMENT_CONFIRM_FIELDS = ("BrokerID", "InvestorID", "ConfirmDate", "ConfirmTime", "SettlementID")
TRADER_ACCOUNT_FIELDS = (
    "AccountID",
    "BrokerID",
    "TradingDay",
    "PreBalance",
    "Balance",
    "Available",
    "CurrMargin",
    "FrozenMargin",
    "FrozenCash",
    "FrozenCommission",
    "CloseProfit",
    "PositionProfit",
    "Commission",
    "Deposit",
    "Withdraw",
    "PreCredit",
    "Credit",
    "PreMortgage",
    "Mortgage",
    "PreSettlementID",
    "SettlementID",
)
TRADER_POSITION_FIELDS = (
    "BrokerID",
    "InvestorID",
    "InstrumentID",
    "ExchangeID",
    "PosiDirection",
    "HedgeFlag",
    "PositionDate",
    "Position",
    "YdPosition",
    "TodayPosition",
    "LongFrozen",
    "ShortFrozen",
    "OpenVolume",
    "CloseVolume",
    "PositionCost",
    "PreSettlementPrice",
    "SettlementPrice",
    "UseMargin",
    "PositionProfit",
    "TradingDay",
)
TRADER_DEPTH_FIELDS = (
    "InstrumentID",
    "ExchangeID",
    "TradingDay",
    "ActionDay",
    "LastPrice",
    "PreSettlementPrice",
    "SettlementPrice",
    "UpperLimitPrice",
    "LowerLimitPrice",
    "BidPrice1",
    "BidVolume1",
    "AskPrice1",
    "AskVolume1",
    "Volume",
    "Turnover",
    "OpenInterest",
    "UpdateTime",
    "UpdateMillisec",
)
TRADER_INSTRUMENT_FIELDS = (
    "InstrumentID",
    "ExchangeID",
    "InstrumentName",
    "ProductID",
    "VolumeMultiple",
    "PriceTick",
    "OpenDate",
    "ExpireDate",
    "IsTrading",
    "InstLifePhase",
    "ProductClass",
    "UnderlyingInstrID",
    "DeliveryYear",
    "DeliveryMonth",
)


def _rsp_info(payload: Mapping[str, object]) -> tuple[int | None, str | None]:
    code = payload.get("ErrorID")
    message = payload.get("ErrorMsg")
    return (_as_optional_int(code), None if message is None else str(message))


def _as_optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


def _parse_day(value: object) -> date | None:
    """解析 CTP 的 YYYYMMDD 交易日字段；缺字段不做任何推算 (交易日不能由本地日期反推)."""
    if not isinstance(value, str) or len(value.strip()) != 8 or not value.strip().isdigit():
        return None
    text = value.strip()
    try:
        return date(int(text[0:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def _struct_fields(source: object, names: Iterable[str]) -> dict[str, object]:
    return _read_fields(source, names)


class CtpCallbackRouter:
    """回调汇入路由：字段转换 → 归一化 → 入队；查询应答转交查询适配器 (ADR-X2).

    转换失败不丢事件：订单 / 成交回报的转换异常交给执行服务的死信处理，并把 ``faulted`` 置位，
    网关据此关闭发送门禁；查询应答的异常只记在查询簿上，不影响交易汇入。
    """

    def __init__(
        self,
        *,
        normalizer: FeedbackNormalizerPort,
        events: CtpEventSink,
        account_id: str,
        queries: CtpQuerySink | None = None,
        source_id: str = "ctp",
        wall_time: Callable[[], datetime] | None = None,
    ) -> None:
        self.normalizer = normalizer
        self.events = events
        self.queries = queries
        self.account_id = account_id
        self.source_id = source_id
        self._wall_time = wall_time or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self.counts = {
            "order_reports": 0,
            "trade_reports": 0,
            "errors": 0,
            "conversion_failures": 0,
            "query_records": 0,
            "unmatched_callbacks": 0,
        }
        self.faulted: str | None = None
        self.observer: Callable[[str, Mapping[str, object]], None] | None = None

    # ------------------------------------------------------------------ 计数
    def _count(self, key: str) -> None:
        with self._lock:
            self.counts[key] += 1

    def _fail(self, reason: str, error: Exception) -> None:
        with self._lock:
            self.counts["conversion_failures"] += 1
            self.faulted = reason
        self.events.enqueue_callback_error(self.source_id, error)

    def _notify(self, action: str, payload: Mapping[str, object]) -> None:
        if self.observer is not None:
            self.observer(action, payload)

    # ------------------------------------------------------------------ 回报
    def on_order(self, order: Any) -> None:
        received_at = self._wall_time()
        try:
            event = self.normalizer.normalize_order(_struct_fields(order, TRADER_ORDER_FIELDS), received_at)
        except Exception as exc:
            self._fail("order_report_conversion", exc)
            return
        if event is None:
            self._count("unmatched_callbacks")
            return
        self._count("order_reports")
        self.events.enqueue(event)

    def on_trade(self, trade: Any) -> None:
        received_at = self._wall_time()
        try:
            event = self.normalizer.normalize_trade(_struct_fields(trade, TRADER_TRADE_FIELDS), received_at)
        except Exception as exc:
            self._fail("trade_report_conversion", exc)
            return
        if event is None:
            self._count("unmatched_callbacks")
            return
        self._count("trade_reports")
        self.events.enqueue(event)

    def on_error(self, kind: str, payload: Any, info: Any | None) -> None:
        received_at = self._wall_time()
        raw = _struct_fields(
            payload, (TRADER_INPUT_ORDER_FIELDS if kind == "order_insert" else TRADER_INPUT_ACTION_FIELDS)
        )
        if info is not None:
            raw["rsp"] = _struct_fields(info, TRADER_RSP_INFO_FIELDS)
        raw["callback"] = kind
        try:
            event = self.normalizer.normalize_error(raw, received_at)
        except Exception as exc:
            self._fail(f"error_conversion:{kind}", exc)
            return
        self._count("errors")
        if event is not None:
            self.events.enqueue(event)

    # ------------------------------------------------------------------ 查询应答
    def on_query(
        self,
        *,
        kind: str,
        request_id: int,
        record: Any | None,
        info: Any | None,
        is_last: bool,
    ) -> None:
        if self.queries is None:
            self._count("unmatched_callbacks")
            return
        info_payload = {} if info is None else _struct_fields(info, TRADER_RSP_INFO_FIELDS)
        error_code, error_message = _rsp_info(info_payload)
        records: tuple[Mapping[str, object], ...] = ()
        if record is not None:
            records = (_struct_fields(record, QUERY_FIELDS[kind]),)
            self._count("query_records")
        if error_code:
            self.queries.on_query_error(request_id=request_id, error_code=error_code, error_message=error_message or "")
        self.queries.on_query_response(
            kind=kind,
            request_id=request_id,
            records=records,
            is_last=bool(is_last),
            error_code=error_code,
            error_message=error_message,
        )


QUERY_FIELDS: dict[str, tuple[str, ...]] = {
    "account": TRADER_ACCOUNT_FIELDS,
    "position": TRADER_POSITION_FIELDS,
    "order": TRADER_ORDER_FIELDS,
    "trade": TRADER_TRADE_FIELDS,
    "instrument": TRADER_INSTRUMENT_FIELDS,
    "depth": TRADER_DEPTH_FIELDS,
    "settlement_confirm": TRADER_SETTLEMENT_CONFIRM_FIELDS,
}


def build_trader_spi(binding: CtpBinding, router: CtpCallbackRouter) -> object:
    """按绑定基类生成并实例化 SPI；回调函数写进类字典，保证覆盖基类默认实现.

    函数直接放在类命名空间里，避免多继承下基类默认实现优先于本适配器。
    """

    def on_front_connected(self: object) -> None:
        router._notify("front_connected", {})

    def on_front_disconnected(self: object, n_reason: int) -> None:
        router._notify("front_disconnected", {"reason_code": int(n_reason)})

    def on_heartbeat_warning(self: object, n_time_lapse: int) -> None:
        router._notify("heartbeat_warning", {"time_lapse_s": int(n_time_lapse)})

    def on_rsp_authenticate(self: object, field: object, info: object, request_id: int, is_last: bool) -> None:
        router._notify("rsp_authenticate", {**_struct_fields(info, TRADER_RSP_INFO_FIELDS), "request_id": request_id})

    def on_rsp_user_login(self: object, field: object, info: object, request_id: int, is_last: bool) -> None:
        payload = _struct_fields(field, TRADER_LOGIN_FIELDS)
        payload.update(_struct_fields(info, TRADER_RSP_INFO_FIELDS))
        payload["request_id"] = request_id
        router._notify("rsp_user_login", payload)

    def on_rsp_user_logout(self: object, field: object, info: object, request_id: int, is_last: bool) -> None:
        router._notify("rsp_user_logout", {**_struct_fields(info, TRADER_RSP_INFO_FIELDS)})

    def on_rsp_settlement_info_confirm(
        self: object, field: object, info: object, request_id: int, is_last: bool
    ) -> None:
        payload = _struct_fields(field, TRADER_SETTLEMENT_CONFIRM_FIELDS)
        payload.update(_struct_fields(info, TRADER_RSP_INFO_FIELDS))
        router._notify("rsp_settlement_confirm", payload)

    def on_rsp_qry_settlement_info_confirm(
        self: object, field: object, info: object, request_id: int, is_last: bool
    ) -> None:
        router.on_query(kind="settlement_confirm", request_id=request_id, record=field, info=info, is_last=is_last)

    def on_rtn_order(self: object, order: object) -> None:
        router.on_order(order)

    def on_rtn_trade(self: object, trade: object) -> None:
        router.on_trade(trade)

    def on_rsp_order_insert(self: object, field: object, info: object, request_id: int, is_last: bool) -> None:
        router.on_error("order_insert", field, info)

    def on_err_rtn_order_insert(self: object, field: object, info: object) -> None:
        router.on_error("order_insert", field, info)

    def on_rsp_order_action(self: object, field: object, info: object, request_id: int, is_last: bool) -> None:
        router.on_error("order_action", field, info)

    def on_err_rtn_order_action(self: object, field: object, info: object) -> None:
        router.on_error("order_action", field, info)

    def on_rsp_error(self: object, info: object, request_id: int, is_last: bool) -> None:
        payload = _struct_fields(info, TRADER_RSP_INFO_FIELDS)
        if router.queries is not None:
            code, message = _rsp_info(payload)
            if code:
                router.queries.on_query_error(request_id=request_id, error_code=code, error_message=message or "")
        router._notify("rsp_error", {**payload, "request_id": request_id})

    def on_rsp_qry_trading_account(self: object, field, info, request_id: int, is_last: bool) -> None:
        router.on_query(kind="account", request_id=request_id, record=field, info=info, is_last=is_last)

    def on_rsp_qry_investor_position(self: object, field, info, request_id: int, is_last: bool) -> None:
        router.on_query(kind="position", request_id=request_id, record=field, info=info, is_last=is_last)

    def on_rsp_qry_order(self: object, field, info, request_id: int, is_last: bool) -> None:
        router.on_query(kind="order", request_id=request_id, record=field, info=info, is_last=is_last)

    def on_rsp_qry_trade(self: object, field, info, request_id: int, is_last: bool) -> None:
        router.on_query(kind="trade", request_id=request_id, record=field, info=info, is_last=is_last)

    def on_rsp_qry_instrument(self: object, field, info, request_id: int, is_last: bool) -> None:
        router.on_query(kind="instrument", request_id=request_id, record=field, info=info, is_last=is_last)

    def on_rsp_qry_depth_market_data(self: object, field, info, request_id: int, is_last: bool) -> None:
        router.on_query(kind="depth", request_id=request_id, record=field, info=info, is_last=is_last)

    namespace = {
        "OnFrontConnected": on_front_connected,
        "OnFrontDisconnected": on_front_disconnected,
        "OnHeartBeatWarning": on_heartbeat_warning,
        "OnRspAuthenticate": on_rsp_authenticate,
        "OnRspUserLogin": on_rsp_user_login,
        "OnRspUserLogout": on_rsp_user_logout,
        "OnRspSettlementInfoConfirm": on_rsp_settlement_info_confirm,
        "OnRspQrySettlementInfoConfirm": on_rsp_qry_settlement_info_confirm,
        "OnRtnOrder": on_rtn_order,
        "OnRtnTrade": on_rtn_trade,
        "OnRspOrderInsert": on_rsp_order_insert,
        "OnErrRtnOrderInsert": on_err_rtn_order_insert,
        "OnRspOrderAction": on_rsp_order_action,
        "OnErrRtnOrderAction": on_err_rtn_order_action,
        "OnRspError": on_rsp_error,
        "OnRspQryTradingAccount": on_rsp_qry_trading_account,
        "OnRspQryInvestorPosition": on_rsp_qry_investor_position,
        "OnRspQryOrder": on_rsp_qry_order,
        "OnRspQryTrade": on_rsp_qry_trade,
        "OnRspQryInstrument": on_rsp_qry_instrument,
        "OnRspQryDepthMarketData": on_rsp_qry_depth_market_data,
    }
    return type("CtpTraderSpi", (binding.trader_spi_base(),), namespace)()


class _LocalRejectionError(RuntimeError):
    """本地可证明"没有发送"的拒发原因；用固定代码与证据返回 ``NOT_SENT`` 而不是抛异常."""

    def __init__(self, code: int, evidence: str) -> None:
        super().__init__(evidence)
        self.code = code
        self.evidence = evidence


@dataclass(frozen=True, slots=True)
class CtpSessionReport:
    """一次完成握手的证据；只含脱敏字段，不带口令与 AuthCode."""

    front_trade: str
    broker_id: str
    user_id: str
    binding: str
    binding_version: str
    api_version: str
    ctp_version: str | None
    terminal_authentication: bool
    front_id: int
    session_id: int
    trading_day: date | None
    max_order_ref: str | None
    connect_seconds: float
    login_seconds: float
    restored_order_refs: int
    dll_hashes: Mapping[str, str]
    notes: tuple[str, ...] = ()

    def as_mapping(self) -> dict[str, object]:
        return {
            "front_trade": self.front_trade,
            "broker_id": self.broker_id,
            "user_id": self.user_id,
            "binding": self.binding,
            "binding_version": self.binding_version,
            "api_version": self.api_version,
            "ctp_version": self.ctp_version,
            "terminal_authentication": self.terminal_authentication,
            "front_id": self.front_id,
            "session_id": self.session_id,
            "trading_day": None if self.trading_day is None else self.trading_day.isoformat(),
            "max_order_ref": self.max_order_ref,
            "connect_seconds": round(self.connect_seconds, 6),
            "login_seconds": round(self.login_seconds, 6),
            "restored_order_refs": self.restored_order_refs,
            "dll_hashes": dict(self.dll_hashes),
            "notes": list(self.notes),
        }


class CtpTraderGateway(ExecutionPort):
    """CTP 交易网关：唯一发送出口的柜台侧实现，不含任何记账或风控逻辑.

    ``authority`` 返回当前持久化控制代次；``events`` 通常是唯一执行服务 (它的 ``enqueue`` /
    ``enqueue_callback_error`` 即满足 ``CtpEventSink``)。查询应答由 ``queries`` 收口。
    """

    def __init__(
        self,
        *,
        settings: CtpSettings,
        account_id: str,
        events: CtpEventSink,
        normalizer: FeedbackNormalizerPort,
        price_tick: Callable[[InstrumentId], Decimal],
        capability_profile: CapabilityProfile,
        capability_version: str,
        authority: Callable[[], ControlEpoch | None],
        binding: CtpBinding | None = None,
        queries: CtpQuerySink | None = None,
        offset_mappings: Iterable[CtpOffsetMapping] = (),
        ref_book: CtpOrderRefBook | None = None,
        source_id: str = "ctp",
        wall_time: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(settings, CtpSettings):
            raise TypeError("CTP gateway requires explicit connection settings")
        if not isinstance(capability_profile, CapabilityProfile):
            raise TypeError("CTP gateway requires the registered capability profile")
        require_text(account_id, "account_id")
        require_text(capability_version, "capability version")
        self.settings = settings
        self.account_id = account_id
        self.source_id = source_id
        self._price_tick = price_tick
        self._profile = capability_profile
        self._capability_version = capability_version
        self._authority = authority
        self._binding = binding if binding is not None else load_ctp_binding()
        self._ref_book = ref_book if ref_book is not None else CtpOrderRefBook()
        self._offsets = {mapping.exchange: mapping for mapping in offset_mappings}
        self._wall_time = wall_time or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self.router = CtpCallbackRouter(
            normalizer=normalizer,
            events=events,
            account_id=account_id,
            queries=queries,
            source_id=source_id,
            wall_time=self._wall_time,
        )
        self.router.observer = self._on_notification
        self._api: Any | None = None
        self._spi: Any | None = None
        self._lock = threading.Lock()
        self._request_id = 0
        self._connected = False
        self._authenticated = False
        self._logged_in = False
        self._settlement_confirmed = False
        self._closed = False
        self._fault: str | None = None
        self._needs_reconciliation = True
        self._front_ready = threading.Event()
        self._authenticated_ready = threading.Event()
        self._login_ready = threading.Event()
        self._settlement_ready = threading.Event()
        self._trading_day: date | None = None
        self._session_report: CtpSessionReport | None = None
        self._front_id = 0
        self._session_id = 0
        self._max_order_ref: str | None = None
        self._last_error: tuple[int | None, str | None] | None = None
        self.rejections: list[str] = []
        self._cancel_locators: list[Mapping[str, object]] = []
        self.counts = {
            "connect_attempts": 0,
            "front_connected": 0,
            "front_disconnected": 0,
            "heartbeat_warnings": 0,
            "logins": 0,
            "logouts": 0,
            "submit_accepted": 0,
            "submit_refused_local": 0,
            "cancel_accepted": 0,
            "cancel_refused_local": 0,
            "send_unknown": 0,
            "fenced_calls": 0,
            "open_without_registration": 0,
            "cancel_action_ref": 0,
            "cancel_by_session": 0,
            "cancel_by_exchange_id": 0,
            "unsupported_capability": 0,
        }

    # ------------------------------------------------------------------ 生命周期
    def __enter__(self) -> CtpTraderGateway:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        self.close()

    @property
    def binding(self) -> CtpBinding:
        return self._binding

    @property
    def api(self) -> Any | None:
        return self._api

    @property
    def session_report(self) -> CtpSessionReport | None:
        return self._session_report

    @property
    def trading_day(self) -> date | None:
        return self._trading_day

    @property
    def ref_book(self) -> CtpOrderRefBook:
        return self._ref_book

    @property
    def fault(self) -> str | None:
        return self._fault

    @property
    def ready_to_send(self) -> bool:
        return (
            self._connected
            and self._logged_in
            and self._settlement_confirmed
            and self._fault is None
            and not self._needs_reconciliation
        )

    @property
    def needs_reconciliation(self) -> bool:
        return self._needs_reconciliation

    def mark_reconciled(self) -> bool:
        """对账成功后由装配显式放行；未登录 / 有故障时不建立任何"已核"."""
        if self._fault is not None or not (self._connected and self._logged_in and self._settlement_confirmed):
            return False
        self._needs_reconciliation = False
        return True

    def close(self) -> None:
        api, self._api = self._api, None
        self._closed = True
        self._logged_in = False
        self._connected = False
        if api is not None:
            release = getattr(api, "Release", None)
            if release is not None:
                release()

    def _next_request_id(self) -> int:
        with self._lock:
            self._request_id += 1
            return self._request_id

    # 查询适配器只通过下面三个方法转发请求；它们不做任何业务判断 (ADR-01)。
    def next_request_id(self) -> int:
        return self._next_request_id()

    def new_field(self, type_name: str) -> Any:
        return self._require_binding().field(type_name)

    def send_request(self, name: str, field: Any | None, request_id: int) -> int:
        """调用柜台查询接口；``field`` 为 ``None`` 时不带结构体（如结算单确认查询）."""
        api = self._require_api()
        request = getattr(api, name, None)
        if request is None:
            raise CtpQueryError(f"CTP binding has no request {name}")
        return int(request(field, request_id) if field is not None else request(request_id))

    def _require_binding(self) -> CtpBinding:
        if self._binding is None:  # 生产默认构造即已注入绑定
            raise CtpBindingUnavailableError(f"CTP binding is unavailable; install it with {CTP_EXTRA_HINT}")
        return self._binding

    def connect(self, *, timeout_s: float | None = None) -> CtpSessionReport:
        """建立连接并完成终端认证 / 登录 / 结算确认；返回脱敏握手证据.

        任一步失败都抛出 :class:`CtpHandshakeError` 并把网关置为不可发送，绝不假装已登录。
        """
        binding = self._require_binding()
        started = self._monotonic()
        self.counts["connect_attempts"] += 1
        flow = Path(self.settings.flow_dir)
        flow.mkdir(parents=True, exist_ok=True)
        self._front_ready.clear()
        self._authenticated_ready.clear()
        self._login_ready.clear()
        self._settlement_ready.clear()
        self._fault = None
        self._needs_reconciliation = True
        api = binding.create_trader_api(str(flow))
        self._api = api
        self._spi = build_trader_spi(binding, self.router)
        api.RegisterSpi(self._spi)
        self._subscribe_private(api)
        api.RegisterFront(self.settings.front_trade)
        api.Init()
        connect_deadline = self._monotonic() + float(timeout_s or self.settings.connect_timeout_s)
        if not self._front_ready.wait(max(0.0, connect_deadline - self._monotonic())):
            self._fault = "front_not_connected"
            raise CtpHandshakeError("CTP front did not report a connection within the timeout")
        login_started = self._monotonic()
        login_deadline = login_started + float(timeout_s or self.settings.login_timeout_s)
        notes: list[str] = []
        if self.settings.authenticated:
            self._authenticate(login_deadline)
        else:
            notes.append("terminal authentication not configured (AppID/AuthCode absent); counter rule unverified")
        self._login(login_deadline)
        self._confirm_settlement(login_deadline)
        api_version = str(self._safe(lambda: binding.api_version(api)) or "unknown")
        dll_hashes = dict(self._safe(binding.dll_hashes) or {})
        self._session_report = CtpSessionReport(
            front_trade=self.settings.front_trade,
            broker_id=self.settings.broker_id,
            user_id=self.settings.user_id,
            binding=binding.name,
            binding_version=binding.version,
            api_version=api_version,
            ctp_version=self.settings.ctp_version,
            terminal_authentication=self.settings.authenticated,
            front_id=self._front_id,
            session_id=self._session_id,
            trading_day=self._trading_day,
            max_order_ref=self._max_order_ref,
            connect_seconds=login_started - started,
            login_seconds=self._monotonic() - login_started,
            restored_order_refs=self._ref_book.restored,
            dll_hashes=dll_hashes,
            notes=tuple(notes),
        )
        return self._session_report

    @staticmethod
    def _safe(call: Callable[[], Any]) -> Any | None:
        try:
            return call()
        except Exception:  # 诊断信息缺失不得打断握手
            return None

    def _subscribe_private(self, api: Any) -> None:
        """私有流从断点续传需要在联调中核验；未核验前用 QUICK (当日最新) 并显式记录."""
        subscribe_private = getattr(api, "SubscribePrivateTopic", None)
        subscribe_public = getattr(api, "SubscribePublicTopic", None)
        if subscribe_private is not None:
            subscribe_private(SUBSCRIBE_QUICK)
        if subscribe_public is not None:
            subscribe_public(SUBSCRIBE_QUICK)

    def _wait(self, event: threading.Event, deadline: float, step: str, *, achieved: Callable[[], bool]) -> None:
        """等待某一步的应答；超时或被柜台拒绝都保持关闭并发无歧义的错误."""
        if not event.wait(max(0.0, deadline - self._monotonic())):
            self._fault = step
            self._needs_reconciliation = True
            raise CtpHandshakeError(f"CTP handshake step {step} did not complete within the timeout")
        if not achieved():
            code = None if self._last_error is None else self._last_error[0]
            self._fault = self._fault or step
            self._needs_reconciliation = True
            raise CtpHandshakeError(f"CTP handshake step {step} was rejected by the counter (code={code})")

    def _authenticate(self, deadline: float) -> None:
        api: Any = self._require_api()
        field: Any = self._require_binding().field("CThostFtdcReqAuthenticateField")
        field.BrokerID = self.settings.broker_id
        field.UserID = self.settings.user_id
        field.AppID = self.settings.app_id
        field.AuthCode = self.settings.auth_code
        field.UserProductInfo = self.settings.product_info
        request_id = self._next_request_id()
        self._last_error = None
        api.ReqAuthenticate(field, request_id)
        self._wait(self._authenticated_ready, deadline, "authenticate", achieved=lambda: self._authenticated)

    def _login(self, deadline: float) -> None:
        api: Any = self._require_api()
        field: Any = self._require_binding().field("CThostFtdcReqUserLoginField")
        field.BrokerID = self.settings.broker_id
        field.UserID = self.settings.user_id
        field.Password = self.settings.password
        field.UserProductInfo = self.settings.product_info
        request_id = self._next_request_id()
        self._last_error = None
        api.ReqUserLogin(field, request_id)
        self._wait(self._login_ready, deadline, "login", achieved=lambda: self._logged_in)

    def _confirm_settlement(self, deadline: float) -> None:
        api: Any = self._require_api()
        field: Any = self._require_binding().field("CThostFtdcSettlementInfoConfirmField")
        field.BrokerID = self.settings.broker_id
        field.InvestorID = self.settings.investor_id
        request_id = self._next_request_id()
        self._last_error = None
        api.ReqSettlementInfoConfirm(field, request_id)
        self._wait(self._settlement_ready, deadline, "settlement_confirm", achieved=lambda: self._settlement_confirmed)

    def _require_api(self) -> Any:
        if self._api is None:
            raise CtpHandshakeError("CTP connection has not been created")
        return self._api

    def maintain(self, *, timeout_s: float | None = None) -> bool:
        """回调侧的连接状态变化在此收口：断线或重连后重新登录并重新要求对账.

        返回是否已重新就绪；未连接或未登录时只是保持关闭，不阻塞调用方。
        """
        if self._closed or self._api is None:
            return False
        if self.ready_to_send:
            return True
        if self._fault is not None or not self._connected:
            return False
        if self._logged_in and self._settlement_confirmed:
            return not self._needs_reconciliation
        deadline = self._monotonic() + float(timeout_s or self.settings.login_timeout_s)
        try:
            if self.settings.authenticated and not self._authenticated:
                self._authenticated_ready.clear()
                self._authenticate(deadline)
            self._login_ready.clear()
            self._login(deadline)
            self._settlement_ready.clear()
            self._confirm_settlement(deadline)
        except CtpHandshakeError as exc:
            LOGGER.warning("CTP re-login failed: %s", exc)
            return False
        return not self._needs_reconciliation

    # ------------------------------------------------------------------ 回调侧状态
    def _on_notification(self, action: str, payload: Mapping[str, object]) -> None:
        if action == "front_connected":
            self.counts["front_connected"] += 1
            self._connected = True
            self._fault = None
            self._front_ready.set()
            if self._logged_in:
                # CTP 自动重连会给出新的会话；旧会话的委托标识仍按原三元组归属
                LOGGER.warning("CTP front reconnected; the session must log in and reconcile again")
                self._logged_in = False
                self._settlement_confirmed = False
                self._needs_reconciliation = True
            return
        if action == "front_disconnected":
            self.counts["front_disconnected"] += 1
            self._connected = False
            self._logged_in = False
            self._settlement_confirmed = False
            self._authenticated = False
            self._front_ready.clear()
            self._login_ready.clear()
            self._settlement_ready.clear()
            self._authenticated_ready.clear()
            self._needs_reconciliation = True
            LOGGER.warning("CTP front disconnected (reason %s); new trading risk is closed", payload.get("reason_code"))
            return
        if action == "heartbeat_warning":
            self.counts["heartbeat_warnings"] += 1
            LOGGER.warning("CTP heartbeat warning after %s s without a message", payload.get("time_lapse_s"))
            return
        if action == "rsp_authenticate":
            code, message = _rsp_info(payload)
            if code:
                self._last_error = (code, message)
                self._fault = f"authenticate_error_{code}"
                self._authenticated_ready.set()
                return
            self._authenticated = True
            self._last_error = None
            self._authenticated_ready.set()
            return
        if action == "rsp_user_login":
            code, message = _rsp_info(payload)
            if code:
                self._last_error = (code, message)
                self._fault = f"login_error_{code}"
                self._login_ready.set()
                return
            self._front_id = _as_optional_int(payload.get("FrontID")) or 0
            self._session_id = _as_optional_int(payload.get("SessionID")) or 0
            self._max_order_ref = None if payload.get("MaxOrderRef") is None else str(payload["MaxOrderRef"])
            self._trading_day = _parse_day(payload.get("TradingDay"))
            if self._trading_day is None:
                self._fault = "login_without_trading_day"
                self._login_ready.set()
                return
            self._ref_book.open_session(self._front_id, self._session_id, self._max_order_ref)
            self._logged_in = True
            self._last_error = None
            self.counts["logins"] += 1
            self._login_ready.set()
            return
        if action == "rsp_settlement_confirm":
            code, message = _rsp_info(payload)
            if code:
                self._last_error = (code, message)
                self._fault = f"settlement_confirm_error_{code}"
                self._settlement_ready.set()
                return
            self._settlement_confirmed = True
            self._last_error = None
            self._settlement_ready.set()
            return
        if action == "rsp_user_logout":
            self.counts["logouts"] += 1
            self._logged_in = False
            self._settlement_confirmed = False
            self._needs_reconciliation = True
            return
        if action == "rsp_error":
            code, message = _rsp_info(payload)
            self._last_error = (code, message)
            LOGGER.error("CTP returned a request error (code %s)", code)
            return

    # ------------------------------------------------------------------ 发送出口
    def _fence(self, epoch: ControlEpoch) -> LocalSendResult | None:
        if not isinstance(epoch, ControlEpoch):
            raise TypeError("gateway calls require a ControlEpoch")
        current = self._authority()
        if current is None or current != epoch:
            self.counts["fenced_calls"] += 1
            return LocalSendResult(SendState.NOT_SENT, CODE_FENCED, "control epoch fence at the CTP API call")
        return None

    def _dispatchable(self) -> None:
        if self._closed or self._api is None:
            raise _LocalRejectionError(CODE_NOT_READY, "CTP gateway has no live connection")
        if self._fault is not None:
            raise _LocalRejectionError(CODE_NOT_READY, f"CTP gateway fault: {self._fault}")
        if not (self._connected and self._logged_in and self._settlement_confirmed):
            raise _LocalRejectionError(
                CODE_NOT_READY, "CTP session is not connected, logged in and settlement-confirmed"
            )
        if self._needs_reconciliation:
            raise _LocalRejectionError(CODE_NOT_READY, "account reconciliation is required before new risk is sent")

    def _capability(self, name: str) -> bool:
        capability = self._profile.values.get(name)
        return bool(capability is not None and capability.verified and bool(capability.value))

    def _offset_flag(self, instrument: InstrumentId, offset: Offset) -> str:
        if offset == Offset.OPEN:
            # 开仓标志由 API 头文件唯一定义 (THOST_FTDC_OF_Open='0')，与交易所无关，因此开仓不依赖
            # 柜台能力核验；平仓因今昨仓制度而别，未核验即禁用 (GAP-S0-05)。
            if instrument.exchange not in self._offsets:
                self.counts["open_without_registration"] += 1
            return OFFSET_OPEN
        mapping = self._offsets.get(instrument.exchange)
        if mapping is None:
            self.counts["unsupported_capability"] += 1
            raise _LocalRejectionError(
                CODE_UNSUPPORTED_CAPABILITY,
                f"no registered open/close mapping for {instrument.exchange.value}; "
                "close flags stay disabled rather than guessed",
            )
        if not mapping.verified:
            self.counts["unsupported_capability"] += 1
            raise _LocalRejectionError(
                CODE_UNSUPPORTED_CAPABILITY,
                f"open/close mapping for {instrument.exchange.value} is registered but not verified "
                "(GAP-S0-05): close flags stay disabled",
            )
        flag = mapping.flags.get(offset)
        if flag is None:
            self.counts["unsupported_capability"] += 1
            raise _LocalRejectionError(
                CODE_UNSUPPORTED_CAPABILITY,
                f"verified mapping for {instrument.exchange.value} has no flag for {offset.value}",
            )
        return flag

    def _order_price(self, order: OrderIntent) -> Decimal | None:
        if order.order_type == OrderType.MARKET:
            if not self._capability("order_types.market_order"):
                self.counts["unsupported_capability"] += 1
                raise _LocalRejectionError(
                    CODE_UNSUPPORTED_CAPABILITY,
                    "market orders are not verified for this counter (GAP-S0-05); send a limit price instead",
                )
            return None
        assert order.limit_price_ticks is not None  # 领域已保证 LIMIT 必须带整数价位
        tick = Decimal(self._price_tick(order.instrument))
        if not tick.is_finite() or tick <= 0:
            raise _LocalRejectionError(
                CODE_UNSUPPORTED_CAPABILITY, "contract price tick is unknown; price cannot be derived"
            )
        price = Decimal(order.limit_price_ticks) * tick
        if price <= 0:
            raise _LocalRejectionError(CODE_UNSUPPORTED_CAPABILITY, "a CTP limit price must be positive")
        return price

    def plan_order(self, order: OrderIntent) -> Mapping[str, object]:
        """把已风控的子单翻译成 CTP 字段；未核验能力在此明确失败 (供预检与测试使用)."""
        if not isinstance(order, OrderIntent):
            raise TypeError("the gateway only accepts final child intents")
        if order.account_id != self.account_id:
            raise ValueError("order intent belongs to another account")
        flag = self._offset_flag(order.instrument, order.offset)
        price = self._order_price(order)
        return {
            "InstrumentID": order.instrument.symbol,
            "ExchangeID": order.instrument.exchange.value,
            "Direction": DIRECTION_BUY if order.side == Side.BUY else DIRECTION_SELL,
            "CombOffsetFlag": flag,
            "CombHedgeFlag": HEDGE_SPECULATION,
            "OrderPriceType": PRICE_LIMIT if price is not None else PRICE_ANY,
            "LimitPrice": 0.0 if price is None else float(price),
            "VolumeTotalOriginal": int(order.quantity),
            "TimeCondition": TIME_GFD,
            "VolumeCondition": VOLUME_ANY,
            "MinVolume": 1,
            "ContingentCondition": CONTINGENT_IMMEDIATELY,
            "ForceCloseReason": FORCE_CLOSE_NOT,
            "IsAutoSuspend": 0,
            "UserForceClose": 0,
        }

    def submit(self, order: OrderIntent, epoch: ControlEpoch) -> LocalSendResult:
        fenced = self._fence(epoch)
        if fenced is not None:
            return fenced
        try:
            self._dispatchable()
            plan = self.plan_order(order)
        except _LocalRejectionError as rejection:
            self.counts["submit_refused_local"] += 1
            self.rejections.append(rejection.evidence)
            return LocalSendResult(SendState.NOT_SENT, rejection.code, rejection.evidence)
        except Exception as exc:
            # 规划阶段只读本地状态：这里能证明"没有发送"，因此返回 NOT_SENT，而不是让执行服务把
            # 本地失败记成"可能已发出"的 SENT_UNKNOWN。
            self.counts["submit_refused_local"] += 1
            evidence = f"order planning failed before any counter call ({type(exc).__name__})"
            self.rejections.append(evidence)
            return LocalSendResult(SendState.NOT_SENT, CODE_PLAN_REJECTED, evidence)
        try:
            ref = self._ref_book.allocate(order.client_order_id, order.instrument)
            identity = OrderIdentity(
                account_id=self.account_id,
                exchange=order.instrument.exchange,
                client_order_id=order.client_order_id,
                front_id=ref.front_id,
                session_id=ref.session_id,
                order_ref=ref.order_ref,
            )
        except (CtpHandshakeError, ValueError, TypeError) as exc:
            # 本地无法形成可归属标识：没有调用柜台，因此是 NOT_SENT 而不是未知发送
            self.counts["submit_refused_local"] += 1
            evidence = f"no attributable local order reference could be assigned ({type(exc).__name__})"
            self.rejections.append(evidence)
            return LocalSendResult(SendState.NOT_SENT, CODE_NOT_READY, evidence)
        # 最后一次复核紧贴实际 API 调用 (ADR-X1)；不一致绝不调用柜台
        fenced = self._fence(epoch)
        if fenced is not None:
            return LocalSendResult(fenced.state, fenced.local_code, fenced.evidence, identity)
        request_id = self._next_request_id()
        try:
            field: Any = self._require_binding().field("CThostFtdcInputOrderField")
            field.BrokerID = self.settings.broker_id
            field.InvestorID = self.settings.investor_id
            field.UserID = self.settings.user_id
            field.OrderRef = ref.order_ref
            for name, value in plan.items():
                setattr(field, name, value)
        except (CtpBindingUnavailableError, AttributeError, TypeError, ValueError) as exc:
            self.counts["submit_refused_local"] += 1
            evidence = f"order cannot be translated to a CTP request ({type(exc).__name__}); nothing was sent"
            self.rejections.append(evidence)
            return LocalSendResult(SendState.NOT_SENT, CODE_NOT_READY, evidence)
        try:
            code = int(self._require_api().ReqOrderInsert(field, request_id))  # type: ignore[attr-defined]
        finally:
            self._release(field)
        return self._send_result(code, request_id, "ReqOrderInsert", ref, identity)

    @staticmethod
    def _release(field: Any) -> None:
        """交回柜台结构体所有权，避免长运行累积；失败不影响已发出的请求."""
        try:
            field.thisown = True  # type: ignore[attr-defined]
        except Exception:
            pass

    def cancel(self, ref: OrderIdentity, epoch: ControlEpoch) -> LocalSendResult:
        fenced = self._fence(epoch)
        if fenced is not None:
            return fenced
        try:
            self._dispatchable()
        except _LocalRejectionError as rejection:
            self.counts["cancel_refused_local"] += 1
            self.rejections.append(rejection.evidence)
            return LocalSendResult(SendState.NOT_SENT, rejection.code, rejection.evidence)
        if not isinstance(ref, OrderIdentity):
            raise TypeError("cancellation requires a normalized order identity")
        triple = None
        if ref.front_id is not None and ref.session_id is not None and ref.order_ref:
            triple = (ref.front_id, ref.session_id, ref.order_ref)
        if triple is None and not ref.exchange_order_id:
            return self._refuse_cancel(
                CODE_INVALID_IDENTITY,
                "cancellation requires either the order's own session triple or its exchange order id",
            )
        instrument = self._ref_book.instrument_for(ref.client_order_id)
        if instrument is None:
            # 撤单必须带合约代码（CTP 演示与 SimTime 实测都如此）；本地不知道就拒绝，不猜
            return self._refuse_cancel(
                CODE_INVALID_IDENTITY,
                "cancellation needs the contract; it is not known locally for this order",
            )
        # 柜台只用当前会话的原会话三元组定位报单；旧会话的报单必须走交易所单号，
        # 否则 SimNow 会返回错误码 25 (CTP:不能找到对应的报单)——2026-09-25 实测。
        same_session = triple is not None and self._ref_book.session == (triple[0], triple[1])
        if not same_session and not ref.exchange_order_id:
            return self._refuse_cancel(
                CODE_INVALID_IDENTITY,
                "an order placed in a former session can only be cancelled by its exchange order id",
            )
        locator = "session" if same_session else "exchange"
        self.counts["cancel_action_ref"] += 1
        request_id = self._next_request_id()
        try:
            field: Any = self._require_binding().field("CThostFtdcInputOrderActionField")
            field.BrokerID = self.settings.broker_id
            field.InvestorID = self.settings.investor_id
            field.UserID = self.settings.user_id
            field.ActionFlag = ACTION_DELETE
            field.ExchangeID = ref.exchange.value
            field.InstrumentID = instrument.symbol
            field.OrderActionRef = self.counts["cancel_action_ref"]
            if same_session and triple is not None:
                field.OrderRef = str(triple[2])
                field.FrontID = int(triple[0])
                field.SessionID = int(triple[1])
            if ref.exchange_order_id:
                field.OrderSysID = ref.exchange_order_id
        except (CtpBindingUnavailableError, AttributeError, TypeError, ValueError) as exc:
            return self._refuse_cancel(
                CODE_INVALID_IDENTITY,
                f"cancellation cannot be translated to a CTP locator ({type(exc).__name__})",
            )
        # 最后一次复核紧贴实际 API 调用 (ADR-X1)
        fenced = self._fence(epoch)
        if fenced is not None:
            return fenced
        try:
            code = int(self._require_api().ReqOrderAction(field, request_id))  # type: ignore[attr-defined]
        finally:
            self._release(field)
        evidence_ref = (
            CtpOrderRef(*triple)
            if triple is not None
            else CtpOrderRef(int(field.FrontID or 0), int(field.SessionID or 0), str(field.OrderRef or "0"))
        )
        self._cancel_locators.append(
            {"locator": locator, "request_id": request_id, "order_ref": evidence_ref.order_ref}
        )
        return self._send_result(code, request_id, f"ReqOrderAction({locator})", evidence_ref, ref)

    def _refuse_cancel(self, code: int, evidence: str) -> LocalSendResult:
        self.counts["cancel_refused_local"] += 1
        self.rejections.append(evidence)
        return LocalSendResult(SendState.NOT_SENT, code, evidence)

    def _send_result(
        self, code: int, request_id: int, action: str, ref: CtpOrderRef, identity: OrderIdentity
    ) -> LocalSendResult:
        """本地返回 0 只说明请求被 API 受理；非零返回在没有柜台实测前不假定为"未发送"."""
        prefix = order_ref_evidence(ref) + "; "
        if code == 0:
            if action == "ReqOrderInsert":
                self.counts["submit_accepted"] += 1
            else:
                self.counts["cancel_accepted"] += 1
                self.counts["cancel_by_session" if "(session)" in action else "cancel_by_exchange_id"] += 1
            return LocalSendResult(
                SendState.SENT_UNKNOWN,
                0,
                f"{prefix}{action} accepted locally (request_id={request_id}); remote processing is unconfirmed",
                identity,
            )
        if code in self.settings.local_reject_codes:
            self.counts["submit_refused_local" if action == "ReqOrderInsert" else "cancel_refused_local"] += 1
            return LocalSendResult(
                SendState.NOT_SENT,
                code,
                f"{prefix}{action} rejected locally by the API (code={code}, request_id={request_id})",
                identity,
            )
        self.counts["send_unknown"] += 1
        return LocalSendResult(
            SendState.SENT_UNKNOWN,
            code,
            f"{prefix}{action} returned unverified code {code} (request_id={request_id}); "
            "non-zero return semantics are not verified against the counter yet",
            identity,
        )

    def capabilities(self) -> VersionedValue[CapabilityProfile]:
        now = self._wall_time()
        return VersionedValue(
            value=self._profile,
            source_id=f"broker_profile:{self._profile.profile_id}",
            version=self._capability_version,
            effective_from=now,
            available_at=now,
        )

    def status(self) -> Mapping[str, object]:
        """脱敏运行状态；给清单、看门狗脚本与证据使用，不含口令 / AuthCode."""
        return {
            "binding": self._binding.name,
            "binding_version": self._binding.version,
            "front_trade": self.settings.front_trade,
            "broker_id": self.settings.broker_id,
            "terminal_authentication": self.settings.authenticated,
            "connected": self._connected,
            "logged_in": self._logged_in,
            "settlement_confirmed": self._settlement_confirmed,
            "needs_reconciliation": self._needs_reconciliation,
            "fault": self._fault,
            # 柜台最近一次错误码与报文：诊断登录失败必须看这个，不能只看本地状态
            "last_error_code": None if self._last_error is None else self._last_error[0],
            "last_error_message": None if self._last_error is None else self._last_error[1],
            "trading_day": None if self._trading_day is None else self._trading_day.isoformat(),
            "front_id": self._front_id,
            "session_id": self._session_id,
            "order_refs_allocated": self._ref_book.allocated,
            "order_refs_restored": self._ref_book.restored,
            "counts": dict(self.counts),
            "callbacks": dict(self.router.counts),
            "last_local_rejections": self.rejections[-5:],
            "cancel_locators": [dict(item) for item in self._cancel_locators[-5:]],
        }
