"""[Gateway 适配器] CTP 行情通道：MdApi 登录、订阅与逐笔快照归一化 (S5-01 行情面, FR-DATA-02, FR-CAL-03).

职责与边界：

- 回调只做字段转换 / 哨兵值剔除 / 入队，绝不改状态、绝不写库 (ADR-X2)；行情事件进入服务侧的
  有界行情队列，队列满时的丢弃由执行服务按数据质量门禁处理。
- **哨兵值不能当价格**：CTP 用 ``DBL_MAX`` (1.7976931348623157e308) 表示"无有效值"。价格字段命中
  哨兵一律记 ``None``；``LastPrice`` 命中哨兵说明这一笔没有成交价（无成交合约的常态），整笔快照
  记为 ``empty_snapshots`` 不产生事件，也不把缺失读成 0。
- 交易日只认柜台 ``TradingDay``（夜盘属于下一交易日），事件时刻用 ``ActionDay``（自然日）+
  ``UpdateTime``/``UpdateMillisec`` 还原为 UTC；与本地接收时刻明显不符时退回接收时刻并记异常
  (FR-CAL-03 的时钟口径，阈值待联调)。
- 交易时段 / 合约状态不在本层判断：``Tick.phase`` 记 ``UNKNOWN``，权限由日历与会话门禁决定。
- 行情转换失败按数据质量缺口登记（计数 + 有界证据 + 告警），**不**触发执行服务关闭交易门禁
  （``enqueue_callback_error`` 会关闭门禁，那只适用于订单 / 成交回报）。
"""

from __future__ import annotations

import importlib
import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from qh_trader.core.constants import EventKind, Exchange, MarketPhase
from qh_trader.core.event import CanonicalEvent
from qh_trader.core.objects import InstrumentId, RecordMeta, Tick, require_text

from .ctp_gateway import (
    CTP_EXTRA_HINT,
    EXCHANGE_TZ,
    CtpBindingUnavailableError,
    CtpEventSink,
    CtpHandshakeError,
    CtpSettings,
    _as_optional_int,
    _read_fields,
)

LOGGER = logging.getLogger(__name__)

# CTP 用 double 的最大值表示"该字段无有效值"；它不是价格，不能参与任何计算。
CTP_SENTINEL = 1.7976931348623157e308

MARKET_DATA_FIELDS = (
    "InstrumentID",
    "ExchangeID",
    "TradingDay",
    "ActionDay",
    "UpdateTime",
    "UpdateMillisec",
    "LastPrice",
    "BidPrice1",
    "BidVolume1",
    "AskPrice1",
    "AskVolume1",
    "Volume",
    "Turnover",
    "OpenInterest",
    "PreSettlementPrice",
    "UpperLimitPrice",
    "LowerLimitPrice",
    "PreClosePrice",
    "OpenPrice",
    "HighestPrice",
    "LowestPrice",
    "AveragePrice",
)
SUBSCRIBE_CALLBACKS = {
    "subscribe": "OnRspSubMarketData",
    "unsubscribe": "OnRspUnSubMarketData",
}


@dataclass(frozen=True, slots=True)
class CtpMarketSettings:
    """行情前置的接入参数；口令同样只来自本地秘密配置."""

    front_market: str
    broker_id: str
    user_id: str
    password: str = field(repr=False)
    flow_dir: str = "runs/live/ctp_md_flow"
    connect_timeout_s: float = 20.0
    login_timeout_s: float = 20.0
    subscribe_timeout_s: float = 10.0

    def __post_init__(self) -> None:
        for name in ("front_market", "broker_id", "user_id", "password"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required to contact a market data front")
        if not self.front_market.startswith("tcp://"):
            raise ValueError("a CTP market data front address must be a tcp:// URI")
        for name, limit in (("broker_id", 10), ("user_id", 15), ("password", 40)):
            if len(getattr(self, name)) > limit:
                raise ValueError(f"{name} exceeds the CTP field limit of {limit} characters")

    @classmethod
    def from_settings(cls, settings: CtpSettings, *, front_market: str) -> CtpMarketSettings:
        """从已核验的交易接入参数派生行情参数，避免同一账户出现两套口径."""
        return cls(
            front_market=front_market,
            broker_id=settings.broker_id,
            user_id=settings.user_id,
            password=settings.password,
            flow_dir=settings.flow_dir.rstrip("/") + "-md",
            connect_timeout_s=settings.connect_timeout_s,
            login_timeout_s=settings.login_timeout_s,
        )


@runtime_checkable
class CtpMarketBinding(Protocol):
    """行情绑定：与交易绑定同源，但只需要 MdApi 表面."""

    def create_md_api(self, flow_dir: str) -> Any: ...
    def md_spi_base(self) -> Any: ...


class OpenCtpMarketBinding:
    """``openctp-ctp`` 的行情模块（``openctp_ctp.mdapi``）."""

    name = "openctp-ctp"
    version = "unavailable"

    def create_md_api(self, flow_dir: str) -> Any:
        try:
            package = importlib.import_module("openctp_ctp")
            module = package.mdapi
        except (ImportError, OSError, AttributeError) as exc:
            raise CtpBindingUnavailableError(
                f"CTP market data binding is unavailable ({type(exc).__name__}); install it with {CTP_EXTRA_HINT}"
            ) from exc
        self.version = str(getattr(package, "__version__", "unknown"))
        return module.CThostFtdcMdApi.CreateFtdcMdApi(flow_dir)

    def md_spi_base(self) -> Any:
        try:
            package = importlib.import_module("openctp_ctp")
        except (ImportError, OSError, AttributeError) as exc:
            raise CtpBindingUnavailableError(
                f"CTP market data binding is unavailable ({type(exc).__name__}); install it with {CTP_EXTRA_HINT}"
            ) from exc
        return package.mdapi.CThostFtdcMdSpi

    def login_field(self) -> Any:
        return self.snapshot_field("CThostFtdcReqUserLoginField")

    def snapshot_field(self, type_name: str) -> Any:
        package = importlib.import_module("openctp_ctp")
        return getattr(package.mdapi, type_name)()


def load_ctp_market_binding() -> OpenCtpMarketBinding:
    return OpenCtpMarketBinding()


class CtpMarketDataGateway:
    """行情通道：连接行情前置、订阅合约，把逐笔快照归一化后入队."""

    def __init__(
        self,
        *,
        settings: CtpMarketSettings,
        events: CtpEventSink,
        binding: OpenCtpMarketBinding | None = None,
        source_id: str = "ctp-md",
        wall_time: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        timestamp_tolerance: timedelta = timedelta(minutes=5),
        evidence_limit: int = 50,
    ) -> None:
        if not isinstance(settings, CtpMarketSettings):
            raise TypeError("the market data gateway requires explicit front settings")
        self.settings = settings
        self.source_id = source_id
        self._events = events
        self._binding = binding if binding is not None else load_ctp_market_binding()
        self._wall_time = wall_time or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self.timestamp_tolerance = timestamp_tolerance
        self.evidence_limit = evidence_limit
        self._api: Any | None = None
        self._spi: Any | None = None
        self._connected = False
        self._logged_in = False
        self._closed = False
        self._fault: str | None = None
        self._ingest_seq = 0
        self._subscribed: set[str] = set()
        self._exchanges: dict[str, Exchange] = {}
        self._front_ready = threading.Event()
        self._login_ready = threading.Event()
        self._subscribe_ready: dict[str, threading.Event] = {}
        self._last_error: tuple[int | None, str | None] | None = None
        self.counts = {
            "connect_attempts": 0,
            "front_connected": 0,
            "front_disconnected": 0,
            "logins": 0,
            "snapshots": 0,
            "ticks_enqueued": 0,
            "empty_snapshots": 0,
            "sentinel_prices": 0,
            "conversion_failures": 0,
            "timestamp_anomalies": 0,
            "subscribe_requests": 0,
            "subscribe_rejections": 0,
        }
        self.evidence: list[Mapping[str, object]] = []

    # ------------------------------------------------------------------ 生命周期
    @property
    def ready(self) -> bool:
        return self._logged_in and self._connected and self._fault is None

    @property
    def fault(self) -> str | None:
        return self._fault

    @property
    def subscribed(self) -> tuple[str, ...]:
        return tuple(sorted(self._subscribed))

    def connect(self, *, timeout_s: float | None = None) -> Mapping[str, object]:
        """连接行情前置并登录；失败即抛错，不假装已就绪."""
        self.counts["connect_attempts"] += 1
        flow = Path(self.settings.flow_dir)
        flow.mkdir(parents=True, exist_ok=True)
        self._front_ready.clear()
        self._login_ready.clear()
        self._fault = None
        api = self._binding.create_md_api(str(flow))
        self._api = api
        self._spi = build_market_spi(self._binding, self)
        api.RegisterSpi(self._spi)
        api.RegisterFront(self.settings.front_market)
        api.Init()
        connect_deadline = self._monotonic() + float(timeout_s or self.settings.connect_timeout_s)
        if not self._front_ready.wait(max(0.0, connect_deadline - self._monotonic())):
            self._fault = "market_front_not_connected"
            raise CtpHandshakeError("CTP market data front did not report a connection within the timeout")
        login_deadline = self._monotonic() + float(timeout_s or self.settings.login_timeout_s)
        login = self._binding.login_field()
        login.BrokerID = self.settings.broker_id
        login.UserID = self.settings.user_id
        login.Password = self.settings.password
        self._last_error = None
        api.ReqUserLogin(login, 1)
        if not self._login_ready.wait(max(0.0, login_deadline - self._monotonic())) or not self._logged_in:
            code = None if self._last_error is None else self._last_error[0]
            self._fault = f"market_login_error_{code}" if code else "market_login"
            raise CtpHandshakeError(f"CTP market data login failed (code={code})")
        return {
            "front_market": self.settings.front_market,
            "broker_id": self.settings.broker_id,
            "binding": self._binding.name,
            "binding_version": self._binding.version,
            "logged_in": True,
        }

    def subscribe(self, instruments: Sequence[InstrumentId], *, timeout_s: float | None = None) -> tuple[str, ...]:
        """订阅行情；返回本次实际得到应答的合约，未应答的合约保持未订阅状态."""
        if not self.ready:
            raise CtpHandshakeError("market data subscription requires a logged-in market session")
        symbols = [
            str(instrument.symbol) for instrument in instruments if str(instrument.symbol) not in self._subscribed
        ]
        if not symbols:
            return ()
        events = {}
        for symbol in symbols:
            events[symbol] = threading.Event()
        self._subscribe_ready = events
        # 先登记合约与交易所再发订阅：柜台可能在应答之前就推送快照，而快照实测不带 ExchangeID
        for symbol, instrument in zip(symbols, instruments, strict=False):
            self._exchanges[symbol] = instrument.exchange
        self.counts["subscribe_requests"] += len(symbols)
        # SWIG 包装的签名是 (list[bytes], nCount)：元素必须是 bytes，元素也不能用元组
        encoded = [symbol.encode("utf-8") for symbol in symbols]
        self._require_api().SubscribeMarketData(encoded, len(encoded))
        deadline = self._monotonic() + float(timeout_s or self.settings.subscribe_timeout_s)
        accepted = []
        for symbol in symbols:
            if events[symbol].wait(max(0.0, deadline - self._monotonic())) and symbol in self._subscribed:
                accepted.append(symbol)
        return tuple(accepted)

    def close(self) -> None:
        api, self._api = self._api, None
        self._closed = True
        self._connected = False
        self._logged_in = False
        if api is not None:
            release = getattr(api, "Release", None)
            if release is not None:
                release()

    def _require_api(self) -> Any:
        if self._api is None:
            raise CtpHandshakeError("CTP market data connection has not been created")
        return self._api

    # ------------------------------------------------------------------ 回调入口（只转换、只入队）
    def on_front_connected(self) -> None:
        self.counts["front_connected"] += 1
        self._connected = True
        self._front_ready.set()

    def on_front_disconnected(self, reason: int) -> None:
        self.counts["front_disconnected"] += 1
        self._connected = False
        self._logged_in = False
        self._front_ready.clear()
        self._login_ready.clear()
        LOGGER.warning("CTP market data front disconnected (reason %s)", reason)

    def on_login(self, payload: Mapping[str, object]) -> None:
        code = _as_optional_int(payload.get("ErrorID"))
        if code:
            self._last_error = (code, None if payload.get("ErrorMsg") is None else str(payload["ErrorMsg"]))
            self._login_ready.set()
            return
        self._logged_in = True
        self.counts["logins"] += 1
        self._last_error = None
        self._login_ready.set()

    def on_subscribe_reply(self, kind: str, payload: Mapping[str, object]) -> None:
        code = _as_optional_int(payload.get("ErrorID"))
        symbol = None if payload.get("InstrumentID") is None else str(payload["InstrumentID"])
        if code or not symbol:
            self.counts["subscribe_rejections"] += 1
            self._record_evidence({"subscribe_failure": symbol, "counter_error_code": code})
            if symbol and symbol in self._subscribe_ready:
                self._subscribe_ready[symbol].set()
            return
        if kind == "subscribe":
            self._subscribed.add(symbol)
        else:
            self._subscribed.discard(symbol)
        if symbol in self._subscribe_ready:
            self._subscribe_ready[symbol].set()

    def on_market_data(self, payload: object) -> None:
        self.counts["snapshots"] += 1
        received_at = self._wall_time()
        raw = _read_fields(payload, MARKET_DATA_FIELDS)
        try:
            tick = self._tick(raw, received_at)
        except Exception as exc:
            # 行情转换失败是数据质量缺口，不关闭交易门禁
            self.counts["conversion_failures"] += 1
            self._record_evidence(
                {
                    "market_data_failure": type(exc).__name__,
                    "instrument": raw.get("InstrumentID"),
                    "reason": str(exc)[:200],
                }
            )
            LOGGER.warning("CTP market data snapshot could not be normalized (%s)", type(exc).__name__)
            return
        if tick is None:
            return
        event = CanonicalEvent(
            event_id=f"{self.source_id}:{tick.meta.trading_day.isoformat()}:{tick.instrument.symbol}:"
            f"{tick.meta.source_seq}",
            kind=EventKind.MARKET_DATA,
            event_time=tick.meta.event_time,
            available_at=tick.meta.available_at,
            sequence=0,
            source_id=self.source_id,
            payload=tick,
        )
        if self._events.enqueue(event):
            self.counts["ticks_enqueued"] += 1

    # ------------------------------------------------------------------ 归一化
    def _tick(self, raw: Mapping[str, object], received_at: datetime) -> Tick | None:
        instrument = self._instrument(raw)
        trading_day = _trading_day(raw)
        last_price = _price(raw, "LastPrice")
        if last_price is None:
            # 无成交（收盘后或冷门合约）：这是常态，不产生事件也不读成 0
            self.counts["empty_snapshots"] += 1
            return None
        event_time, from_counter = exchange_instant(
            None if raw.get("ActionDay") is None else str(raw["ActionDay"]),
            None if raw.get("UpdateTime") is None else str(raw["UpdateTime"]),
            received_at,
            update_millisec=_as_optional_int(raw.get("UpdateMillisec")),
            tolerance=self.timestamp_tolerance,
        )
        if not from_counter:
            self.counts["timestamp_anomalies"] += 1
        self._ingest_seq += 1
        meta = RecordMeta(
            event_time=event_time,
            available_at=received_at,
            ingested_at=received_at,
            trading_day=trading_day,
            source_id=self.source_id,
            source_version=self._binding.version,
            ingest_seq=self._ingest_seq,
            receive_time=received_at,
        )
        return Tick(
            instrument=instrument,
            meta=meta,
            last_price=last_price,
            bid_price=_price(raw, "BidPrice1"),
            ask_price=_price(raw, "AskPrice1"),
            bid_volume=_optional_count(raw.get("BidVolume1")),
            ask_volume=_optional_count(raw.get("AskVolume1")),
            cumulative_volume=_count(raw, "Volume"),
            cumulative_turnover=_price(raw, "Turnover") or Decimal(0),
            open_interest=_count(raw, "OpenInterest"),
            pre_settlement_price=_price(raw, "PreSettlementPrice"),
            upper_limit_price=_price(raw, "UpperLimitPrice"),
            lower_limit_price=_price(raw, "LowerLimitPrice"),
            phase=MarketPhase.UNKNOWN,
        )

    def _instrument(self, raw: Mapping[str, object]) -> InstrumentId:
        """合约标识：快照不带交易所代码时用订阅时的登记补齐；两者都有则必须一致."""
        symbol = raw.get("InstrumentID")
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("a market data snapshot must carry its instrument id")
        declared = raw.get("ExchangeID")
        registered = self._exchanges.get(symbol)
        if isinstance(declared, str) and declared:
            try:
                exchange = Exchange(declared)
            except ValueError as exc:
                raise ValueError(f"unregistered exchange {declared!r} in a market data snapshot") from exc
            if registered is not None and exchange != registered:
                raise ValueError(f"snapshot exchange {exchange.value} differs from the subscribed {registered.value}")
            return InstrumentId(exchange, symbol)
        if registered is None:
            raise ValueError("the snapshot carries no exchange and the instrument was not subscribed locally")
        return InstrumentId(registered, symbol)

    def _record_evidence(self, entry: Mapping[str, object]) -> None:
        self.evidence.append(dict(entry))
        if len(self.evidence) > self.evidence_limit:
            del self.evidence[: len(self.evidence) - self.evidence_limit]

    def status(self) -> Mapping[str, object]:
        return {
            "front_market": self.settings.front_market,
            "binding": self._binding.name,
            "binding_version": self._binding.version,
            "connected": self._connected,
            "logged_in": self._logged_in,
            "fault": self._fault,
            "subscribed": list(self.subscribed),
            "counts": dict(self.counts),
            "recent_evidence": [dict(item) for item in self.evidence[-5:]],
        }


def build_market_spi(binding: CtpMarketBinding, gateway: CtpMarketDataGateway) -> Any:
    """生成 MdApi 的 SPI 实例；回调函数直接写进类字典以避免基类默认实现优先."""

    def on_front_connected(self: object) -> None:
        gateway.on_front_connected()

    def on_front_disconnected(self: object, n_reason: int) -> None:
        gateway.on_front_disconnected(int(n_reason))

    def on_rsp_user_login(self: object, field: object, info: object, request_id: int, is_last: bool) -> None:
        payload = _read_fields(info, ("ErrorID", "ErrorMsg"))
        if field is not None:
            payload.update(_read_fields(field, ("TradingDay", "SessionID", "FrontID")))
        gateway.on_login(payload)

    def on_rsp_sub_market_data(self: object, field: object, info: object, request_id: int, is_last: bool) -> None:
        payload = _read_fields(info, ("ErrorID", "ErrorMsg"))
        if field is not None:
            payload.update(_read_fields(field, ("InstrumentID",)))
        gateway.on_subscribe_reply("subscribe", payload)

    def on_rsp_un_sub_market_data(self: object, field: object, info: object, request_id: int, is_last: bool) -> None:
        payload = _read_fields(info, ("ErrorID", "ErrorMsg"))
        if field is not None:
            payload.update(_read_fields(field, ("InstrumentID",)))
        gateway.on_subscribe_reply("unsubscribe", payload)

    def on_rtn_depth_market_data(self: object, field: object) -> None:
        gateway.on_market_data(field)

    namespace = {
        "OnFrontConnected": on_front_connected,
        "OnFrontDisconnected": on_front_disconnected,
        "OnRspUserLogin": on_rsp_user_login,
        "OnRspSubMarketData": on_rsp_sub_market_data,
        "OnRspUnSubMarketData": on_rsp_un_sub_market_data,
        "OnRtnDepthMarketData": on_rtn_depth_market_data,
    }
    return type("CtpMdSpi", (binding.md_spi_base(),), namespace)()


# --------------------------------------------------------------------------------------- 字段工具


def _instrument(raw: Mapping[str, object]) -> InstrumentId:
    exchange = raw.get("ExchangeID")
    symbol = raw.get("InstrumentID")
    if not isinstance(exchange, str) or not isinstance(symbol, str) or not symbol:
        raise ValueError("a market data snapshot must carry its exchange and instrument id")
    try:
        return InstrumentId(Exchange(exchange), symbol)
    except ValueError as exc:
        raise ValueError(f"unregistered exchange {exchange!r} in a market data snapshot") from exc


def _trading_day(raw: Mapping[str, object]) -> "datetime.date":  # type: ignore[valid-type]
    text = raw.get("TradingDay")
    if not isinstance(text, str) or len(text) != 8 or not text.isdigit():
        raise ValueError("the market data snapshot carries no usable trading day")
    return datetime.strptime(text, "%Y%m%d").date()


def _optional_count(value: object) -> int | None:
    """可缺失的整数计数（买卖量）；缺失返回 ``None``，非整数明确失败."""
    if value in (None, ""):
        return None
    return _as_count(value, "volume")


def _count(raw: Mapping[str, object], name: str) -> int:
    """必填的整数计数（累计成交量 / 持仓量）：缺失或非整数都明确失败，绝不读成 0."""
    return _as_count(raw.get(name), name)


def _as_count(value: object, name: str) -> int:
    if isinstance(value, bool) or value in (None, ""):
        raise ValueError(f"{name} must be an integer count")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} is not a number") from exc
    if not number.is_finite() or number != number.to_integral_value():
        raise ValueError(f"{name} must be an integral count")
    return int(number)


def _price(raw: Mapping[str, object], name: str) -> Decimal | None:
    """价格字段：缺失或命中 CTP 哨兵一律返回 ``None``（不读成 0）."""
    value = raw.get(name)
    if value in (None, ""):
        return None
    try:
        price = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} is not a decimal") from exc
    if not price.is_finite():
        return None
    if float(price) >= CTP_SENTINEL * 0.99 or price <= 0:
        # 哨兵值或非正价格都不是有效价格
        return None
    return price


def exchange_instant(
    day_text: str | None,
    time_text: str | None,
    received_at: datetime,
    *,
    update_millisec: int | None = None,
    tolerance: timedelta = timedelta(minutes=5),
) -> tuple[datetime, bool]:
    """柜台日期 + 时间 + 毫秒 → UTC 时刻；与本地接收时刻明显不符时退回接收时刻.

    ``ActionDay`` 是自然日（夜盘不会跳到次日），因此这里不用交易日字段拼时刻。
    """
    if not day_text or not time_text or len(day_text) != 8 or not day_text.isdigit():
        return received_at, False
    clock = time_text.strip()
    if "." in clock:  # 少数柜台返回 HH:MM:SS.sss
        clock = clock.split(".", 1)[0]
    try:
        naive = datetime.strptime(f"{day_text} {clock}", "%Y%m%d %H:%M:%S")
    except ValueError:
        return received_at, False
    if update_millisec:
        naive = naive.replace(microsecond=int(update_millisec) * 1000)
    candidate = naive.replace(tzinfo=EXCHANGE_TZ).astimezone(timezone.utc)
    if candidate > received_at + tolerance:
        return received_at, False
    return candidate, True


def require_market_symbol(instrument: InstrumentId) -> str:
    require_text(str(instrument.symbol), "instrument symbol")
    return str(instrument.symbol)
