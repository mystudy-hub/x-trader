"""[Engine 层] BaseEngine：策略上下文、订单意图流水线与回报分发 (S3-02, FR-MATCH-01, FR-EXEC-01, FR-RISK-03).

架构约束：Engine 只依赖 Core 协议与 Domain 内核；网关、日志、时段门均以端口注入。

意图流水线 (send_order)：
1. 控制代次校验 -> 交易所硬约束 (价格带 / 开仓限额 / 持仓限额 / 交割月) -> 长假钩子 (FR-RISK-02/03/06)；
2. SmartRouter 拆单 -> 持仓预占 -> 资金预占 (同一意图内原子)；
3. 按执行时点策略立即发往网关或登记到目标时刻的定时器 (FR-EXEC-02/03)。

回报分发 (dispatch_gateway_events)：网关出站事件经 EventQueue 按 (可见时刻, 显式优先级, 序号) 排序后
进入 OrderManager / AccountLedger；终态回报释放剩余预占；每批事件作为一笔 Journal 事务落盘。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal

from qh_trader.core.clock import VirtualClock
from qh_trader.core.constants import (
    EventKind,
    Exchange,
    ExecutionPolicy,
    MissedExecutionPolicy,
    MissingRuleError,
    Offset,
    OrderStatus,
    OrderType,
    PositionSide,
    ReplayOrder,
    SendState,
    Side,
)
from qh_trader.core.event import CanonicalEvent, EventQueue, JournalTransaction, TimerEvent
from qh_trader.core.objects import (
    ControlEpoch,
    InstrumentId,
    LocalSendResult,
    OrderIdentity,
    OrderIntent,
    OrderUpdate,
    Trade,
    require_decimal,
    require_text,
)
from qh_trader.core.ports import (
    ExecutionPort,
    JournalPort,
    RuleStorePort,
    SessionGatePort,
    StrategyContextPort,
    StrategyPort,
)
from qh_trader.domain.ledger import AccountLedger
from qh_trader.domain.limits import ExchangeLimits, LimitViolationError
from qh_trader.domain.orders import Order, OrderManager
from qh_trader.domain.positions import PositionManager
from qh_trader.domain.risk import EpochViolationError, RiskManager, RiskViolationError
from qh_trader.domain.rules import RuleEngine
from qh_trader.domain.smart_router import (
    CloseCapabilityTable,
    ExchangeCloseCapability,
    SmartRouter,
)

#: 模拟事件同时刻的显式优先级 (写入运行清单)。成交事实先于订单状态，定时器最后。
SIMULATED_EVENT_PRIORITIES: Mapping[EventKind, int] = {
    EventKind.TRADE_REPORT: 0,
    EventKind.ORDER_REPORT: 1,
    EventKind.SETTLEMENT: 2,
    EventKind.TIMER: 3,
}

UNATTRIBUTED_STRATEGY = "unattributed"
DEFERRED_TIMER_PREFIX = "deferred-order:"


def default_close_capability_table() -> CloseCapabilityTable:
    """提供国内期货各大交易所标准平今/平昨能力表."""
    caps = {
        Exchange.SHFE: ExchangeCloseCapability(
            supports_close_today=True,
            requires_explicit_close_today=True,
            supports_unified_close=False,
            evidence_ref="standard-shfe-rules",
        ),
        Exchange.INE: ExchangeCloseCapability(
            supports_close_today=True,
            requires_explicit_close_today=True,
            supports_unified_close=False,
            evidence_ref="standard-ine-rules",
        ),
        Exchange.DCE: ExchangeCloseCapability(
            supports_close_today=True,
            requires_explicit_close_today=False,
            supports_unified_close=True,
            evidence_ref="standard-dce-rules",
        ),
        Exchange.CZCE: ExchangeCloseCapability(
            supports_close_today=False,
            requires_explicit_close_today=False,
            supports_unified_close=True,
            evidence_ref="standard-czce-rules",
        ),
        Exchange.CFFEX: ExchangeCloseCapability(
            supports_close_today=True,
            requires_explicit_close_today=False,
            supports_unified_close=True,
            evidence_ref="standard-cffex-rules",
        ),
        Exchange.GFEX: ExchangeCloseCapability(
            supports_close_today=True,
            requires_explicit_close_today=False,
            supports_unified_close=True,
            evidence_ref="standard-gfex-rules",
        ),
    }
    return CloseCapabilityTable(version="v1.0-default", capabilities=caps)


@dataclass(frozen=True, slots=True)
class InstrumentEconomics:
    """按合约登记的经济参数；来源写入运行清单."""

    multiplier: Decimal
    price_tick: Decimal
    commission_per_lot: Decimal
    margin_ratio: Decimal
    source: str

    def __post_init__(self) -> None:
        require_decimal(self.multiplier, "multiplier", Decimal("0.000001"))
        require_decimal(self.price_tick, "price_tick", Decimal("0.000001"))
        require_decimal(self.commission_per_lot, "commission_per_lot", Decimal(0))
        require_decimal(self.margin_ratio, "margin_ratio", Decimal(0))
        require_text(self.source, "source")


@dataclass(frozen=True, slots=True)
class RejectedIntent:
    """事前风控或本地校验拒绝的意图 (FR-RISK-03: 违规在回测报告中可见)."""

    client_order_id: str
    strategy_id: str
    instrument: InstrumentId
    side: Side
    offset: Offset
    quantity: int
    at: datetime
    stage: str
    reason: str


@dataclass(frozen=True, slots=True)
class MissedExecution:
    """错过目标执行时点的意图及其处置 (FR-EXEC-03)."""

    client_order_id: str
    target_time: datetime
    detected_at: datetime
    policy: MissedExecutionPolicy
    rescheduled_to: datetime | None


@dataclass(frozen=True, slots=True)
class OffsetRewrite:
    """递延平仓意图跨交易日后在送出时刻重新规划的开平标志 (FR-CAL-06 / FR-ORD-01)."""

    client_order_id: str
    planned_offset: Offset
    sent_offset: Offset
    at: datetime


@dataclass
class _DeferredIntent:
    intent: OrderIntent
    target_time: datetime
    reason: str  # "session-gate" (本地持有到允许报单) / "execution-policy" (显式目标开盘)
    attempts: int = 0


@dataclass
class _Attribution:
    strategy_id: str
    parent_id: str
    children: list[str] = field(default_factory=list)


class BaseEngine(StrategyContextPort):
    """引擎基类，实现 StrategyContextPort 为策略提供运行时环境."""

    def __init__(
        self,
        account_id: str,
        *,
        start_time: datetime,
        initial_capital: Decimal = Decimal("1000000.00"),
        trading_day: date | None = None,
        controller_id: str = "engine-controller",
        gateway: ExecutionPort | None = None,
        journal: JournalPort | None = None,
        rule_store: RuleStorePort | None = None,
        session_gate: SessionGatePort | None = None,
        smart_router: SmartRouter | None = None,
        risk_manager: RiskManager | None = None,
        exchange_limits: ExchangeLimits | None = None,
        natural_person: bool = False,
        default_economics: InstrumentEconomics | None = None,
        execution_policy: ExecutionPolicy = ExecutionPolicy.NEXT_BAR_OPEN,
        missed_execution: MissedExecutionPolicy = MissedExecutionPolicy.DEFER,
        fixed_time_before_close: timedelta | None = None,
        commission_profile: str = "default",
    ) -> None:
        require_text(account_id, "account_id")
        if execution_policy == ExecutionPolicy.NEXT_DAY_FIXED_TIME:
            if not isinstance(fixed_time_before_close, timedelta) or fixed_time_before_close <= timedelta(0):
                raise ValueError("NEXT_DAY_FIXED_TIME requires a positive fixed_time_before_close offset")
        self.fixed_time_before_close = fixed_time_before_close
        self.account_id = account_id
        self.clock: VirtualClock[TimerEvent] = VirtualClock(start_time)
        self.epoch = ControlEpoch(controller_id, 1)

        self.ledger = AccountLedger(account_id=account_id, initial_capital=initial_capital, trading_day=trading_day)
        self.position_manager: PositionManager = self.ledger.position_manager
        self.order_manager = OrderManager()
        self.smart_router = smart_router or SmartRouter(default_close_capability_table())
        self.risk_manager = risk_manager or RiskManager(
            account_id=account_id, control=self.epoch, limits=exchange_limits, trading_day=trading_day
        )
        if exchange_limits is not None and risk_manager is not None:
            self.risk_manager.limits = exchange_limits
        self.natural_person = natural_person

        self.gateway = gateway
        self.journal = journal
        self.rule_store = rule_store
        self.rule_engine = RuleEngine(rule_store) if rule_store is not None else None
        self.session_gate = session_gate
        self.commission_profile = commission_profile
        self.execution_policy = execution_policy
        self.missed_execution = missed_execution

        self._economics: dict[InstrumentId, InstrumentEconomics] = {}
        self._default_economics = default_economics
        self._mark_prices: dict[InstrumentId, Decimal] = {}
        self._price_limits: dict[InstrumentId, tuple[Decimal, Decimal]] = {}

        self.strategies: dict[str, StrategyPort] = {}
        self._order_id_counter = 0
        self._attribution: dict[str, _Attribution] = {}
        self._deferred: dict[str, _DeferredIntent] = {}
        self._next_bar_open: datetime | None = None

        self._ingress_seq = 0
        self._journal_cursor = 0
        self._tx_counter = 0
        self._dispatch_depth = 0
        #: 每个合约严格晚于当前瞬间的下一根可观测 Bar 开盘时刻 (由回测引擎在每个阶段前刷新)
        self._following_open: dict[InstrumentId, datetime] = {}

        self.rejected_intents: list[RejectedIntent] = []
        self.missed_executions: list[MissedExecution] = []
        self.offset_rewrites: list[OffsetRewrite] = []
        self.local_rejection_reports: list[OrderUpdate] = []
        self.processed_events: list[CanonicalEvent] = []

        if isinstance(gateway, ExecutionPort) and hasattr(gateway, "bind_clock"):
            gateway.bind_clock(self.clock)
        if isinstance(gateway, ExecutionPort) and hasattr(gateway, "bind_session_gate"):
            gateway.bind_session_gate(session_gate)

    # ------------------------------------------------------------------ 合约经济参数
    def register_instrument(self, instrument: InstrumentId, economics: InstrumentEconomics) -> None:
        self._economics[instrument] = economics

    def economics(self, instrument: InstrumentId) -> InstrumentEconomics:
        found = self._economics.get(instrument)
        if found is not None:
            return found
        if self._default_economics is not None:
            return self._default_economics
        raise MissingRuleError(f"no contract economics registered for {instrument}")

    def economics_table(self) -> dict[str, InstrumentEconomics]:
        return {str(inst): eco for inst, eco in self._economics.items()}

    def set_mark_price(self, instrument: InstrumentId, price: Decimal) -> None:
        self._mark_prices[instrument] = price

    def set_price_limits(self, instrument: InstrumentId, upper: Decimal, lower: Decimal) -> None:
        self._price_limits[instrument] = (upper, lower)

    def clear_price_limits(self, instrument: InstrumentId) -> None:
        self._price_limits.pop(instrument, None)

    # ------------------------------------------------------------------ 策略管理
    def add_strategy(self, strategy: StrategyPort) -> None:
        if strategy.strategy_id in self.strategies:
            raise ValueError(f"duplicate strategy_id: {strategy.strategy_id}")
        self.strategies[strategy.strategy_id] = strategy
        strategy.on_init()

    # ------------------------------------------------------------------ StrategyContextPort 接口
    def now(self) -> datetime:
        return self.clock.now()

    def get_position(self, instrument: InstrumentId) -> int:
        """获取当前净持仓 (多头 - 空头)."""
        pos_l = self.position_manager.get_position(instrument, PositionSide.LONG)
        pos_s = self.position_manager.get_position(instrument, PositionSide.SHORT)
        return pos_l.total_position - pos_s.total_position

    def schedule_timer(self, at: datetime, timer_id: str, payload: object = None) -> None:
        self.clock.schedule(at, TimerEvent(timer_id=timer_id, payload=payload))

    def is_order_active(self, client_order_id: str) -> bool:
        """父单或其任一子单仍可能成交时为 True；本地拒绝、终态回报、过期都使其变为 False."""
        order = self.order_manager.get_order(client_order_id)
        if order is None:
            return False
        if order.child_order_ids:
            return any(
                (child := self.order_manager.get_order(cid)) is not None and child.is_active
                for cid in order.child_order_ids
            )
        return order.is_active

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
        """提交意图：风控 -> 拆单 -> 预占 -> 按执行时点策略发往网关或登记定时器。返回父单号."""
        if quantity <= 0:
            raise ValueError(f"order quantity must be positive: {quantity}")
        if order_type == OrderType.LIMIT and limit_price_ticks is None:
            raise ValueError("a LIMIT order requires limit_price_ticks; use OrderType.MARKET for unpriced intents")
        if order_type == OrderType.MARKET and limit_price_ticks is not None:
            raise ValueError("a MARKET order cannot carry a limit price")
        owner = strategy_id or UNATTRIBUTED_STRATEGY

        self._order_id_counter += 1
        parent_cid = f"ord-{self._order_id_counter}"
        now = self.now()
        parent_intent = OrderIntent(
            client_order_id=parent_cid,
            account_id=self.account_id,
            strategy_id=owner,
            instrument=instrument,
            side=side,
            offset=offset,
            quantity=quantity,
            order_type=order_type,
            limit_price_ticks=limit_price_ticks,
            created_at=now,
        )
        self.order_manager.create_order(parent_intent)
        self._attribution[parent_cid] = _Attribution(strategy_id=owner, parent_id=parent_cid)

        # 1. 事前风控 (FR-RISK-02/03/06)
        rejection = self._pre_trade_check(parent_intent)
        if rejection is not None:
            self._reject_locally(parent_cid, rejection[0], rejection[1])
            return parent_cid

        # 2. 路由拆单
        target_pos = None
        if offset != Offset.OPEN:
            target_side = PositionSide.LONG if side == Side.SELL else PositionSide.SHORT
            target_pos = self.position_manager.get_position(instrument, target_side)

        def id_gen() -> str:
            self._order_id_counter += 1
            return f"ord-{self._order_id_counter}"

        try:
            plan = self.smart_router.plan_order(parent_intent, target_pos, id_gen)
        except (ValueError, MissingRuleError) as exc:
            self._reject_locally(parent_cid, "routing", str(exc))
            return parent_cid

        # 3. 预占并按执行时点提交 (预占失败整单本地拒绝)
        reserved: list[OrderIntent] = []
        try:
            for child in plan.children:
                if child.client_order_id != parent_cid:
                    self.order_manager.create_order(child)
                    self._attribution[parent_cid].children.append(child.client_order_id)
                    self._attribution[child.client_order_id] = _Attribution(owner, parent_cid)
                if child.offset != Offset.OPEN:
                    self.position_manager.reserve_for_order(
                        child.client_order_id, child.instrument, child.side, child.offset, child.quantity
                    )
                margin, fee = self._estimate_reservation(child)
                self.ledger.reserve_funds(child.client_order_id, margin, fee)
                reserved.append(child)
        except (ValueError, MissingRuleError) as exc:
            for child in reserved:
                self.position_manager.release_reservation(child.client_order_id)
                self.ledger.release_funds(child.client_order_id)
            for child in plan.children:
                if self.order_manager.get_order(child.client_order_id) is not None:
                    self._reject_locally(child.client_order_id, "reservation", str(exc))
            if self.order_manager.get_order(parent_cid) is not None and parent_cid not in {
                c.client_order_id for c in plan.children
            }:
                self._reject_locally(parent_cid, "reservation", str(exc))
            return parent_cid

        for child in plan.children:
            self._dispatch_intent(child)
        return parent_cid

    def buy(
        self,
        instrument: InstrumentId,
        quantity: int,
        offset: Offset = Offset.OPEN,
        limit_price_ticks: int | None = None,
        *,
        strategy_id: str | None = None,
    ) -> str:
        order_type = OrderType.LIMIT if limit_price_ticks is not None else OrderType.MARKET
        return self.send_order(
            instrument, Side.BUY, offset, quantity, order_type, limit_price_ticks, strategy_id=strategy_id
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
        order_type = OrderType.LIMIT if limit_price_ticks is not None else OrderType.MARKET
        return self.send_order(
            instrument, Side.SELL, offset, quantity, order_type, limit_price_ticks, strategy_id=strategy_id
        )

    def cancel_order(self, client_order_id: str) -> None:
        order = self.order_manager.get_order(client_order_id)
        if order is None:
            return
        targets = [order]
        if order.child_order_ids:
            targets = [o for cid in order.child_order_ids if (o := self.order_manager.get_order(cid)) is not None]

        for target in targets:
            if not target.is_active:
                continue
            deferred = self._deferred.pop(target.client_order_id, None)
            if deferred is not None:
                # 尚未发往网关：本地撤销，立即释放预占
                self._reject_locally(target.client_order_id, "cancel-before-send", "cancelled before dispatch")
                continue
            if target.identity is None or self.gateway is None:
                continue
            self.risk_manager.check_cancel_command(target, self.epoch, self.ledger.current_trading_day)
            target.request_cancel()
            result = self.gateway.cancel(target.identity, self.epoch)
            if result.state == SendState.NOT_SENT:
                target.cancel_rejected(result.evidence)
            self.dispatch_gateway_events()

    # ------------------------------------------------------------------ 风控与预占
    def _pre_trade_check(self, intent: OrderIntent) -> tuple[str, str] | None:
        """S2 RiskManager 统一事前风控入口 (FR-RISK-01~07)；回测与实盘同一实现."""
        day = self.ledger.current_trading_day
        band = None
        limits = self._price_limits.get(intent.instrument)
        if limits is not None:
            tick = self.economics(intent.instrument).price_tick
            band = (int(limits[1] / tick), int(limits[0] / tick))
        target_side = PositionSide.LONG if intent.side == Side.BUY else PositionSide.SHORT
        if intent.offset != Offset.OPEN:
            target_side = PositionSide.LONG if intent.side == Side.SELL else PositionSide.SHORT
        current_pos = self.position_manager.get_position(intent.instrument, target_side)
        funds = self.ledger.get_funds_state(current_prices=dict(self._mark_prices), margin_rates=self._margin_rates())
        try:
            margin_needed, _ = (
                self._estimate_reservation(intent) if intent.offset == Offset.OPEN else (Decimal(0), Decimal(0))
            )
        except MissingRuleError as exc:
            return ("reference-price", str(exc))
        live = self.live_orders(intent.instrument, exclude=intent.client_order_id)
        pending_open = sum(o.leaves_qty for o in live if o.offset == Offset.OPEN and o.side == intent.side)
        try:
            execution_day = self._execution_trading_day(intent)
        except MissingRuleError as exc:
            return ("session-gate", str(exc))
        try:
            self.risk_manager.check_order(
                intent,
                self.epoch,
                funds,
                current_pos,
                active_orders=live,
                estimated_margin_needed=margin_needed,
                trading_day=day,
                price_band=band,
                natural_person=self.natural_person,
                now=self.now(),
                pending_open_lots=pending_open,
                execution_trading_day=execution_day,
            )
        except EpochViolationError as exc:
            return ("control-epoch", str(exc))
        except LimitViolationError as exc:
            return ("exchange-limit", str(exc))
        except RiskViolationError as exc:
            return ("risk", str(exc))
        except ValueError as exc:
            return ("risk-input", str(exc))
        return None

    def _execution_trading_day(self, intent: OrderIntent) -> date | None:
        """意图最早可能送达柜台的时刻所属交易日 (FR-RISK-06).

        日线信号在 T 收盘产生、T+1 首个可报单时段送出；节前窗口必须按送出所属交易日判断，
        否则节前最后交易日收盘的意图 (节后才成交) 被拒，而前一日收盘的意图却在节前最后交易日成交并持仓过节。
        无时段门时返回 None，由风控退回账本当前交易日。
        """
        if self.session_gate is None:
            return None
        now = self.now()
        target = self._policy_target(intent.instrument, now)
        earliest = target if target is not None and target > now else now
        submit_at = self.session_gate.next_submit_time(intent.instrument, earliest)
        if submit_at is None:
            return None
        return self.session_gate.trading_day_at(intent.instrument, submit_at)

    def _margin_rates(self) -> dict[InstrumentId, Decimal]:
        rates = {inst: eco.margin_ratio for inst, eco in self._economics.items()}
        if self._default_economics is not None:
            for inst in self._mark_prices:
                rates.setdefault(inst, self._default_economics.margin_ratio)
        return rates

    def _estimate_reservation(self, intent: OrderIntent) -> tuple[Decimal, Decimal]:
        eco = self.economics(intent.instrument)
        if intent.limit_price_ticks is not None:
            price = Decimal(intent.limit_price_ticks) * eco.price_tick
        else:
            price = self._mark_prices.get(intent.instrument)
            if price is None:
                raise MissingRuleError(f"no visible reference price to reserve funds for {intent.instrument}")
        margin = Decimal(0)
        if intent.offset == Offset.OPEN:
            margin = price * eco.multiplier * Decimal(intent.quantity) * eco.margin_ratio
        fee = eco.commission_per_lot * Decimal(intent.quantity)
        return margin, fee

    def _reject_locally(self, client_order_id: str, stage: str, reason: str) -> None:
        order = self.order_manager.get_order(client_order_id)
        if order is None:
            return
        self.position_manager.release_reservation(client_order_id)
        self.ledger.release_funds(client_order_id)
        if order.status in (OrderStatus.CREATED, OrderStatus.SUBMITTING):
            order.apply_send_result(LocalSendResult(state=SendState.NOT_SENT, local_code=-9, evidence=reason))
        else:
            order.status = OrderStatus.REJECTED
        self._record_local_rejection(order, stage, reason)

    def _record_local_rejection(self, order: Order, stage: str, reason: str) -> None:
        """记录本地拒绝并向策略合成一条 REJECTED 回报.

        本地拒绝 (风控、路由、预占、时段、错过执行、发送失败) 没有网关回报；若不通知策略，
        策略按委托跟踪的在途状态会永久卡住，移仓状态机也停在已提交腿 (FR-CON-06, FR-RISK-03)。
        """
        self.rejected_intents.append(
            RejectedIntent(
                client_order_id=order.client_order_id,
                strategy_id=order.strategy_id,
                instrument=order.instrument,
                side=order.side,
                offset=order.offset,
                quantity=order.quantity,
                at=self.now(),
                stage=stage,
                reason=reason,
            )
        )
        identity = order.identity or OrderIdentity(
            account_id=self.account_id,
            exchange=order.instrument.exchange,
            client_order_id=order.client_order_id,
        )
        now = self.now()
        update = OrderUpdate(
            identity=identity,
            instrument=order.instrument,
            side=order.side,
            offset=order.offset,
            status=OrderStatus.REJECTED,
            quantity=order.quantity,
            filled_quantity=min(order.cum_filled_qty, order.quantity),
            event_time=now,
            available_at=now,
        )
        self.local_rejection_reports.append(update)
        for strategy in self.strategies.values():
            strategy.on_order(update)

    # ------------------------------------------------------------------ 执行时点策略 (FR-EXEC-02/03)
    def _policy_target(self, instrument: InstrumentId, after: datetime) -> datetime | None:
        """按执行时点策略求严格晚于 after 的目标时刻；无日历或无可见时段时明确失败."""
        policy = self.execution_policy
        if policy == ExecutionPolicy.NEXT_BAR_OPEN:
            return None
        if self.session_gate is None:
            raise MissingRuleError(f"execution policy {policy} requires a versioned session gate")
        if policy == ExecutionPolicy.NEXT_SESSION_OPEN:
            target = self.session_gate.next_session_open(instrument, after)
        elif policy == ExecutionPolicy.NEXT_DAY_SESSION_OPEN:
            target = self.session_gate.next_session_open(instrument, after, day_session_only=True)
        else:
            assert self.fixed_time_before_close is not None
            close = self.session_gate.next_day_session_close(instrument, after)
            target = close - self.fixed_time_before_close if close is not None else None
        if target is None:
            raise MissingRuleError(f"no visible session after {after.isoformat()} for {instrument} under {policy}")
        return target

    def _target_execution_time(self, intent: OrderIntent) -> datetime | None:
        return self._policy_target(intent.instrument, intent.created_at)

    def _dispatch_intent(self, intent: OrderIntent) -> None:
        now = self.now()
        target = self._target_execution_time(intent)
        if target is not None and target > now:
            self._defer(intent, target, "execution-policy")
            return
        following = self._following_open.get(intent.instrument)
        if self._dispatch_depth > 0 and following is not None and following > now:
            # 对本瞬间回报 (成交 / 状态) 的反应：仅有 OHLC 时不能用本瞬间的开盘价，也不能推断盘中路径；
            # 持有到该合约下一根可观测 Bar 的开盘再送出 (05 §17 "延至下一完整 Bar 评估" 的保守路径)
            self._defer(intent, following, "intrabar-arrival")
            return
        if self._hold_for_session(intent, now):
            return
        self._submit_to_gateway(intent)

    def _hold_for_session(self, intent: OrderIntent, now: datetime) -> bool:
        """本地发送前检查 (FR-CAL-07)：当前时段不允许报单时持有到下一个允许时刻；返回 True 表示已持有或已拒绝."""
        if self.session_gate is None:
            return False
        permissions = self.session_gate.permissions_at(intent.instrument, now)
        if permissions is not None and permissions.submit:
            return False
        next_time = self.session_gate.next_submit_time(intent.instrument, now)
        if next_time is None:
            self._reject_locally(intent.client_order_id, "session-gate", "no visible session permits submission")
            return True
        self._defer(intent, next_time, "session-gate")
        return True

    def _defer(self, intent: OrderIntent, target: datetime, reason: str) -> None:
        self._deferred[intent.client_order_id] = _DeferredIntent(intent=intent, target_time=target, reason=reason)
        self.schedule_timer(target, DEFERRED_TIMER_PREFIX + intent.client_order_id)

    def _submit_to_gateway(self, intent: OrderIntent) -> None:
        if self.gateway is None:
            return
        order = self.order_manager.get_order(intent.client_order_id)
        if order is None or not order.is_active:
            return
        now = self.now()
        sent = replace(intent, created_at=now) if intent.created_at != now else intent
        # 递延到交易日切换之后才送出的平仓意图：今仓已结转为昨仓，预占桶也随之改写；
        # 按当前预占桶重新规划开平标志，而不是把柜台必然拒绝的平今单送出再由内核静默改桶 (FR-ORD-01 / FR-CAL-06)
        reservation = self.position_manager.get_reservation(intent.client_order_id)
        if (
            reservation is not None
            and reservation.offset != sent.offset
            and {reservation.offset, sent.offset} <= {Offset.CLOSE_TODAY, Offset.CLOSE_YESTERDAY}
        ):
            self.offset_rewrites.append(OffsetRewrite(intent.client_order_id, sent.offset, reservation.offset, now))
            sent = replace(sent, offset=reservation.offset)
            order.intent = replace(order.intent, offset=reservation.offset)
        order.mark_submitting(self.epoch.epoch)
        if hasattr(self.gateway, "reactive_submission"):
            # 在分发回报 (成交 / 状态) 期间由策略发出的订单是对该瞬间事件的反应：生效严格晚于该瞬间
            self.gateway.reactive_submission = self._dispatch_depth > 0
        result = self.gateway.submit(sent, self.epoch)
        if hasattr(self.gateway, "reactive_submission"):
            self.gateway.reactive_submission = False
        self.order_manager.record_send_result(intent.client_order_id, result)
        if result.state == SendState.NOT_SENT:
            self.position_manager.release_reservation(intent.client_order_id)
            self.ledger.release_funds(intent.client_order_id)
            self._record_local_rejection(order, "gateway-send", result.evidence)
        self.dispatch_gateway_events()

    def _fire_deferred(self, client_order_id: str) -> None:
        pending = self._deferred.pop(client_order_id, None)
        if pending is None:
            return
        now = self.now()
        next_open = self._next_bar_open
        missed = next_open is not None and now < next_open
        if self.execution_policy == ExecutionPolicy.NEXT_DAY_FIXED_TIME and pending.reason == "execution-policy":
            # 固定时刻必须有恰好在该时刻开始的执行 Bar；分辨率不足不能用更早的 Bar 或未完成的 Bar 替代 (A21)
            missed = next_open != now
        if pending.reason == "execution-policy" and missed:
            # 目标开盘时刻没有对应的执行数据：错过 (FR-EXEC-03)，按配置顺延或取消，不回填过去的开盘
            if (
                self.missed_execution == MissedExecutionPolicy.CANCEL
                or self.session_gate is None
                or pending.attempts >= 5
            ):
                self.missed_executions.append(
                    MissedExecution(client_order_id, pending.target_time, now, self.missed_execution, None)
                )
                self._reject_locally(
                    client_order_id,
                    "missed-execution",
                    f"target {pending.target_time.isoformat()} had no execution data",
                )
                return
            try:
                retry = self._policy_target(pending.intent.instrument, now)
            except MissingRuleError:
                retry = None
            if retry is None:
                self.missed_executions.append(
                    MissedExecution(client_order_id, pending.target_time, now, self.missed_execution, None)
                )
                self._reject_locally(client_order_id, "missed-execution", "no further visible session open to defer to")
                return
            self.missed_executions.append(
                MissedExecution(client_order_id, pending.target_time, now, self.missed_execution, retry)
            )
            pending.attempts += 1
            pending.target_time = retry
            self._deferred[client_order_id] = pending
            self.schedule_timer(retry, DEFERRED_TIMER_PREFIX + client_order_id)
            return
        if pending.reason != "session-gate" and self._hold_for_session(pending.intent, now):
            return
        self._submit_to_gateway(pending.intent)

    def cancel_deferred_intents(self, reason: str) -> None:
        """撤销尚未发往网关的延后意图并释放预占 (回测结束或控制权切换时使用)."""
        for client_order_id in sorted(self._deferred):
            self._reject_locally(client_order_id, "deferred-cancelled", reason)
        self._deferred.clear()

    # ------------------------------------------------------------------ 时钟推进与定时器
    def advance_clock(self, at: datetime, *, next_bar_open: datetime | None = None) -> None:
        """推进虚拟时钟到 at，途中依次触发所有到期定时器 (空行情时段同样推进)."""
        self._next_bar_open = next_bar_open
        while True:
            due = self.clock.next_time()
            if due is None or due > at:
                break
            timer = self.clock.next_event()
            assert timer is not None
            self._on_timer(timer)
        self.clock.advance_to(at)

    def _on_timer(self, timer: TimerEvent) -> None:
        self._ingress_seq += 1
        event = CanonicalEvent(
            event_id=f"timer-{self._ingress_seq}",
            kind=EventKind.TIMER,
            event_time=self.now(),
            available_at=self.now(),
            sequence=self._ingress_seq,
            source_id="engine-clock",
            payload=timer,
        )
        self.processed_events.append(event)
        if timer.timer_id.startswith(DEFERRED_TIMER_PREFIX):
            self._fire_deferred(timer.timer_id[len(DEFERRED_TIMER_PREFIX) :])
            return
        for strategy in self.strategies.values():
            strategy.on_timer(timer)

    # ------------------------------------------------------------------ 回报分发
    def dispatch_gateway_events(self, events: Sequence[CanonicalEvent] | None = None) -> list[CanonicalEvent]:
        """把网关出站事件按可见时间与显式优先级送入内核，并作为一笔 Journal 事务落盘."""
        if events is None:
            events = (
                self.gateway.drain_events()
                if self.gateway is not None and hasattr(self.gateway, "drain_events")
                else ()
            )
        if not events:
            return []
        # 同一批到达的事件按 (可见时刻, 显式优先级, 入站序号) 排序；批次之间保持因果先后
        queue = EventQueue(ReplayOrder.SIMULATED, SIMULATED_EVENT_PRIORITIES)
        for offset, event in enumerate(events, start=1):
            queue.push(replace(event, sequence=offset))
        processed: list[CanonicalEvent] = []
        self._dispatch_depth += 1
        try:
            while (popped := queue.pop()) is not None:
                self._ingress_seq += 1
                event = replace(popped, sequence=self._ingress_seq)
                if event.kind == EventKind.TRADE_REPORT:
                    self.process_trade_event(event)
                elif event.kind == EventKind.ORDER_REPORT:
                    self.process_order_event(event)
                processed.append(event)
        finally:
            self._dispatch_depth -= 1
        self.processed_events.extend(processed)
        self._journal_commit(processed)
        return processed

    def _journal_commit(self, events: Sequence[CanonicalEvent]) -> None:
        if self.journal is None or not events:
            return
        self._tx_counter += 1
        keys = tuple(e.payload.deduplication_key for e in events if isinstance(e.payload, Trade))
        transaction = JournalTransaction(
            transaction_id=f"tx-{self._tx_counter}",
            events=tuple(events),
            cursor_before=self._journal_cursor,
            cursor_after=self._journal_cursor + len(events),
            deduplication_keys=keys,
            state_updates={
                "balance": str(self.ledger.balance),
                "trading_day": str(self.ledger.current_trading_day),
            },
        )
        self.journal.append(transaction)
        self._journal_cursor += len(events)

    def process_order_event(self, event: CanonicalEvent[OrderUpdate]) -> None:
        update: OrderUpdate = event.payload
        order = self.order_manager.process_order_update(update)
        if order is None:
            return
        day = self.ledger.current_trading_day
        if update.status == OrderStatus.ACCEPTED:
            self.risk_manager.on_order_accepted(order.intent, day)
        elif update.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
            unfilled = order.quantity - order.cum_filled_qty
            if update.status == OrderStatus.CANCELLED:
                self.risk_manager.on_order_canceled(order.intent, unfilled, day)
            elif update.status == OrderStatus.REJECTED:
                self.risk_manager.on_order_rejected(order.intent, was_accepted=False, trading_day=day)
            # 释放已确认不会再成交的预占；未入账成交份额继续保留 (A02 / FR-ORD-06)
            self.position_manager.on_order_canceled_or_rejected(order.client_order_id, order.cum_filled_qty)
            fraction = Decimal(order.unaccounted_fill_qty) / Decimal(order.quantity)
            self.ledger.release_funds(order.client_order_id, fraction)
        for strategy in self.strategies.values():
            strategy.on_order(update)

    def process_trade_event(self, event: CanonicalEvent[Trade]) -> None:
        trade: Trade = event.payload
        order, is_new = self.order_manager.process_trade(trade)
        if not is_new:
            return
        cid = order.client_order_id if order else None
        eco = self.economics(trade.instrument)
        commission = self._commission_for(trade, eco)
        self.ledger.on_trade(trade, commission=commission, multiplier=eco.multiplier, client_order_id=cid)
        if order is not None and order.is_terminal and order.unaccounted_fill_qty == 0:
            # 终态且成交全部入账：剩余预占应已由成交路径消耗；确保没有残留
            self.position_manager.on_order_canceled_or_rejected(order.client_order_id, order.cum_filled_qty)
            self.ledger.release_funds(order.client_order_id)
        for strategy in self.strategies.values():
            strategy.on_trade(trade)

    def _commission_for(self, trade: Trade, eco: InstrumentEconomics) -> Decimal:
        if self.rule_engine is None:
            return eco.commission_per_lot * Decimal(trade.quantity)
        # 缺规则或规则冲突时由 RuleStore 抛出核心异常，不静默回退 (FR-RULE, A21)
        return self.rule_engine.evaluate_commission(
            trade.instrument,
            self.commission_profile,
            trade.offset,
            trade.price,
            trade.quantity,
            eco.multiplier,
            effective_at=trade.event_time,
            known_at=trade.available_at,
        )

    # ------------------------------------------------------------------ 查询
    def live_orders(self, instrument: InstrumentId | None = None, *, exclude: str | None = None) -> list[Order]:
        """真正在途的委托：排除已拆分为子单的路由父单与本地拒绝的记录."""
        return [
            o
            for o in self.order_manager.active_orders(instrument)
            if not o.child_order_ids and o.client_order_id != exclude
        ]

    def strategy_of(self, client_order_id: str) -> str:
        found = self._attribution.get(client_order_id)
        return found.strategy_id if found is not None else UNATTRIBUTED_STRATEGY

    def orders(self) -> tuple[Order, ...]:
        return self.order_manager.orders()
