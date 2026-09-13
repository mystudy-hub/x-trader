"""[Core 层] Port/Adapter 的结构类型边界；适配器由 scripts/ 装配注入。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from .constants import Exchange, MarketPhase, Offset, PriceType
from .event import CanonicalEvent, JournalTransaction, TimerEvent
from .objects import (
    AccountFunds,
    Bar,
    Capability,
    CapabilityProfile,
    CommissionRule,
    ContractSpec,
    ControlEpoch,
    ControlRecord,
    ExecutionReference,
    InstrumentId,
    LocalSendResult,
    MarginRule,
    OrderIdentity,
    OrderIntent,
    OrderUpdate,
    Position,
    ProductId,
    QueryBatch,
    QueryRateLimit,
    QueryResult,
    SeriesId,
    Session,
    Tick,
    Trade,
    VersionedValue,
)


@runtime_checkable
class ExecutionPort(Protocol):
    """仅接收已风控且已持久化的子单；回报经事件队列传递，本地返回不等于远端确认。"""

    def submit(self, order: OrderIntent, epoch: ControlEpoch) -> LocalSendResult: ...
    def cancel(self, ref: OrderIdentity, epoch: ControlEpoch) -> LocalSendResult: ...
    def capabilities(self) -> VersionedValue[CapabilityProfile]: ...


@runtime_checkable
class MarketDataPort(Protocol):
    """返回值必须满足 available_at <= 查询时钟；缺失执行价格返回 None，禁止替代或插值。"""

    def subscribe(self, instruments: Sequence[InstrumentId]) -> None: ...
    def bars(self, instrument: InstrumentId | SeriesId, interval: str, until: datetime) -> Sequence[Bar]: ...
    def execution_reference(
        self,
        instrument: InstrumentId,
        session_id: str,
        reference_time: datetime,
        price_type: PriceType,
        known_at: datetime,
    ) -> ExecutionReference | None: ...
    def latest_tick(self, instrument: InstrumentId, known_at: datetime) -> Tick | None: ...
    def instrument_status(self, instrument: InstrumentId, known_at: datetime) -> VersionedValue[MarketPhase] | None: ...


@runtime_checkable
class ClockPort(Protocol):
    def now(self) -> datetime: ...
    def schedule(self, at: datetime, event: TimerEvent) -> None: ...


@runtime_checkable
class JournalPort(Protocol):
    """append 必须原子提交事实去重、状态变更、预占和游标；失败通过异常交给调用方。"""

    def append(self, transaction: JournalTransaction) -> int: ...
    def snapshot(self, seq: int) -> None: ...
    def replay_from(self, seq: int) -> Iterator[CanonicalEvent]: ...
    def load_control_record(self) -> ControlRecord | None: ...


@runtime_checkable
class RuleStorePort(Protocol):
    """按业务生效时刻及允许获知时刻查询唯一规则；缺失或冲突分别抛出核心查询异常。"""

    def contract_rule(
        self,
        instrument: InstrumentId,
        profile: str,
        effective_at: datetime,
        known_at: datetime,
    ) -> VersionedValue[ContractSpec]: ...
    def commission_rule(
        self,
        instrument: InstrumentId,
        profile: str,
        offset: Offset,
        effective_at: datetime,
        known_at: datetime,
    ) -> VersionedValue[CommissionRule]: ...
    def margin_rule(
        self,
        instrument: InstrumentId,
        profile: str,
        effective_at: datetime,
        known_at: datetime,
    ) -> VersionedValue[MarginRule]: ...
    def capability(
        self,
        exchange: Exchange,
        product: ProductId,
        broker_profile: str,
        ctp_version: str,
        name: str,
        effective_at: datetime,
        known_at: datetime,
    ) -> VersionedValue[Capability] | None: ...
    def sessions(self, instrument: InstrumentId, trading_day: date, known_at: datetime) -> Sequence[Session]: ...


@runtime_checkable
class MappingStorePort(Protocol):
    """只返回已发布且当前可见的映射版本；短代码解析必须指定交易日和目录版本。"""

    def dominant(self, product: ProductId, at: datetime) -> VersionedValue[InstrumentId]: ...
    def resolve(self, raw_symbol: str, as_of: date, catalog_version: str) -> InstrumentId: ...
    def continuous_factors(self, product: ProductId, at: datetime) -> VersionedValue[Mapping[str, Decimal]]: ...


@runtime_checkable
class AccountQueryPort(Protocol):
    """查询批次和完成标志不可省略；空记录或半结算结果不能自行升级为一致账户快照。"""

    def query_account(self, batch: QueryBatch) -> QueryResult[AccountFunds]: ...
    def query_positions(self, batch: QueryBatch) -> QueryResult[Position]: ...
    def query_orders(self, batch: QueryBatch) -> QueryResult[OrderUpdate]: ...
    def query_trades(self, batch: QueryBatch) -> QueryResult[Trade]: ...
    def rate_limit(self) -> QueryRateLimit: ...
