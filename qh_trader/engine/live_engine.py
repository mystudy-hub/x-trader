"""[Engine 层] 已闭合 Bar 驱动的策略命令生产者 (S5-05, FR-ORD-08, FR-REC-02).

账户执行服务仍是唯一交易出口；持仓只读，报价/拆单显式注入，游标先于信号持久化。
崩溃可能丢失本根信号，但绝不自动重发未知结果。同一策略游标须由一个生产者独占。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Protocol

from qh_trader.core.clock import utc_timestamp
from qh_trader.core.constants import MarketPhase, Offset, OrderStatus, OrderType, QualityFlag, Side
from qh_trader.core.event import TimerEvent
from qh_trader.core.execution import CommandKind, CommandStatus, ExecutionCommand, QueuedCommand
from qh_trader.core.objects import (
    Bar,
    ControlEpoch,
    InstrumentId,
    OrderIdentity,
    OrderIntent,
    OrderUpdate,
    Tick,
    Trade,
    TradeKey,
    require_decimal,
    require_int,
    require_text,
)
from qh_trader.core.ports import ClockPort, StrategyPort


class StrategyCommandClient(Protocol):
    """命令表的生产者接口，无网关操作权。"""

    def get(self, command_id: str) -> QueuedCommand | None: ...
    def submit(self, command: ExecutionCommand) -> QueuedCommand: ...


class StrategyAccountView(Protocol):
    """由装配层提供的已提交账户视图，查询不得推算或修改持仓。"""

    def get_position(self, instrument: InstrumentId) -> int: ...
    def is_order_active(self, client_order_id: str) -> bool: ...
    def order_identity(self, client_order_id: str) -> OrderIdentity | None: ...
    def has_active_orders(self, instrument: InstrumentId) -> bool: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class StrategyCheckpoint:
    """每个策略配置独占持久化游标；processing=True 必须核对后恢复。"""

    stream_id: str
    bar_end: datetime | None
    processing: bool
    pending_command_ids: tuple[str, ...] = ()
    tick_time: datetime | None = None
    tick_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_text(self.stream_id, "stream_id")
        if self.bar_end is not None:
            object.__setattr__(self, "bar_end", utc_timestamp(self.bar_end))
        if self.tick_time is not None:
            object.__setattr__(self, "tick_time", utc_timestamp(self.tick_time))
        if self.bar_end is None and self.tick_time is None:
            raise ValueError("checkpoint needs a bar or tick cursor")
        if bool(self.tick_keys) != (self.tick_time is not None):
            raise ValueError("tick cursor requires keys at its timestamp")
        object.__setattr__(self, "tick_keys", tuple(self.tick_keys))
        if not isinstance(self.processing, bool):
            raise TypeError("processing must be bool")
        ids = tuple(self.pending_command_ids)
        for command_id in ids:
            require_text(command_id, "pending command_id")
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate pending command IDs")
        object.__setattr__(self, "pending_command_ids", ids)


class StrategyCheckpointStore(Protocol):
    """save 必须原子持久化；同一 stream_id 必须由一个生产者独占。"""

    def load(self, stream_id: str) -> StrategyCheckpoint | None: ...
    def save(self, checkpoint: StrategyCheckpoint) -> None: ...


@dataclass(frozen=True, slots=True)
class StrategyBarResult:
    """保留原始意图、最终命令和停止提交的原因，供审计输出。"""

    bar_end: datetime
    reason: str
    intents: tuple[OrderIntent, ...] = ()
    commands: tuple[ExecutionCommand, ...] = ()


class LiveEngine:
    """单策略、单合约、单周期的消费者，同时实现 StrategyContextPort。

    order_translator 明确实现价格与今昨仓转换，不能由引擎猜测。
    ready_provider 必须同时检查执行服务、行情连接和账户对账就绪状态。
    max_bar_age 从 bar_end 计算，历史回放不能冒充实时行情。
    """

    def __init__(
        self,
        *,
        account_id: str,
        producer_id: str,
        strategy_id: str,
        config_version: str,
        instrument: InstrumentId,
        interval: str,
        command_client: StrategyCommandClient,
        account_view: StrategyAccountView,
        checkpoint_store: StrategyCheckpointStore,
        clock: ClockPort,
        control_provider: Callable[[], ControlEpoch | None],
        ready_provider: Callable[[], bool],
        order_translator: Callable[[OrderIntent], Sequence[OrderIntent]],
        max_bar_age: timedelta,
        min_warmup_bars: int = 0,
        equity_provider: Callable[[], Decimal] | None = None,
        max_tick_age: timedelta = timedelta(seconds=5),
    ) -> None:
        for name, value in (
            ("account_id", account_id),
            ("producer_id", producer_id),
            ("strategy_id", strategy_id),
            ("config_version", config_version),
            ("interval", interval),
        ):
            require_text(value, name)
        if not isinstance(instrument, InstrumentId):
            raise TypeError("live strategy requires an actual instrument")
        if max_bar_age <= timedelta(0) or max_tick_age <= timedelta(0):
            raise ValueError("market data maximum age must be positive")
        require_int(min_warmup_bars, "min_warmup_bars")
        self.account_id, self.producer_id, self.strategy_id = account_id, producer_id, strategy_id
        self.instrument, self.interval = instrument, interval
        parts = (account_id, producer_id, strategy_id, config_version, str(instrument), interval)
        self.stream_id = sha256(repr(parts).encode()).hexdigest()
        self._client, self._account, self._store = command_client, account_view, checkpoint_store
        self._clock, self._control, self._ready = clock, control_provider, ready_provider
        self._translate, self._equity = order_translator, equity_provider
        self._max_bar_age, self._min_warmup = max_bar_age, min_warmup_bars
        self._max_tick_age = max_tick_age
        self._checkpoint = checkpoint_store.load(self.stream_id)
        if self._checkpoint is not None and self._checkpoint.stream_id != self.stream_id:
            raise ValueError("checkpoint belongs to another strategy stream")
        self._pending = list(self._checkpoint.pending_command_ids) if self._checkpoint else []
        self._strategy: StrategyPort | None = None
        self._running = False
        self._fault: str | None = None
        self._warmup_count = 0
        self._last_warmup: datetime | None = None
        self._current_bar: Bar | None = None
        self._current_tick: Tick | None = None
        self._submission_reason = "outside_bar"
        self._ordinal = 0
        self._intents: list[OrderIntent] = []
        self._commands: list[ExecutionCommand] = []
        self._children: dict[str, tuple[str, ...]] = {}
        self._seen_trades: set[TradeKey] = set()

    @property
    def checkpoint(self) -> StrategyCheckpoint | None:
        return self._checkpoint

    @property
    def fault(self) -> str | None:
        return self._fault

    def attach_strategy(self, strategy: StrategyPort) -> None:
        if self._strategy is not None or strategy.strategy_id != self.strategy_id:
            raise ValueError("exactly one strategy with the configured strategy_id is required")
        self._strategy = strategy
        strategy.on_init()

    def _require_strategy(self) -> StrategyPort:
        if self._strategy is None:
            raise RuntimeError("attach a strategy before consuming bars")
        return self._strategy

    def _validate_bar(self, bar: Bar) -> None:
        if bar.meta.quality_flags != QualityFlag.OK:
            raise ValueError("live strategy requires verified market data quality")
        if bar.instrument != self.instrument or bar.interval != self.interval:
            raise ValueError("bar belongs to another instrument or interval")
        if max(bar.bar_end, bar.meta.available_at) > self.now():
            raise ValueError("strategy cannot consume an unfinished or unavailable bar")

    def warmup(self, bars: Iterable[Bar]) -> None:
        """顺序预热；下单/撤单均被抑制，不推进持久化交易游标。"""
        if self._running or self._fault is not None:
            raise RuntimeError("warmup requires a stopped, healthy engine")
        strategy = self._require_strategy()
        for bar in bars:
            self._validate_bar(bar)
            if self._last_warmup is not None and bar.bar_end <= self._last_warmup:
                raise ValueError("warmup bars must be strictly ordered")
            self._current_bar, self._submission_reason, self._ordinal = bar, "warmup", 0
            try:
                strategy.on_bar(bar)
            except Exception as exc:
                self._fault = f"warmup failed: {exc}"
                raise
            finally:
                self._current_bar, self._submission_reason = None, "outside_bar"
            self._last_warmup = bar.bar_end
            self._warmup_count += 1

    def start(self) -> None:
        strategy = self._require_strategy()
        if self._running:
            raise RuntimeError("strategy is already running")
        if self._fault or (self._checkpoint is not None and self._checkpoint.processing):
            raise RuntimeError("incomplete strategy processing requires reconciliation before restart")
        if self._warmup_count < self._min_warmup:
            raise RuntimeError("not enough warmup bars")
        if (
            self._checkpoint is not None
            and self._checkpoint.bar_end is not None
            and (self._last_warmup is None or self._last_warmup < self._checkpoint.bar_end)
        ):
            raise RuntimeError("restart must replay warmup through the persisted bar cursor")
        if (
            self._account.get_position(self.instrument) != 0
            or self._account.has_active_orders(self.instrument)
            or self._pending_commands()
        ):
            raise RuntimeError("restart with exposure or pending orders requires strategy state reconciliation")
        strategy.on_start()
        self._running = True

    def stop(self) -> None:
        self._running = False
        self._require_strategy().on_stop()

    def _pending_commands(self) -> bool:
        retained = []
        for command_id in self._pending:
            queued = self._client.get(command_id)
            if queued is None:
                raise RuntimeError("persisted strategy command is missing from the execution queue")
            payload = queued.command.payload
            active = queued.status in (CommandStatus.PENDING, CommandStatus.DISPATCHING)
            if isinstance(payload, OrderIntent):
                if queued.status in (CommandStatus.NOT_SENT, CommandStatus.REJECTED, CommandStatus.REJECTED_STALE):
                    self._notify_local_rejection(payload, self._signal_id(payload.client_order_id))
                active |= self._account.is_order_active(payload.client_order_id)
                active |= (
                    queued.status == CommandStatus.SENT_UNKNOWN
                    and self._account.order_identity(payload.client_order_id) is None
                )
                parent_id = self._signal_id(payload.client_order_id)
                children = self._children.get(parent_id, ())
                self._children[parent_id] = tuple(dict.fromkeys((*children, payload.client_order_id)))
            if active:
                retained.append(command_id)
        self._pending = retained
        return bool(retained)

    def _gate(self, bar: Bar) -> str:
        if not self._ready() or self._control() is None:
            return "not_ready"
        if self.now() - bar.bar_end > self._max_bar_age:
            return "stale_bar"
        if self._pending_commands() or self._account.has_active_orders(self.instrument):
            return "pending_orders"
        return "processed"

    @staticmethod
    def _tick_key(tick: Tick) -> str:
        values = (
            str(tick.instrument),
            tick.meta.source_id,
            tick.meta.event_time.isoformat(),
            tick.last_price,
            tick.bid_price,
            tick.ask_price,
            tick.bid_volume,
            tick.ask_volume,
            tick.cumulative_volume,
            tick.cumulative_turnover,
        )
        return sha256(repr(values).encode()).hexdigest()

    def _save(self, event: Bar | Tick, *, processing: bool) -> None:
        old = self._checkpoint
        bar_end = old.bar_end if old else None
        tick_time = old.tick_time if old else None
        tick_keys = old.tick_keys if old else ()
        if isinstance(event, Bar):
            bar_end = event.bar_end
        else:
            key = self._tick_key(event)
            tick_keys = tuple(dict.fromkeys((*tick_keys, key))) if tick_time == event.meta.event_time else (key,)
            tick_time = event.meta.event_time
        checkpoint = StrategyCheckpoint(
            stream_id=self.stream_id,
            bar_end=bar_end,
            processing=processing,
            pending_command_ids=tuple(self._pending),
            tick_time=tick_time,
            tick_keys=tick_keys,
        )
        self._store.save(checkpoint)
        self._checkpoint = checkpoint

    def on_bar(self, bar: Bar) -> StrategyBarResult:
        """阻断的新 Bar 仍更新指标；重复或乱序 Bar 不再进入策略。"""
        if not self._running or self._fault:
            raise RuntimeError("strategy is not running or has faulted")
        self._validate_bar(bar)
        cursor = self._checkpoint.bar_end if self._checkpoint else None
        watermark = max(value for value in (cursor, self._last_warmup, bar.bar_start) if value is not None)
        if bar.bar_end <= watermark:
            return StrategyBarResult(bar.bar_end, "duplicate_or_out_of_order")
        self._intents, self._commands, self._ordinal = [], [], 0
        try:
            reason = self._gate(bar)
            self._save(bar, processing=True)
            self._current_bar, self._submission_reason = bar, reason
            self._require_strategy().on_bar(bar)
            self._reject_suppressed_intents()
            self._save(bar, processing=False)
            return StrategyBarResult(bar.bar_end, reason, tuple(self._intents), tuple(self._commands))
        except Exception as exc:
            self._fault = f"bar processing failed: {exc}"
            self._running = False
            raise
        finally:
            self._current_bar, self._submission_reason = None, "outside_bar"

    def on_tick(self, tick: Tick) -> StrategyBarResult:
        """可选实时止损回调；只接受减仓意图，并持久化同时间戳内的快照去重键。"""
        if not self._running or self._fault:
            raise RuntimeError("strategy is not running or has faulted")
        if tick.instrument != self.instrument:
            raise ValueError("tick belongs to another instrument")
        if tick.meta.quality_flags != QualityFlag.OK:
            raise ValueError("live strategy requires verified market data quality")
        at = tick.meta.event_time
        if max(at, tick.meta.available_at) > self.now():
            raise ValueError("strategy cannot consume an unavailable tick")
        callback = getattr(self._require_strategy(), "on_tick", None)
        if callback is None:
            return StrategyBarResult(at, "no_tick_callback")
        old = self._checkpoint
        if (
            old is not None
            and old.tick_time is not None
            and (at < old.tick_time or (at == old.tick_time and self._tick_key(tick) in old.tick_keys))
        ):
            return StrategyBarResult(at, "duplicate_or_out_of_order")
        self._intents, self._commands, self._ordinal = [], [], 0
        try:
            if not self._ready() or self._control() is None:
                reason = "not_ready"
            elif self.now() - at > self._max_tick_age:
                reason = "stale_tick"
            elif tick.phase != MarketPhase.CONTINUOUS:
                reason = "market_not_continuous"
            elif self._pending_commands() or self._account.has_active_orders(self.instrument):
                reason = "pending_orders"
            else:
                reason = "processed"
            self._save(tick, processing=True)
            self._current_tick, self._submission_reason = tick, reason
            callback(tick)
            self._reject_suppressed_intents()
            self._save(tick, processing=False)
            return StrategyBarResult(at, reason, tuple(self._intents), tuple(self._commands))
        except Exception as exc:
            self._fault = f"tick processing failed: {exc}"
            self._running = False
            raise
        finally:
            self._current_tick, self._submission_reason = None, "outside_bar"

    def _reject_suppressed_intents(self) -> None:
        """调用方已保存返回的父 ID 后才通知本地拒绝，避免策略留下虚假未决状态。

        这是生产者本地拒绝通知，不是柜台事实，绝不进入账户日志或账本。
        """
        if self._submission_reason == "processed":
            return
        for intent in tuple(self._intents):
            self._notify_local_rejection(intent, intent.client_order_id)

    def _notify_local_rejection(self, intent: OrderIntent, parent_id: str) -> None:
        at = self.now()
        self._require_strategy().on_order(
            OrderUpdate(
                identity=OrderIdentity(
                    account_id=self.account_id,
                    exchange=intent.instrument.exchange,
                    client_order_id=parent_id,
                ),
                instrument=intent.instrument,
                side=intent.side,
                offset=intent.offset,
                status=OrderStatus.REJECTED,
                quantity=intent.quantity,
                filled_quantity=0,
                event_time=at,
                available_at=at,
            )
        )

    def now(self) -> datetime:
        return utc_timestamp(self._clock.now())

    def get_equity(self) -> Decimal:
        if self._equity is None:
            raise RuntimeError("strategy equity requires an explicit account equity provider")
        equity = self._equity()
        require_decimal(equity, "account equity")
        return equity

    def get_position(self, instrument: InstrumentId) -> int:
        return self._account.get_position(instrument)

    def is_order_active(self, client_order_id: str) -> bool:
        ids = self._children.get(client_order_id, (client_order_id,))
        if any(self._account.is_order_active(value) for value in ids):
            return True
        return any(
            (queued := self._client.get(command_id)) is not None
            and isinstance(queued.command.payload, OrderIntent)
            and queued.command.payload.client_order_id in ids
            and queued.status in (CommandStatus.PENDING, CommandStatus.DISPATCHING, CommandStatus.SENT_UNKNOWN)
            for command_id in self._pending
        )

    def _next_id(self) -> str:
        if self._current_bar is None and self._current_tick is None:
            raise RuntimeError("trading intents are only accepted inside a market callback")
        self._ordinal += 1
        event = (
            "tick:" + self._tick_key(self._current_tick)
            if self._current_tick is not None
            else "bar:" + self._current_bar.bar_end.isoformat()
        )
        key = f"{self.stream_id}:{event}:{self._ordinal}"
        return "strategy-" + sha256(key.encode()).hexdigest()[:32]

    def _check_ready(self, control: ControlEpoch) -> None:
        if not self._ready() or self._control() != control:
            raise RuntimeError("execution readiness or control epoch changed during the strategy callback")

    def send_order(
        self,
        instrument: InstrumentId,
        side: Side,
        offset: Offset,
        quantity: int,
        order_type: OrderType = OrderType.MARKET,
        limit_price_ticks: int | None = None,
        *,
        strategy_id: str | None = None,
    ) -> str:
        if instrument != self.instrument or strategy_id not in (None, self.strategy_id):
            raise ValueError("order belongs to another strategy or instrument")
        if self._current_tick is not None and offset == Offset.OPEN:
            raise ValueError("tick callbacks may only reduce exposure")
        intent = OrderIntent(
            client_order_id=self._next_id(),
            account_id=self.account_id,
            strategy_id=self.strategy_id,
            instrument=instrument,
            side=side,
            offset=offset,
            quantity=quantity,
            order_type=order_type,
            limit_price_ticks=limit_price_ticks,
            created_at=self.now(),
        )
        if self._submission_reason == "warmup":
            return intent.client_order_id
        self._intents.append(intent)
        if self._submission_reason != "processed":
            return intent.client_order_id
        control = self._control()
        if control is None:
            raise RuntimeError("execution control is unavailable")
        self._check_ready(control)
        children = tuple(self._translate(intent))
        if not children or sum(child.quantity for child in children) != intent.quantity:
            raise ValueError("order translation must preserve total quantity")
        final = []
        for index, child in enumerate(children):
            if (
                child.account_id != intent.account_id
                or child.strategy_id != intent.strategy_id
                or child.instrument != intent.instrument
                or child.side != intent.side
                or (child.offset == Offset.OPEN) != (intent.offset == Offset.OPEN)
                or child.offset == Offset.CLOSE
                or (intent.offset in (Offset.CLOSE_TODAY, Offset.CLOSE_YESTERDAY) and child.offset != intent.offset)
            ):
                raise ValueError("order translation changed ownership/direction or left an unresolved close bucket")
            final.append(
                replace(
                    child,
                    client_order_id=f"{intent.client_order_id}-{index}",
                    parent_order_id=None,
                    created_at=intent.created_at,
                )
            )
        self._children[intent.client_order_id] = tuple(child.client_order_id for child in final)
        for child in final:
            self._check_ready(control)
            at = self._current_tick.meta.event_time if self._current_tick else self._current_bar.bar_end
            max_age = self._max_tick_age if self._current_tick else self._max_bar_age
            if self.now() - at > max_age:
                raise RuntimeError("market data became stale during the strategy callback")
            self._submit(
                ExecutionCommand(
                    command_id=child.client_order_id,
                    account_id=self.account_id,
                    producer_id=self.producer_id,
                    control=control,
                    kind=CommandKind.SUBMIT,
                    submitted_at=intent.created_at,
                    payload=child,
                )
            )
        return intent.client_order_id

    def _submit(self, command: ExecutionCommand) -> None:
        self._client.submit(command)
        self._pending.append(command.command_id)
        self._commands.append(command)

    def buy(
        self,
        instrument: InstrumentId,
        quantity: int,
        offset: Offset = Offset.OPEN,
        limit_price_ticks: int | None = None,
        *,
        strategy_id: str | None = None,
    ) -> str:
        return self.send_order(
            instrument,
            Side.BUY,
            offset,
            quantity,
            OrderType.LIMIT if limit_price_ticks is not None else OrderType.MARKET,
            limit_price_ticks,
            strategy_id=strategy_id,
        )

    def sell(
        self,
        instrument: InstrumentId,
        quantity: int,
        offset: Offset = Offset.CLOSE,
        limit_price_ticks: int | None = None,
        *,
        strategy_id: str | None = None,
    ) -> str:
        return self.send_order(
            instrument,
            Side.SELL,
            offset,
            quantity,
            OrderType.LIMIT if limit_price_ticks is not None else OrderType.MARKET,
            limit_price_ticks,
            strategy_id=strategy_id,
        )

    def cancel_order(self, client_order_id: str) -> None:
        command_id = self._next_id()
        if self._submission_reason == "warmup":
            return
        control = self._control()
        if not self._ready() or control is None:
            return
        ids = self._children.get(client_order_id, (client_order_id,))
        for index, child_id in enumerate(ids):
            identity = self._account.order_identity(child_id)
            if identity is None or identity.account_id != self.account_id:
                raise ValueError("cancel requires an original identity from the committed account view")
            self._check_ready(control)
            self._submit(
                ExecutionCommand(
                    command_id=f"{command_id}-{index}",
                    account_id=self.account_id,
                    producer_id=self.producer_id,
                    control=control,
                    kind=CommandKind.CANCEL,
                    submitted_at=self.now(),
                    payload=identity,
                )
            )

    def schedule_timer(self, at: datetime, timer_id: str, payload: object = None) -> None:
        self._clock.schedule(at, TimerEvent(timer_id=timer_id, payload=payload))

    def _parent_identity(self, identity: OrderIdentity | None) -> OrderIdentity | None:
        if identity is None or identity.account_id != self.account_id or identity.client_order_id is None:
            return None
        queued = self._client.get(identity.client_order_id)
        if queued is None or queued.command.producer_id != self.producer_id:
            return None
        intent = queued.command.payload
        if not isinstance(intent, OrderIntent) or intent.strategy_id != self.strategy_id:
            return None
        return replace(identity, client_order_id=self._signal_id(intent.client_order_id))

    @staticmethod
    def _signal_id(client_order_id: str) -> str:
        """最终子单 ID 编码策略信号 ID；不使用账户领域的 parent_order_id。

        账户父单实体从未创建，故不能把信号关联写入那个外键语义字段。
        此编码随命令表持久化，重启后仍能重建策略的父子关联。
        """
        parent, separator, ordinal = client_order_id.rpartition("-")
        if separator and parent.startswith("strategy-") and len(parent) == 41 and ordinal.isdecimal():
            return parent
        return client_order_id

    def on_order(self, order: OrderUpdate) -> None:
        """只转发已归属的回报，父 ID 供策略追踪，柜台三元组原样保留。"""
        identity = self._parent_identity(order.identity)
        if identity is not None and order.instrument == self.instrument:
            self._require_strategy().on_order(replace(order, identity=identity))

    def on_trade(self, trade: Trade) -> None:
        """账户观察队列允许重投；按成交键确保本次策略实例只消费一次。"""
        if trade.deduplication_key in self._seen_trades:
            return
        identity = self._parent_identity(trade.order_identity)
        if identity is not None and trade.instrument == self.instrument:
            self._require_strategy().on_trade(replace(trade, order_identity=identity))
            self._seen_trades.add(trade.deduplication_key)

    def on_timer(self, timer: TimerEvent) -> None:
        """首版只允许 Bar 内提交交易意图，定时器回调只能观察与告警。"""
        self._require_strategy().on_timer(timer)
