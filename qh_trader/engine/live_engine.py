"""[Engine 层] 独立策略 Journal 消费与命令投递 (S5-05, FR-RISK-01, ADR-X1, A23).

策略仅处理已提交事件，不连接柜台、不取得执行锁、不更新账户。每个回调的意图先完整缓冲，
与事件摘要一起持久化，才投递命令表；任何失败均终止当前实例。恢复通过同一配置和已记录
事件重建确定性策略状态，并逐回调核对意图。策略必须仅依赖回调、上下文与固定配置，不能
使用外部 I/O、随机数或墙钟。当前持仓查询是本策略已归属成交的只读净持仓。

首次启动从已提交 head 之后消费，不追发历史信号。只接受已有 Bar 与已提交 TimerEvent；
Tick 不伪装成 Bar，本地定时器也不绕过 Journal。控制代次固定绑定，接管后必须显式迁移。
"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime

from qh_trader.core.constants import EventKind, Offset, OrderStatus, OrderType, SendState, Side
from qh_trader.core.event import CanonicalEvent
from qh_trader.core.execution import CommandKind, ExecutionCommand, ExecutionNotReadyError
from qh_trader.core.objects import (
    Bar,
    ControlEpoch,
    InstrumentId,
    LocalSendResult,
    OrderIdentity,
    OrderIntent,
    OrderUpdate,
    Trade,
    TradeKey,
    require_text,
)
from qh_trader.core.ports import StrategyJournalPort, StrategyPort, StrategyRuntimeStorePort
from qh_trader.domain.orders import OrderManager


class StrategyReplayError(RuntimeError):
    """历史回调未能生成原来的意图；拒绝继续交易以免恢复后的状态偏移."""


class LiveStrategyEngine:
    """同时实现 StrategyContextPort，由装配器注入策略与端口."""

    def __init__(
        self,
        *,
        journal: StrategyJournalPort,
        runtime: StrategyRuntimeStorePort,
        heartbeat: Callable[[bool], object] | None = None,
        execution_ready: Callable[[], bool] | None = None,
        heartbeat_interval: float = 1.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.journal = journal
        self.runtime = runtime
        self.strategy_id = str(runtime.metadata["strategy_id"])
        self.account_id = str(runtime.metadata["account_id"])
        self.control = runtime.metadata["control"]
        if self.account_id != journal.account_id or not isinstance(self.control, ControlEpoch):
            raise ValueError("strategy runtime account/control binding is invalid")
        self._heartbeat = heartbeat or (lambda ready: None)
        self._execution_ready = execution_ready or (lambda: True)
        if not math.isfinite(heartbeat_interval) or heartbeat_interval <= 0:
            raise ValueError("strategy heartbeat interval must be finite and positive")
        self._heartbeat_interval = heartbeat_interval
        self._monotonic = monotonic
        self._last_beat = monotonic()
        self._strategy: StrategyPort | None = None
        self._orders = OrderManager()
        self._commands: dict[str, ExecutionCommand] = {}
        self._notified_trades: set[TradeKey] = set()
        self._reconstructed: set[str] = set()
        self._event: CanonicalEvent | None = None
        self._buffer: list[ExecutionCommand] = []
        self._started = False
        self._failed = False
        self._stopped = False

    @property
    def ready(self) -> bool:
        return self._started and not self._failed and not self._stopped

    def _fence(self) -> None:
        if not self._execution_ready():
            raise ExecutionNotReadyError("execution heartbeat is stale, unready or belongs to another epoch")
        current = self.journal.control()
        service = self.journal.state("execution_service")
        if current is None or current.epoch != self.control:
            raise ExecutionNotReadyError("strategy control epoch changed; explicit migration is required")
        if not isinstance(service, Mapping) or service.get("phase") != "READY":
            raise ExecutionNotReadyError("execution service is not ready for strategy commands")
        if service.get("control") != self.control:
            raise ExecutionNotReadyError("execution service readiness belongs to another controller/epoch")

    def _beat(self, ready: bool) -> None:
        self._heartbeat(ready)
        self._last_beat = self._monotonic()

    def _beat_if_due(self, ready: bool) -> None:
        if self._monotonic() - self._last_beat >= self._heartbeat_interval:
            self._fence()
            self._beat(ready)

    def start(self, strategy: StrategyPort) -> None:
        if self._started or self._failed or self._stopped:
            raise RuntimeError("a strategy engine instance can only start once")
        if strategy.strategy_id != self.strategy_id:
            raise ValueError("strategy id differs from its persisted runtime binding")
        self._strategy = strategy
        try:
            self._fence()
            self._beat(False)
            strategy.on_init()
            strategy.on_start()
            # 回放先完整重建并校验，再重投 outbox；回放中绝不创建历史缺失意图。
            head, events = self.journal.read_events(int(self.runtime.metadata["start_cursor"]))
            if head < self.runtime.cursor or (events[-1].sequence if events else 0) < self.runtime.last_sequence:
                raise StrategyReplayError(
                    "strategy history is ahead of the available journal; restore the matching pair"
                )
            for event in events:
                previous = self.runtime.recorded(event)
                if previous is None:
                    break
                actual = self._callback(event)
                if previous != actual:
                    raise StrategyReplayError("strategy replay changed previously recorded intents")
                self._reconstructed.add(event.event_id)
                self._beat_if_due(False)
            self._flush()
            self._started = True
            self._beat(True)
        except BaseException:
            self._failed = True
            self._beat(False)
            raise

    def run_once(self) -> int:
        if not self.ready:
            raise ExecutionNotReadyError("strategy engine is stopped, failed or not started")
        try:
            self._fence()
            head, events = self.journal.read_events(self.runtime.cursor)
            processed = 0
            for event in events:
                self._fence()
                if event.event_id not in self._reconstructed:
                    previous = self.runtime.recorded(event)
                    if previous is not None:
                        raise StrategyReplayError("recorded callback was not reconstructed during startup")
                    commands = self._callback(event)
                    self.runtime.record(event, commands)
                    self._reconstructed.add(event.event_id)
                    processed += 1
                self._flush()
                self._beat_if_due(True)
            self.runtime.advance(head)
            self._beat(True)
            return processed
        except BaseException:
            self._failed = True
            self._beat(False)
            raise

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._event = None
        try:
            if self._strategy is not None:
                self._strategy.on_stop()
        finally:
            self._beat(False)

    def _flush(self) -> None:
        for command in self.runtime.pending():
            self._fence()
            if command.control != self.control:
                raise StrategyReplayError("outbox command belongs to another control epoch")
            self.journal.submit(command)
            self.runtime.delivered(command.command_id)
            self._beat_if_due(self.ready)

    def _callback(self, event: CanonicalEvent) -> tuple[ExecutionCommand, ...]:
        self._event, self._buffer = event, []
        try:
            payload = event.payload
            if event.kind == EventKind.MARKET_DATA and isinstance(payload, Bar):
                if payload.meta.available_at > event.available_at:
                    raise ValueError("journal event exposes a bar before its availability")
                self._strategy.on_bar(payload)
            elif event.kind == EventKind.ORDER_REPORT and isinstance(payload, OrderUpdate):
                if payload.identity.account_id != self.account_id:
                    raise ValueError("strategy journal contains a report for another account")
                # 未归属的账户委托不送入本策略。
                identity = payload.identity
                own = self._orders.find_matching_order(
                    client_order_id=identity.client_order_id,
                    exchange=identity.exchange,
                    exchange_order_id=identity.exchange_order_id,
                    front_id=identity.front_id,
                    session_id=identity.session_id,
                    order_ref=identity.order_ref,
                )
                if own is not None:
                    self._orders.process_order_update(payload)
                    self._strategy.on_order(
                        replace(payload, identity=replace(payload.identity, client_order_id=own.client_order_id))
                    )
                    self._notify_trades()
            elif event.kind == EventKind.TRADE_REPORT and isinstance(payload, Trade):
                if payload.account_id != self.account_id:
                    raise ValueError("strategy journal contains a trade for another account")
                self._orders.process_trade(payload)
                self._notify_trades()
            elif event.kind == EventKind.TIMER:
                self._strategy.on_timer(payload)
            elif event.kind == EventKind.CONTROL and isinstance(payload, Mapping):
                self._execution_fact(payload)
            return tuple(self._buffer)
        finally:
            self._event = None

    def _notify_trades(self) -> None:
        for order in self._orders.orders():
            for trade in order.trades:
                if trade.deduplication_key not in self._notified_trades:
                    self._notified_trades.add(trade.deduplication_key)
                    identity = trade.order_identity or OrderIdentity(
                        account_id=self.account_id,
                        exchange=trade.instrument.exchange,
                        client_order_id=order.client_order_id,
                    )
                    self._strategy.on_trade(
                        replace(trade, order_identity=replace(identity, client_order_id=order.client_order_id))
                    )

    def _execution_fact(self, payload: Mapping[str, object]) -> None:
        action = payload.get("action")
        command = payload.get("command")
        if action == "command_rejected" and isinstance(command, ExecutionCommand):
            if command.command_id in self._commands and command.kind == CommandKind.SUBMIT:
                self._reject_order(command.payload)
        elif action == "local_send_result":
            command = self._commands.get(str(payload.get("command_id")))
            result = payload.get("result")
            if command is not None and command.kind == CommandKind.SUBMIT and isinstance(result, LocalSendResult):
                self._orders.record_send_result(command.payload.client_order_id, result)
                if result.state == SendState.NOT_SENT:
                    self._reject_order(command.payload)

    def _reject_order(self, intent: OrderIntent) -> None:
        order = self._orders.get_order(intent.client_order_id)
        update = OrderUpdate(
            identity=OrderIdentity(
                account_id=self.account_id, exchange=intent.instrument.exchange, client_order_id=intent.client_order_id
            ),
            instrument=intent.instrument,
            side=intent.side,
            offset=intent.offset,
            status=OrderStatus.REJECTED,
            quantity=intent.quantity,
            filled_quantity=order.cum_filled_qty,
            event_time=self.now(),
            available_at=self.now(),
        )
        self._orders.process_order_update(update)
        self._strategy.on_order(update)

    def now(self) -> datetime:
        if self._event is None:
            raise ExecutionNotReadyError("strategy time and intents require a committed event callback")
        return self._event.available_at

    def _identifier(self) -> str:
        self.now()  # 生命周期回调不允许产生没有耐久来源事件的命令。
        key = "|".join(
            (
                self.account_id,
                self.strategy_id,
                str(self.runtime.metadata["run_id"]),
                self._event.event_id,
                str(len(self._buffer)),
            )
        )
        return "strategy-" + hashlib.sha256(key.encode("utf-8")).hexdigest()

    def _enqueue(self, kind: CommandKind, payload: OrderIntent | OrderIdentity, identifier: str) -> None:
        command = ExecutionCommand(
            command_id=identifier,
            account_id=self.account_id,
            producer_id="strategy:" + self.strategy_id,
            control=self.control,
            kind=kind,
            submitted_at=self.now(),
            payload=payload,
        )
        self._buffer.append(command)
        self._commands[identifier] = command

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
        if strategy_id not in (None, self.strategy_id):
            raise ValueError("strategy cannot submit intents attributed to another strategy")
        identifier = self._identifier()
        intent = OrderIntent(
            client_order_id=identifier,
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
        self._orders.create_order(intent)
        self._enqueue(CommandKind.SUBMIT, intent, identifier)
        return identifier

    def buy(self, instrument, quantity, offset=Offset.OPEN, limit_price_ticks=None, *, strategy_id=None) -> str:
        return self.send_order(
            instrument,
            Side.BUY,
            offset,
            quantity,
            OrderType.MARKET if limit_price_ticks is None else OrderType.LIMIT,
            limit_price_ticks,
            strategy_id=strategy_id,
        )

    def sell(self, instrument, quantity, offset=Offset.CLOSE, limit_price_ticks=None, *, strategy_id=None) -> str:
        return self.send_order(
            instrument,
            Side.SELL,
            offset,
            quantity,
            OrderType.MARKET if limit_price_ticks is None else OrderType.LIMIT,
            limit_price_ticks,
            strategy_id=strategy_id,
        )

    def cancel_order(self, client_order_id: str) -> None:
        require_text(client_order_id, "client_order_id")
        order = self._orders.get_order(client_order_id)
        if order is None:
            raise ValueError("strategy cannot cancel an order it does not own")
        identity = order.identity or OrderIdentity(
            account_id=self.account_id, exchange=order.instrument.exchange, client_order_id=client_order_id
        )
        identity = replace(identity, client_order_id=client_order_id)
        self._enqueue(CommandKind.CANCEL, identity, self._identifier())

    def get_position(self, instrument: InstrumentId) -> int:
        return sum(
            trade.quantity * (1 if trade.side == Side.BUY else -1)
            for order in self._orders.orders()
            if order.instrument == instrument
            for trade in order.trades
        )

    def is_order_active(self, client_order_id: str) -> bool:
        order = self._orders.get_order(client_order_id)
        return order is not None and order.is_active

    def schedule_timer(self, at: datetime, timer_id: str, payload: object = None) -> None:
        raise NotImplementedError("live timers must be committed by the execution event producer before subscription")
