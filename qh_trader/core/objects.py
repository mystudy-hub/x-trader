"""[Core 层] 不可变数据与命令契约；业务规则选择、数据库和柜台转换由上层实现。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from math import isfinite
from types import MappingProxyType
from typing import Generic, TypeVar

from .clock import utc_timestamp
from .constants import (
    Exchange,
    MarketPhase,
    Offset,
    OrderStatus,
    OrderType,
    PositionSide,
    PriceType,
    QualityFlag,
    SendState,
    SeriesKind,
    Side,
)

ROUNDING_MODES = frozenset(
    {
        "ROUND_05UP",
        "ROUND_CEILING",
        "ROUND_DOWN",
        "ROUND_FLOOR",
        "ROUND_HALF_DOWN",
        "ROUND_HALF_EVEN",
        "ROUND_HALF_UP",
        "ROUND_UP",
    }
)

T = TypeVar("T")


def require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")


def require_int(value: int, name: str, minimum: int | None = 0) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def require_decimal(value: Decimal, name: str, minimum: Decimal | None = None) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal; convert source values explicitly")
    if not value.is_finite() or (minimum is not None and value < minimum):
        raise ValueError(f"{name} must be finite and within its declared domain")


def require_date(value: date, name: str) -> None:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError(f"{name} must be a date, separate from physical timestamps")


def require_enum(value, enum_type) -> None:
    if not isinstance(value, enum_type):
        raise TypeError(f"expected {enum_type.__name__}, not a raw gateway value")


def require_bool(value, name: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be bool, not a truthy placeholder")


def require_instrument(value) -> None:
    if not isinstance(value, InstrumentId):
        raise TypeError("expected an actual InstrumentId")


def require_meta(value) -> None:
    if not isinstance(value, RecordMeta):
        raise TypeError("record requires normalized provenance")


def freeze_payload(value):
    """Snapshot containers and frozen dataclass fields; never retain a mutable source alias."""
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("payload mapping keys must be strings")
        return MappingProxyType({key: freeze_payload(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_payload(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(freeze_payload(item) for item in value)
    if isinstance(value, datetime):
        return utc_timestamp(value)
    if isinstance(value, Decimal):
        require_decimal(value, "payload decimal")
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("payload float must be finite")
        return value
    if value is None or isinstance(value, (str, bytes, bool, int, date, Enum)):
        return value
    parameters = getattr(type(value), "__dataclass_params__", None)
    if is_dataclass(value) and not isinstance(value, type) and parameters is not None and parameters.frozen:
        members = fields(value)
        if any(not member.init for member in members):
            raise TypeError("payload dataclasses must expose all fields in their value constructor")
        return replace(value, **{member.name: freeze_payload(getattr(value, member.name)) for member in members})
    raise TypeError("payload must contain normalized values, not mutable gateway objects")


def normalize_times(instance, *names):
    for name in names:
        object.__setattr__(instance, name, utc_timestamp(getattr(instance, name)))


@dataclass(frozen=True, slots=True)
class InstrumentId:
    exchange: Exchange
    symbol: str

    def __post_init__(self) -> None:
        require_enum(self.exchange, Exchange)
        require_text(self.symbol, "symbol")
        if any(char.isspace() or char in "/\\." for char in self.symbol):
            raise ValueError("symbol must be an atomic identifier; catalog resolution belongs to ContractResolver")

    def __str__(self) -> str:
        return f"{self.exchange.value}.{self.symbol}"


@dataclass(frozen=True, slots=True)
class ProductId:
    exchange: Exchange
    product: str

    def __post_init__(self) -> None:
        require_enum(self.exchange, Exchange)
        require_text(self.product, "product")


@dataclass(frozen=True, slots=True)
class SeriesId:
    name: str
    kind: SeriesKind

    def __post_init__(self) -> None:
        require_text(self.name, "series name")
        require_enum(self.kind, SeriesKind)


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordMeta:
    event_time: datetime
    available_at: datetime
    ingested_at: datetime
    trading_day: date
    source_id: str
    source_version: str
    ingest_seq: int
    session_id: str | None = None
    receive_time: datetime | None = None
    source_seq: int | None = None
    schema_version: int = 1
    quality_flags: QualityFlag = QualityFlag.OK

    def __post_init__(self) -> None:
        normalize_times(self, "event_time", "available_at", "ingested_at")
        if self.receive_time is not None:
            normalize_times(self, "receive_time")
        require_date(self.trading_day, "trading_day")
        require_text(self.source_id, "source_id")
        require_text(self.source_version, "source_version")
        require_int(self.ingest_seq, "ingest_seq")
        require_int(self.schema_version, "schema_version", 1)
        if self.source_seq is not None:
            require_int(self.source_seq, "source_seq")
        if self.session_id is not None:
            require_text(self.session_id, "session_id")
        require_enum(self.quality_flags, QualityFlag)

    def visible_at(self, at: datetime) -> bool:
        return self.available_at <= utc_timestamp(at)


@dataclass(frozen=True, kw_only=True)
class VersionedValue(Generic[T]):
    value: T
    source_id: str
    version: str
    effective_from: datetime
    available_at: datetime
    effective_to: datetime | None = None

    def __post_init__(self) -> None:
        require_text(self.source_id, "source_id")
        require_text(self.version, "version")
        normalize_times(self, "effective_from", "available_at")
        if self.effective_to is not None:
            normalize_times(self, "effective_to")
            if self.effective_to <= self.effective_from:
                raise ValueError("effective intervals must be nonempty and left-closed/right-open")
        object.__setattr__(self, "value", freeze_payload(self.value))

    def effective_at(self, at: datetime) -> bool:
        at = utc_timestamp(at)
        return self.effective_from <= at and (self.effective_to is None or at < self.effective_to)

    def visible_at(self, at: datetime) -> bool:
        return self.available_at <= utc_timestamp(at)


@dataclass(frozen=True, slots=True, kw_only=True)
class Bar:
    instrument: InstrumentId | SeriesId
    meta: RecordMeta
    bar_start: datetime
    bar_end: datetime
    interval: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    turnover: Decimal
    open_interest: int
    open_time: datetime
    includes_auction: bool

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, (InstrumentId, SeriesId)):
            raise TypeError("bar requires an instrument or explicitly typed series")
        require_meta(self.meta)
        require_bool(self.includes_auction, "includes_auction")
        normalize_times(self, "bar_start", "bar_end", "open_time")
        require_text(self.interval, "interval")
        if self.bar_start >= self.bar_end or self.open_time >= self.bar_end:
            raise ValueError("bar interval must be nonempty and its open must precede the end")
        if self.open_time < self.bar_start and not self.includes_auction:
            raise ValueError("an open before bar_start requires explicit auction inclusion")
        if self.meta.available_at < self.bar_end:
            raise ValueError("a final OHLC bar cannot be visible before its end")
        for name in ("open", "high", "low", "close"):
            require_decimal(getattr(self, name), name)
        require_decimal(self.turnover, "turnover", Decimal(0))
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("inconsistent OHLC range")
        require_int(self.volume, "volume")
        require_int(self.open_interest, "open_interest")


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionReference:
    instrument: InstrumentId
    meta: RecordMeta
    session_id: str
    reference_time: datetime
    price_type: PriceType
    price: Decimal
    source_record_id: str
    resolution: str
    available_volume: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, InstrumentId):
            raise TypeError("execution prices require an actual instrument")
        require_meta(self.meta)
        normalize_times(self, "reference_time")
        if self.meta.available_at < self.reference_time:
            raise ValueError("execution price cannot be visible before its reference time")
        require_enum(self.price_type, PriceType)
        require_decimal(self.price, "price")
        for name in ("session_id", "source_record_id", "resolution"):
            require_text(getattr(self, name), name)
        if self.available_volume is not None:
            require_int(self.available_volume, "available_volume")


@dataclass(frozen=True, slots=True, kw_only=True)
class Tick:
    instrument: InstrumentId
    meta: RecordMeta
    last_price: Decimal | None
    bid_price: Decimal | None
    ask_price: Decimal | None
    bid_volume: int | None
    ask_volume: int | None
    cumulative_volume: int
    cumulative_turnover: Decimal
    open_interest: int
    pre_settlement_price: Decimal | None
    upper_limit_price: Decimal | None
    lower_limit_price: Decimal | None
    phase: MarketPhase

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, InstrumentId):
            raise TypeError("tick requires an actual instrument")
        require_meta(self.meta)
        for name in (
            "last_price",
            "bid_price",
            "ask_price",
            "pre_settlement_price",
            "upper_limit_price",
            "lower_limit_price",
        ):
            if getattr(self, name) is not None:
                require_decimal(getattr(self, name), name)
        for name in ("cumulative_volume", "open_interest"):
            require_int(getattr(self, name), name)
        for name in ("bid_volume", "ask_volume"):
            if getattr(self, name) is not None:
                require_int(getattr(self, name), name)
        require_decimal(self.cumulative_turnover, "cumulative_turnover", Decimal(0))
        require_enum(self.phase, MarketPhase)


@dataclass(frozen=True, slots=True, kw_only=True)
class Settlement:
    instrument: InstrumentId
    meta: RecordMeta
    settlement_price: Decimal
    pre_settlement_price: Decimal | None
    published_at: datetime
    is_final: bool

    def __post_init__(self) -> None:
        require_instrument(self.instrument)
        require_meta(self.meta)
        normalize_times(self, "published_at")
        require_bool(self.is_final, "is_final")
        require_decimal(self.settlement_price, "settlement_price")
        if self.pre_settlement_price is not None:
            require_decimal(self.pre_settlement_price, "pre_settlement_price")
        if self.meta.available_at < self.published_at:
            raise ValueError("settlement cannot be visible before publication")


@dataclass(frozen=True, slots=True)
class Permissions:
    submit: bool
    cancel: bool
    match: bool

    def __post_init__(self) -> None:
        for name in ("submit", "cancel", "match"):
            require_bool(getattr(self, name), name)


@dataclass(frozen=True, slots=True, kw_only=True)
class Session:
    instrument: InstrumentId
    session_id: str
    trading_day: date
    start: datetime
    end: datetime
    phase: MarketPhase
    permissions: Permissions
    rule_version: str
    source_id: str
    available_at: datetime

    def __post_init__(self) -> None:
        require_instrument(self.instrument)
        if not isinstance(self.permissions, Permissions):
            raise TypeError("session requires explicit Permissions")
        normalize_times(self, "start", "end", "available_at")
        require_date(self.trading_day, "trading_day")
        require_enum(self.phase, MarketPhase)
        for name in ("session_id", "rule_version", "source_id"):
            require_text(getattr(self, name), name)
        if self.start >= self.end:
            raise ValueError("session interval must be nonempty")
        if self.phase == MarketPhase.UNKNOWN and self.permissions != Permissions(False, False, False):
            raise ValueError("unknown sessions cannot grant trading permissions")

    def contains(self, at: datetime) -> bool:
        return self.start <= utc_timestamp(at) < self.end


@dataclass(frozen=True, slots=True)
class ControlEpoch:
    controller_id: str
    epoch: int

    def __post_init__(self) -> None:
        require_text(self.controller_id, "controller_id")
        require_int(self.epoch, "epoch")


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderIdentity:
    account_id: str
    exchange: Exchange
    client_order_id: str | None = None
    exchange_order_id: str | None = None
    front_id: int | None = None
    session_id: int | None = None
    order_ref: str | None = None

    def __post_init__(self) -> None:
        require_text(self.account_id, "account_id")
        require_enum(self.exchange, Exchange)
        for name in ("client_order_id", "exchange_order_id", "order_ref"):
            if getattr(self, name) is not None:
                require_text(getattr(self, name), name)
        for name in ("front_id", "session_id"):
            if getattr(self, name) is not None:
                require_int(getattr(self, name), name)
        original = (self.front_id, self.session_id, self.order_ref)
        if any(part is not None for part in original) and any(part is None for part in original):
            raise ValueError("original session identity must include front_id, session_id and order_ref")
        if (
            not self.client_order_id
            and not self.exchange_order_id
            and not (self.front_id is not None and self.session_id is not None and self.order_ref)
        ):
            raise ValueError("order identity needs a local ID, exchange ID, or complete original session tuple")


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderIntent:
    client_order_id: str
    account_id: str
    strategy_id: str
    instrument: InstrumentId
    side: Side
    offset: Offset
    quantity: int
    order_type: OrderType
    created_at: datetime
    limit_price_ticks: int | None = None
    parent_order_id: str | None = None
    mapping_version: str | None = None

    def __post_init__(self) -> None:
        for name in ("client_order_id", "account_id", "strategy_id"):
            require_text(getattr(self, name), name)
        for name in ("parent_order_id", "mapping_version"):
            if getattr(self, name) is not None:
                require_text(getattr(self, name), name)
        if not isinstance(self.instrument, InstrumentId):
            raise TypeError("final order intents require an actual contract, not a product or series")
        require_enum(self.side, Side)
        require_enum(self.offset, Offset)
        require_enum(self.order_type, OrderType)
        require_int(self.quantity, "quantity", 1)
        normalize_times(self, "created_at")
        if self.order_type == OrderType.LIMIT:
            if isinstance(self.limit_price_ticks, bool) or not isinstance(self.limit_price_ticks, int):
                raise TypeError("limit order price must be integer ticks")
        elif self.limit_price_ticks is not None:
            raise ValueError("market order cannot carry a limit price")


@dataclass(frozen=True, slots=True)
class LocalSendResult:
    """本地调用结果；``remote_identity`` 是本地已分配的柜台标识，不是远端确认。

    适配器在调用真实接口前就决定了本会话的 (front_id, session_id, order_ref) 三元组，
    把它随发送结果一起持久化，重启后仍能按原会话三元组归属迟到的订单 / 成交回报
    (FR-ORD-05, FR-REC-02)。远程是否受理仍由回报决定，因此字段与状态互不替代。
    """

    state: SendState
    local_code: int | None
    evidence: str
    remote_identity: OrderIdentity | None = None

    def __post_init__(self) -> None:
        require_enum(self.state, SendState)
        require_text(self.evidence, "send-result evidence")
        if self.local_code is not None:
            require_int(self.local_code, "local_code", None)
        if self.state == SendState.CONFIRMED_REMOTE:
            raise ValueError("a local call result cannot itself confirm remote processing")
        if self.remote_identity is not None and not isinstance(self.remote_identity, OrderIdentity):
            raise TypeError("a locally assigned remote identity must be an OrderIdentity")


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderUpdate:
    identity: OrderIdentity
    instrument: InstrumentId
    side: Side
    offset: Offset
    status: OrderStatus
    quantity: int
    filled_quantity: int
    event_time: datetime
    available_at: datetime

    def __post_init__(self) -> None:
        require_instrument(self.instrument)
        if not isinstance(self.identity, OrderIdentity) or self.identity.exchange != self.instrument.exchange:
            raise ValueError("order identity must match the instrument exchange")
        require_enum(self.side, Side)
        require_enum(self.offset, Offset)
        require_enum(self.status, OrderStatus)
        require_int(self.quantity, "quantity", 1)
        require_int(self.filled_quantity, "filled_quantity")
        if self.filled_quantity > self.quantity:
            raise ValueError("reported filled quantity exceeds the original quantity")
        normalize_times(self, "event_time", "available_at")


@dataclass(frozen=True, slots=True)
class TradeKey:
    account_id: str
    exchange: Exchange
    trading_day: date
    trade_id: str
    extra_scope: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_text(self.account_id, "account_id")
        require_enum(self.exchange, Exchange)
        require_date(self.trading_day, "trading_day")
        require_text(self.trade_id, "trade_id")
        if isinstance(self.extra_scope, str):
            raise TypeError("extra_scope must be a sequence of identity fields")
        scope = tuple(self.extra_scope)
        for part in scope:
            require_text(part, "broker identity field")
        object.__setattr__(self, "extra_scope", scope)


@dataclass(frozen=True, slots=True, kw_only=True)
class Trade:
    account_id: str
    instrument: InstrumentId
    trading_day: date
    trade_id: str
    side: Side
    offset: Offset
    quantity: int
    price: Decimal
    event_time: datetime
    available_at: datetime
    deduplication_key: TradeKey
    order_identity: OrderIdentity | None = None

    def __post_init__(self) -> None:
        require_instrument(self.instrument)
        require_text(self.account_id, "account_id")
        require_text(self.trade_id, "trade_id")
        require_date(self.trading_day, "trading_day")
        require_enum(self.side, Side)
        require_enum(self.offset, Offset)
        require_int(self.quantity, "quantity", 1)
        require_decimal(self.price, "price")
        normalize_times(self, "event_time", "available_at")
        key = self.deduplication_key
        if not isinstance(key, TradeKey) or (key.account_id, key.exchange, key.trading_day, key.trade_id) != (
            self.account_id,
            self.instrument.exchange,
            self.trading_day,
            self.trade_id,
        ):
            raise ValueError("trade identity and adapter-supplied deduplication scope must agree")
        if self.order_identity is not None and (
            not isinstance(self.order_identity, OrderIdentity)
            or self.order_identity.account_id != self.account_id
            or self.order_identity.exchange != self.instrument.exchange
        ):
            raise ValueError("trade order identity must match the account and exchange")


@dataclass(frozen=True, slots=True, kw_only=True)
class Position:
    instrument: InstrumentId
    side: PositionSide
    hedge_flag: str
    pos_yd: int
    pos_td: int
    frozen_yd: int
    frozen_td: int

    def __post_init__(self) -> None:
        require_instrument(self.instrument)
        require_enum(self.side, PositionSide)
        require_text(self.hedge_flag, "hedge_flag")
        for name in ("pos_yd", "pos_td", "frozen_yd", "frozen_td"):
            require_int(getattr(self, name), name)
        if self.frozen_yd > self.pos_yd or self.frozen_td > self.pos_td:
            raise ValueError("frozen quantities cannot exceed their matching position buckets")


@dataclass(frozen=True, slots=True, kw_only=True)
class ContractSpec:
    instrument: InstrumentId
    product: ProductId
    delivery_year: int
    delivery_month: int
    multiplier: Decimal
    price_tick: Decimal
    listed_on: date
    last_trading_day: date

    def __post_init__(self) -> None:
        require_instrument(self.instrument)
        if not isinstance(self.product, ProductId) or self.product.exchange != self.instrument.exchange:
            raise ValueError("contract and product exchanges must agree")
        require_int(self.delivery_year, "delivery_year", 1000)
        if self.delivery_year > 9999:
            raise ValueError("delivery_year must be a full four-digit year")
        require_int(self.delivery_month, "delivery_month", 1)
        if not 1 <= self.delivery_month <= 12:
            raise ValueError("delivery_month must be 1..12")
        require_decimal(self.multiplier, "multiplier")
        require_decimal(self.price_tick, "price_tick")
        if self.multiplier <= 0 or self.price_tick <= 0:
            raise ValueError("contract multiplier and price tick must be positive")
        require_date(self.listed_on, "listed_on")
        require_date(self.last_trading_day, "last_trading_day")
        if self.listed_on > self.last_trading_day:
            raise ValueError("contract listing starts after its last trading day")


@dataclass(frozen=True, slots=True)
class CommissionRule:
    per_lot: Decimal
    ad_valorem: Decimal
    currency_unit: Decimal
    rounding: str

    def __post_init__(self) -> None:
        require_decimal(self.per_lot, "per_lot", Decimal(0))
        require_decimal(self.ad_valorem, "ad_valorem", Decimal(0))
        require_decimal(self.currency_unit, "currency_unit")
        if self.currency_unit <= 0:
            raise ValueError("currency_unit must be positive")
        require_text(self.rounding, "rounding")
        if self.rounding not in ROUNDING_MODES:
            raise ValueError("rounding must name an explicit Decimal rounding mode")


@dataclass(frozen=True, slots=True)
class MarginRule:
    ratio: Decimal
    per_lot: Decimal

    def __post_init__(self) -> None:
        require_decimal(self.ratio, "ratio", Decimal(0))
        require_decimal(self.per_lot, "per_lot", Decimal(0))


@dataclass(frozen=True, slots=True)
class Capability:
    value: bool | int | str | Decimal | None
    verified: bool
    evidence_ref: str | None = None

    def __post_init__(self) -> None:
        require_bool(self.verified, "verified")
        if self.value is not None and not isinstance(self.value, (bool, int, str, Decimal)):
            raise TypeError("capability must contain an explicit normalized scalar or None")
        if isinstance(self.value, Decimal):
            require_decimal(self.value, "capability value")
        if isinstance(self.value, str):
            require_text(self.value, "capability value")
        if self.evidence_ref is not None:
            require_text(self.evidence_ref, "capability evidence")
        if self.verified and (self.value is None or not self.evidence_ref):
            raise ValueError("verified capability needs an explicit value and evidence")
        if not self.verified and self.value is not None:
            raise ValueError("unknown capability must not expose a default value")

    def __bool__(self) -> bool:
        raise TypeError("inspect capability.verified and capability.value explicitly")


@dataclass(frozen=True, slots=True)
class CapabilityProfile:
    profile_id: str
    ctp_version: str | None
    values: Mapping[str, Capability]

    def __post_init__(self) -> None:
        require_text(self.profile_id, "profile_id")
        if self.ctp_version is not None:
            require_text(self.ctp_version, "ctp_version")
        if not isinstance(self.values, Mapping):
            raise TypeError("capability values must be a named mapping")
        if any(not isinstance(value, Capability) for value in self.values.values()):
            raise TypeError("capabilities must explicitly distinguish verified values from unknown values")
        object.__setattr__(self, "values", freeze_payload(self.values))


@dataclass(frozen=True, slots=True)
class ControlRecord:
    epoch: ControlEpoch
    acquired_at: datetime
    journal_seq: int

    def __post_init__(self) -> None:
        if not isinstance(self.epoch, ControlEpoch):
            raise TypeError("control record requires a controller and epoch")
        normalize_times(self, "acquired_at")
        require_int(self.journal_seq, "journal_seq")


@dataclass(frozen=True, slots=True)
class QueryBatch:
    batch_id: str
    account_id: str
    trading_day: date
    requested_at: datetime

    def __post_init__(self) -> None:
        require_text(self.batch_id, "batch_id")
        require_text(self.account_id, "account_id")
        require_date(self.trading_day, "trading_day")
        normalize_times(self, "requested_at")


@dataclass(frozen=True, kw_only=True)
class QueryResult(Generic[T]):
    """complete means the query stream ended; account consistency remains a separate check."""

    batch: QueryBatch
    records: tuple[T, ...]
    available_at: datetime
    source_id: str
    source_version: str
    complete: bool = False
    error_code: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.batch, QueryBatch):
            raise TypeError("query result requires its account, trading day and request batch")
        require_bool(self.complete, "complete")
        if self.error_code is not None:
            require_int(self.error_code, "error_code", None)
        normalize_times(self, "available_at")
        require_text(self.source_id, "source_id")
        require_text(self.source_version, "source_version")
        object.__setattr__(self, "records", tuple(freeze_payload(item) for item in self.records))


@dataclass(frozen=True, slots=True)
class AccountFunds:
    balance: Decimal | None
    equity: Decimal | None
    margin: Decimal | None
    available_for_new_trades: Decimal | None

    def __post_init__(self) -> None:
        for name in ("balance", "equity", "margin", "available_for_new_trades"):
            if getattr(self, name) is not None:
                require_decimal(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class QueryRateLimit:
    interval_ms: int
    max_in_flight: int

    def __post_init__(self) -> None:
        require_int(self.interval_ms, "interval_ms")
        require_int(self.max_in_flight, "max_in_flight", 1)
