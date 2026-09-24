"""[Gateway 适配器] CTP 回报归一化：原始回调 → 规范事件 (S5-02, FR-ORD-05, FR-REC-02, ADR-X2).

只做字段校验、标识关联与去重键构造。本模块**不记账**：订单、持仓、资金与风险计算仍在内核。

归一化口径（每条都能追溯到 CTP 头文件或已登记缺口）：

- 交易日只认柜台 ``TradingDay`` 字段；缺失即明确失败，绝不用本地自然日或 UTC 日期推算
  （规划 §2.2、FR-CAL-03）。
- 事件时刻优先取柜台的日期 + 时间字段（交易所时区固定 UTC+8，无夏令时）；当它与本地接收时刻明显
  不符（柜台日期在夜盘按交易日填写）时退回落本地接收时刻并记 ``timestamp_anomalies``，
  不用柜台字段凑出一个可能落在未来的时刻。
- 平仓方向标志取自 ``ThostFtdcUserApiDataType.h`` 的 ``THOST_FTDC_OF_*``；枚举无法表达的柜台标志
  （强平 / 强减 / 本地强平）明确失败，不当作普通平仓入账。
- 成交去重键作用域：``TradeKey(account, exchange, trading_day, TradeID)``。CTP 声明 ``TradeID`` 是
  交易所成交编号、在同一交易日内唯一；该唯一性假设须在柜台联调中核验 (FR-REC-02，见 07 证据表)。
- 无法归属或当前事件类型无法表达的回报返回 ``None``，并由 :class:`CtpCallbackRouter` 写入审计事件，
  不静默丢弃。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from qh_trader.core.constants import EventKind, Exchange, Offset, OrderStatus, Side
from qh_trader.core.event import CanonicalEvent
from qh_trader.core.objects import InstrumentId, OrderIdentity, OrderUpdate, Trade, TradeKey
from qh_trader.core.ports import FeedbackNormalizerPort

from .ctp_gateway import (
    DIRECTION_BUY,
    DIRECTION_SELL,
    EXCHANGE_TZ,
    OFFSET_CLOSE,
    OFFSET_CLOSE_TODAY,
    OFFSET_CLOSE_YESTERDAY,
    OFFSET_OPEN,
    ORDER_STATUS_ALL_TRADED,
    ORDER_STATUS_CANCELED,
    ORDER_STATUS_NO_TRADE_NOT_QUEUEING,
    ORDER_STATUS_NO_TRADE_QUEUEING,
    ORDER_STATUS_PART_TRADED_NOT_QUEUEING,
    ORDER_STATUS_PART_TRADED_QUEUEING,
    SUBMIT_STATUS_CANCEL_REJECTED,
    SUBMIT_STATUS_INSERT_REJECTED,
    SUBMIT_STATUS_MODIFY_REJECTED,
)

LOGGER = logging.getLogger(__name__)

STATUS_BY_COUNTER_FLAG = {
    ORDER_STATUS_ALL_TRADED: OrderStatus.FILLED,
    ORDER_STATUS_PART_TRADED_QUEUEING: OrderStatus.PARTIALLY_FILLED,
    ORDER_STATUS_PART_TRADED_NOT_QUEUEING: OrderStatus.CANCELLED,
    ORDER_STATUS_NO_TRADE_QUEUEING: OrderStatus.ACCEPTED,
    ORDER_STATUS_NO_TRADE_NOT_QUEUEING: OrderStatus.CANCELLED,
    ORDER_STATUS_CANCELED: OrderStatus.CANCELLED,
    "a": OrderStatus.UNKNOWN,
    "b": OrderStatus.UNKNOWN,
    "c": OrderStatus.UNKNOWN,
}
OFFSET_BY_COUNTER_FLAG = {
    OFFSET_OPEN: Offset.OPEN,
    OFFSET_CLOSE: Offset.CLOSE,
    OFFSET_CLOSE_TODAY: Offset.CLOSE_TODAY,
    OFFSET_CLOSE_YESTERDAY: Offset.CLOSE_YESTERDAY,
}


class CtpNormalizationError(ValueError):
    """回报字段缺失或含义无法确定；调用方必须按死信处置，不能猜测替代值."""


def _text(raw: Mapping[str, Any], name: str, *, required: bool = True) -> str | None:
    value = raw.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise CtpNormalizationError(f"CTP callback is missing {name}")
        return None
    return str(value).strip()


def _integer(raw: Mapping[str, Any], name: str, *, required: bool = True, default: int = 0) -> int:
    value = raw.get(name)
    if value is None or value == "":
        if required:
            raise CtpNormalizationError(f"CTP callback is missing {name}")
        return default
    if isinstance(value, bool):
        raise CtpNormalizationError(f"{name} must be an integer, not a boolean")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise CtpNormalizationError(f"{name} is not an integer") from exc


def _decimal(raw: Mapping[str, Any], name: str, *, required: bool = True) -> Decimal | None:
    value = raw.get(name)
    if value is None or value == "":
        if required:
            raise CtpNormalizationError(f"CTP callback is missing {name}")
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CtpNormalizationError(f"{name} is not a decimal") from exc
    if not parsed.is_finite():
        raise CtpNormalizationError(f"{name} must be finite")
    return parsed


def parse_trading_day(raw: Mapping[str, Any]) -> date:
    """只接受柜台 ``TradingDay``（YYYYMMDD）；缺失或非法即明确失败."""
    text = _text(raw, "TradingDay")
    assert text is not None
    if len(text) != 8 or not text.isdigit():
        raise CtpNormalizationError("TradingDay must be the counter's YYYYMMDD trading day")
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError as exc:
        raise CtpNormalizationError("TradingDay is not a calendar date") from exc


def exchange_instant(
    day_text: str | None,
    time_text: str | None,
    received_at: datetime,
    *,
    tolerance: timedelta = timedelta(minutes=5),
) -> tuple[datetime, bool]:
    """柜台日期 + 时间 → UTC 时刻；与本地接收时刻明显不符时退回落接收时刻.

    返回 ``(时刻, 是否采用柜台时间)``。柜台在夜盘会把日期字段填成交易日，直接拼接会得到未来时刻，
    此时只保留可证明的接收时刻，并把异常计数留给联调核验 (FR-CAL-03)。
    """
    if not day_text or not time_text or len(day_text) != 8 or not day_text.isdigit():
        return received_at, False
    try:
        naive = datetime.strptime(f"{day_text} {time_text.strip()}", "%Y%m%d %H:%M:%S")
    except (TypeError, ValueError):
        return received_at, False
    candidate = naive.replace(tzinfo=EXCHANGE_TZ).astimezone(timezone.utc)
    if candidate > received_at + tolerance:
        return received_at, False
    return candidate, True


class CtpIdentityResolver:
    """把回调里不完整的标识补成可唯一归属的原会话三元组；有歧义就返回 ``None``（不猜）.

    只承认本网关自己分配过（或从已持久化发送结果恢复）的 ``OrderRef``：同一次会话内分配的唯一性由
    :class:`~qh_trader.gateway.ctp_gateway.CtpOrderRefBook` 保证，因此补全会话号和会话号不是猜测；
    同一个 ``OrderRef`` 对应多个会话时视为歧义并放弃归属。
    """

    def __init__(self, ref_book) -> None:  # noqa: ANN001 - 端口只依赖两个查询方法
        self._book = ref_book

    def session_for_ref(self, order_ref: str | None) -> tuple[int, int] | None:
        if not order_ref:
            return None
        matches = {key[:2] for key in self._book.snapshot() if key[2] == order_ref}
        if len(matches) != 1:
            return None
        return next(iter(matches))


class CtpFeedbackNormalizer(FeedbackNormalizerPort):
    """CTP 回报归一化；``resolver`` 用于补全缺少会话字段的回调 (S5-01 分配结果)."""

    def __init__(
        self,
        *,
        account_id: str,
        resolver: CtpIdentityResolver | None = None,
        source_id: str = "ctp",
        source_version: str = "ctp-6.7",
        timestamp_tolerance: timedelta = timedelta(minutes=5),
    ) -> None:
        if not account_id:
            raise ValueError("normalization requires the local account id")
        self.account_id = account_id
        self.source_id_value = source_id
        self.source_version = source_version
        self.resolver = resolver
        self.timestamp_tolerance = timestamp_tolerance
        self.counts = {
            "orders": 0,
            "trades": 0,
            "errors": 0,
            "timestamp_anomalies": 0,
            "unattributable": 0,
            "unrepresentable": 0,
        }
        self.gaps: list[Mapping[str, object]] = []

    # ------------------------------------------------------------------ 端口
    def source_id(self) -> str:
        return self.source_id_value

    def normalize_order(self, raw: Mapping[str, object], received_at: datetime) -> CanonicalEvent | None:
        if not isinstance(raw, Mapping):
            raise CtpNormalizationError("an CTP order callback must be a field mapping")
        self._require_aware(received_at)
        trading_day = parse_trading_day(raw)
        submit_status = _text(raw, "OrderSubmitStatus", required=False)
        status: OrderStatus
        if submit_status == SUBMIT_STATUS_INSERT_REJECTED:
            status = OrderStatus.REJECTED
        elif submit_status in (SUBMIT_STATUS_CANCEL_REJECTED, SUBMIT_STATUS_MODIFY_REJECTED):
            self._unrepresentable(
                "order_report", raw, f"counter submit status {submit_status} has no normalized representation yet"
            )
            return None
        else:
            flag = _text(raw, "OrderStatus")
            assert flag is not None
            resolved = STATUS_BY_COUNTER_FLAG.get(flag)
            if resolved is None:
                raise CtpNormalizationError(f"unknown CTP order status flag {flag!r}")
            status = resolved
        instrument = self._instrument(raw)
        side = self._side(raw)
        offset = self._offset(raw, "CombOffsetFlag")
        quantity = _integer(raw, "VolumeTotalOriginal")
        if quantity <= 0:
            raise CtpNormalizationError("a counter order report must carry a positive ordered quantity")
        filled = _integer(raw, "VolumeTraded", required=False)
        identity = self._identity(raw, instrument)
        if identity is None:
            self._unrepresentable("order_report", raw, "callback carries no uniquely attributable order identifier")
            return None
        event_time, from_counter = self._order_event_time(raw, received_at)
        event_id = ":".join(
            [
                self.source_id_value,
                "order",
                trading_day.isoformat(),
                instrument.exchange.value,
                identity.exchange_order_id or identity.order_ref or "unknown",
                status.value,
                str(filled),
                str(submit_status or ""),
            ]
        )
        update = OrderUpdate(
            identity=identity,
            instrument=instrument,
            side=side,
            offset=offset,
            status=status,
            quantity=quantity,
            filled_quantity=filled,
            event_time=event_time,
            available_at=received_at,
        )
        if filled > quantity:
            LOGGER.warning("counter reported filled quantity above the ordered quantity; the kernel will reconcile")
        if not from_counter:
            self._count("timestamp_anomalies")
        self._count("orders")
        return CanonicalEvent(
            event_id=event_id,
            kind=EventKind.ORDER_REPORT,
            event_time=update.event_time,
            available_at=update.available_at,
            sequence=0,
            source_id=self.source_id_value,
            payload=update,
        )

    def normalize_trade(self, raw: Mapping[str, object], received_at: datetime) -> CanonicalEvent | None:
        if not isinstance(raw, Mapping):
            raise CtpNormalizationError("an CTP trade callback must be a field mapping")
        self._require_aware(received_at)
        trading_day = parse_trading_day(raw)
        instrument = self._instrument(raw)
        side = self._side(raw)
        offset = self._offset(raw, "OffsetFlag")
        price = _decimal(raw, "Price")
        quantity = _integer(raw, "Volume")
        if quantity <= 0:
            raise CtpNormalizationError("a counter trade must carry a positive volume")
        assert price is not None
        if price <= 0:
            raise CtpNormalizationError("a counter trade price must be positive; zero is never read as a fill price")
        trade_id = _text(raw, "TradeID")
        assert trade_id is not None
        identity = self._identity(raw, instrument)
        event_time, from_counter = self._event_time(raw, "TradeDate", "TradeTime", received_at)
        trade = Trade(
            account_id=self.account_id,
            instrument=instrument,
            trading_day=trading_day,
            trade_id=trade_id,
            side=side,
            offset=offset,
            quantity=quantity,
            price=price,
            event_time=event_time,
            available_at=received_at,
            deduplication_key=TradeKey(
                account_id=self.account_id,
                exchange=instrument.exchange,
                trading_day=trading_day,
                trade_id=trade_id,
            ),
            order_identity=identity,
        )
        if not from_counter:
            self._count("timestamp_anomalies")
        self._count("trades")
        return CanonicalEvent(
            event_id=f"{self.source_id_value}:trade:{trading_day.isoformat()}:{instrument.exchange.value}:{trade_id}",
            kind=EventKind.TRADE_REPORT,
            event_time=trade.event_time,
            available_at=trade.available_at,
            sequence=0,
            source_id=self.source_id_value,
            payload=trade,
        )

    def normalize_error(self, raw: Mapping[str, object], received_at: datetime) -> CanonicalEvent | None:
        """拒单 / 撤单错误回报：能归属的转成 REJECTED 订单回报，其余写审计缺口."""
        if not isinstance(raw, Mapping):
            raise CtpNormalizationError("an CTP error callback must be a field mapping")
        self._require_aware(received_at)
        callback = str(raw.get("callback", "error"))
        info = raw.get("rsp")
        error_code = None
        error_message = None
        if isinstance(info, Mapping):
            error_code = None if info.get("ErrorID") in (None, "") else int(info["ErrorID"])
            error_message = None if info.get("ErrorMsg") in (None, "") else str(info["ErrorMsg"])
        if callback != "order_insert":
            self._unrepresentable(
                callback, raw, f"{callback} callback cannot be expressed as an order or trade report yet"
            )
            return None
        # OnErrRtnOrderInsert / OnRspOrderInsert: 交易所或柜台明确拒单，订单从未进入活动状态
        payload = dict(raw)
        payload.setdefault("VolumeTraded", 0)
        payload["OrderSubmitStatus"] = SUBMIT_STATUS_INSERT_REJECTED
        payload["__error_code"] = error_code
        payload["__error_message"] = error_message
        event = self.normalize_order(payload, received_at)
        self._count("errors")
        if event is None:
            return None
        return event

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _require_aware(received_at: datetime) -> None:
        if not isinstance(received_at, datetime) or received_at.tzinfo is None:
            raise CtpNormalizationError("callback receive time must be an aware timestamp")

    def _count(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1

    def _unrepresentable(self, callback: str, raw: Mapping[str, object], reason: str) -> None:
        """当前事件类型无法表达的回报：留审计证据并返回 None，绝不静默丢弃."""
        self._count("unrepresentable")
        self.gaps.append(
            {
                "callback": callback,
                "reason": reason,
                "instrument": raw.get("InstrumentID"),
                "order_sys_id": raw.get("OrderSysID"),
                "order_ref": raw.get("OrderRef"),
                "trade_id": raw.get("TradeID"),
            }
        )
        LOGGER.warning("CTP callback %s could not be represented: %s", callback, reason)

    def _instrument(self, raw: Mapping[str, Any]) -> InstrumentId:
        exchange_text = _text(raw, "ExchangeID")
        symbol = _text(raw, "InstrumentID")
        assert exchange_text is not None and symbol is not None
        try:
            exchange = Exchange(exchange_text)
        except ValueError as exc:
            raise CtpNormalizationError(f"unregistered exchange {exchange_text!r} in a callback") from exc
        return InstrumentId(exchange, symbol)

    @staticmethod
    def _side(raw: Mapping[str, Any]) -> Side:
        direction = _text(raw, "Direction")
        if direction == DIRECTION_BUY:
            return Side.BUY
        if direction == DIRECTION_SELL:
            return Side.SELL
        raise CtpNormalizationError(f"unknown CTP direction flag {direction!r}")

    @staticmethod
    def _offset(raw: Mapping[str, Any], field: str) -> Offset:
        text = _text(raw, field)
        assert text is not None
        flag = text[0]
        offset = OFFSET_BY_COUNTER_FLAG.get(flag)
        if offset is None:
            raise CtpNormalizationError(f"counter offset flag {flag!r} cannot be represented as a normalized offset")
        return offset

    def _identity(self, raw: Mapping[str, Any], instrument: InstrumentId) -> OrderIdentity | None:
        """可唯一归属的远端标识；一个都拿不到时返回 ``None``（不编造标识）."""
        exchange_order_id = _text(raw, "OrderSysID", required=False)
        front_id = raw.get("FrontID")
        session_id = raw.get("SessionID")
        order_ref = _text(raw, "OrderRef", required=False)
        triple: tuple[int, int, str] | None = None
        if front_id is not None and session_id is not None and order_ref:
            triple = (int(front_id), int(session_id), order_ref)
        elif self.resolver is not None and order_ref:
            resolved = self.resolver.session_for_ref(order_ref)
            if resolved is not None:
                triple = (resolved[0], resolved[1], order_ref)
        if exchange_order_id is None and triple is None:
            # 无法唯一归属：成交仍必须保留 (order_identity 留空，由内核放入待关联队列)；
            # 订单回报没有标识就无法表达，调用方按缺口记录并重新对账 (FR-ORD-05)。
            self._count("unattributable")
            return None
        return OrderIdentity(
            account_id=self.account_id,
            exchange=instrument.exchange,
            exchange_order_id=exchange_order_id,
            front_id=None if triple is None else triple[0],
            session_id=None if triple is None else triple[1],
            order_ref=None if triple is None else triple[2],
        )

    def _event_time(
        self, raw: Mapping[str, Any], day_field: str, time_field: str, received_at: datetime
    ) -> tuple[datetime, bool]:
        day_text = _text(raw, day_field, required=False)
        time_text = _text(raw, time_field, required=False)
        return exchange_instant(day_text, time_text, received_at, tolerance=self.timestamp_tolerance)

    def _order_event_time(self, raw: Mapping[str, Any], received_at: datetime) -> tuple[datetime, bool]:
        """报单时刻优先用 ``InsertDate + InsertTime``；只有插入时刻缺失才退到 ``UpdateTime``."""
        if _text(raw, "InsertTime", required=False):
            return self._event_time(raw, "InsertDate", "InsertTime", received_at)
        return self._event_time(raw, "InsertDate", "UpdateTime", received_at)


def build_normalizer(
    account_id: str,
    ref_book,  # noqa: ANN001 - CtpOrderRefBook，避免在类型上引入实现细节
    *,
    source_id: str = "ctp",
    source_version: str = "ctp-6.7",
) -> CtpFeedbackNormalizer:
    """工厂：装配侧用它把分配簿接到归一化器上."""
    return CtpFeedbackNormalizer(
        account_id=account_id,
        resolver=CtpIdentityResolver(ref_book),
        source_id=source_id,
        source_version=source_version,
    )
