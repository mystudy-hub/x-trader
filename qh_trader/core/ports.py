"""[Core 层] Port/Adapter 的结构类型边界；适配器由 scripts/ 装配注入。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from .constants import Exchange, MarketPhase, Offset, PriceType
from .event import CanonicalEvent, JournalSnapshot, JournalTransaction, TimerEvent
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
    TradeKey,
    VersionedValue,
)


@runtime_checkable
class ExecutionPort(Protocol):
    """仅接收已风控且已持久化的子单；回报经事件队列传递，本地返回不等于远端确认。"""

    def submit(self, order: OrderIntent, epoch: ControlEpoch) -> LocalSendResult: ...
    def cancel(self, ref: OrderIdentity, epoch: ControlEpoch) -> LocalSendResult: ...
    def capabilities(self) -> VersionedValue[CapabilityProfile]: ...


@runtime_checkable
class FeedbackNormalizerPort(Protocol):
    """回报归一化契约 (FR-ORD-05, S2-07)。

    适配器把柜台原始回调 (OnRspOrderInsert / OnErrRtnOrderInsert / OnRtnOrder / OnRtnTrade / 撤单错误)
    转换为 CanonicalEvent[OrderUpdate] / CanonicalEvent[Trade]：
    - 只做字段校验、标识关联与去重键构造，不做任何记账；
    - Trade.deduplication_key 的作用域由适配器按柜台编号真实唯一性定义 (FR-REC-02)；
    - Trade.order_identity 只填入可唯一归属的远端标识 (ExchangeID+OrderSysID 或完整原会话三元组)，
      不得补上当前会话号猜测关联；无法归属时留空，由领域内核放入待关联队列；
    - 无法解析的原始回报返回 None 并由适配器记录证据，不能静默丢弃真实成交。
    """

    def normalize_order(self, raw: Mapping[str, object], received_at: datetime) -> CanonicalEvent | None: ...
    def normalize_trade(self, raw: Mapping[str, object], received_at: datetime) -> CanonicalEvent | None: ...
    def normalize_error(self, raw: Mapping[str, object], received_at: datetime) -> CanonicalEvent | None: ...
    def source_id(self) -> str: ...


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
    def load_checkpoint(self) -> JournalSnapshot: ...
    def load_snapshot(self, seq: int | None = None) -> JournalSnapshot | None: ...
    def contains_trade(self, key: TradeKey) -> bool: ...


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
