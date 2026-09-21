"""[Gateway 适配器] 模拟撮合网关 (S3-03, FR-MATCH-01~05, FR-MATCH-07 基础延迟, FR-EXEC-01~03).

实现 ExecutionPort 协议：
1. 报单 / 撤单按"到达时刻"生效，到达时刻 = 请求时刻 + 配置延迟；到达时按时段权限二次核验 (FR-CAL-07)；
2. Bar 级确定性撮合分两相：开盘候选只用开盘时点信息，盘中候选只在 Bar 结束时评估尚未成交的订单，
   后来的收盘触板不改写已确定的开盘成交 (FR-MATCH-02/03, A20)；
3. 涨跌停流动性情景 (方向保守 / 触板无成交)；参与率共享预算，预算为零不成交 (FR-MATCH-04)；
4. 成交价同时满足限价、Bar 高低价、涨跌停边界与价格步长 (FR-MATCH-04)；
5. 订单当交易日有效 (GFD)，交易日切换时未成交部分过期；
6. 全部回报进入网关出站队列，由引擎按可见时间与显式优先级消费，网关不直接记账。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from qh_trader.core.constants import (
    AuctionFillPolicy,
    EventKind,
    IntrabarTouchRule,
    LimitLiquidityScenario,
    MissingRuleError,
    OrderStatus,
    OrderType,
    PriceType,
    SendState,
    Side,
)
from qh_trader.core.event import CanonicalEvent
from qh_trader.core.objects import (
    Bar,
    Capability,
    CapabilityProfile,
    ControlEpoch,
    InstrumentId,
    LocalSendResult,
    OrderIdentity,
    OrderIntent,
    OrderUpdate,
    Permissions,
    Trade,
    TradeKey,
    VersionedValue,
    require_decimal,
    require_enum,
    require_int,
    require_text,
)
from qh_trader.core.ports import ClockPort, ExecutionPort, MarketDataPort, SessionGatePort


@dataclass
class _SimulatedOrderState:
    intent: OrderIntent
    identity: OrderIdentity
    status: OrderStatus
    effective_at: datetime
    order_ref_int: int
    filled_quantity: int = 0

    @property
    def unfilled_quantity(self) -> int:
        return self.intent.quantity - self.filled_quantity

    @property
    def is_active(self) -> bool:
        return self.status in (OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED)


@dataclass(frozen=True, slots=True)
class ExecutionDegradation:
    """开盘执行参考价缺失或分辨率不足时的降级记录 (A21, A25-07)：不回填、不插值，只记录."""

    instrument: InstrumentId
    at: datetime
    session_id: str | None
    price_type: str
    reason: str
    action: str  # "open_candidates_skipped"


@dataclass(frozen=True, slots=True)
class MatchingAssumptions:
    """写入运行清单的撮合假设 (FR-MATCH-03/05 报告要求)."""

    slippage_ticks: int
    price_tick: str
    participation_rate: str
    limit_liquidity_scenario: str
    intrabar_touch_rule: str
    auction_fill_policy: str
    order_delay_ms: int
    cancel_delay_ms: int
    order_validity: str
    open_fill_time: str
    intrabar_fill_time: str
    execution_reference: str
    reactive_orders: str = ""


class SimulatedGateway(ExecutionPort):
    """基于 Bar 级行情的模拟撮合网关，满足 ExecutionPort 协议."""

    def __init__(
        self,
        account_id: str,
        trading_day: date,
        *,
        slippage_ticks: int = 0,
        price_tick: Decimal = Decimal("1"),
        participation_rate: Decimal = Decimal("1.0"),
        limit_liquidity_scenario: LimitLiquidityScenario = LimitLiquidityScenario.DIRECTION_CONSERVATIVE,
        intrabar_touch_rule: IntrabarTouchRule = IntrabarTouchRule.TOUCH,
        auction_fill_policy: AuctionFillPolicy = AuctionFillPolicy.ASSUME_PARTICIPATION,
        order_delay: timedelta = timedelta(0),
        cancel_delay: timedelta = timedelta(0),
        clock: ClockPort | None = None,
        session_gate: SessionGatePort | None = None,
        execution_reference_port: MarketDataPort | None = None,
        execution_price_type: PriceType = PriceType.BAR_OPEN,
        strict_execution_reference: bool = False,
        front_id: int = 1,
        session_id: int = 1,
        source_id: str = "simulated-gateway",
    ) -> None:
        require_text(account_id, "account_id")
        require_int(slippage_ticks, "slippage_ticks", 0)
        require_decimal(price_tick, "price_tick", Decimal("0.000001"))
        require_decimal(participation_rate, "participation_rate", Decimal(0))
        if participation_rate > 1:
            raise ValueError("participation_rate cannot exceed the observed bar volume")
        require_enum(limit_liquidity_scenario, LimitLiquidityScenario)
        require_enum(intrabar_touch_rule, IntrabarTouchRule)
        require_enum(auction_fill_policy, AuctionFillPolicy)
        require_text(source_id, "source_id")
        if not isinstance(order_delay, timedelta) or not isinstance(cancel_delay, timedelta):
            raise TypeError("delays must be timedelta values")
        if order_delay < timedelta(0) or cancel_delay < timedelta(0):
            raise ValueError("delays cannot be negative")

        self._account_id = account_id
        self._trading_day = trading_day
        self._slippage_ticks = slippage_ticks
        self._price_tick = price_tick
        self._participation_rate = participation_rate
        self._limit_liquidity_scenario = limit_liquidity_scenario
        self._intrabar_touch_rule = intrabar_touch_rule
        self._auction_fill_policy = auction_fill_policy
        self._order_delay = order_delay
        self._cancel_delay = cancel_delay
        self._clock = clock
        self._session_gate = session_gate
        require_enum(execution_price_type, PriceType)
        self._execution_reference_port = execution_reference_port
        self._execution_price_type = execution_price_type
        self._strict_execution_reference = strict_execution_reference
        self.degradations: list[ExecutionDegradation] = []
        self.execution_references_used = 0
        self._front_id = front_id
        self._session_id = session_id
        self._source_id = source_id

        self._seq = 0
        self._trade_counter = 0
        self._order_ref_counter = 0
        self._last_time: datetime | None = None
        #: 引擎在分发回报期间置 True：此时提交的订单是对该瞬间事件的反应，生效严格晚于该瞬间
        self.reactive_submission = False

        self._orders: dict[str, _SimulatedOrderState] = {}  # client_order_id -> state
        self._identity_map: dict[str, str] = {}  # order_ref -> client_order_id
        self._pending_cancels: list[tuple[datetime, int, str]] = []  # (arrival, seq, client_order_id)
        self._events: list[CanonicalEvent] = []

    # ------------------------------------------------------------------ 属性
    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def trading_day(self) -> date:
        return self._trading_day

    @property
    def slippage_ticks(self) -> int:
        return self._slippage_ticks

    @property
    def price_tick(self) -> Decimal:
        return self._price_tick

    @property
    def participation_rate(self) -> Decimal:
        return self._participation_rate

    @property
    def limit_liquidity_scenario(self) -> LimitLiquidityScenario:
        return self._limit_liquidity_scenario

    def assumptions(self) -> MatchingAssumptions:
        return MatchingAssumptions(
            slippage_ticks=self._slippage_ticks,
            price_tick=str(self._price_tick),
            participation_rate=str(self._participation_rate),
            limit_liquidity_scenario=self._limit_liquidity_scenario.value,
            intrabar_touch_rule=self._intrabar_touch_rule.value,
            auction_fill_policy=self._auction_fill_policy.value,
            order_delay_ms=int(self._order_delay.total_seconds() * 1000),
            cancel_delay_ms=int(self._cancel_delay.total_seconds() * 1000),
            order_validity="GOOD_FOR_TRADING_DAY",
            open_fill_time="bar.open_time",
            intrabar_fill_time="bar.bar_end (approximation: OHLC gives no intrabar path)",
            reactive_orders="orders submitted while reports of instant t are being processed become effective "
            "strictly after t; with OHLC-only data they are evaluated from the next bar",
            execution_reference=self._execution_reference_label(),
        )

    def _execution_reference_label(self) -> str:
        if self._execution_reference_port is None:
            return "bar.open (no execution reference port bound)"
        mode = "strict" if self._strict_execution_reference else "skip open candidates when missing"
        return f"{self._execution_price_type.value} via MarketDataPort.execution_reference ({mode})"

    def set_trading_day(self, day: date) -> None:
        self._trading_day = day

    def bind_clock(self, clock: ClockPort) -> None:
        self._clock = clock

    def bind_session_gate(self, gate: SessionGatePort | None) -> None:
        self._session_gate = gate

    @property
    def session_gate(self) -> SessionGatePort | None:
        return self._session_gate

    def active_orders(self) -> tuple[OrderIntent, ...]:
        return tuple(st.intent for st in self._orders.values() if st.is_active)

    # ------------------------------------------------------------------ 时间与权限
    def _now(self, fallback: datetime) -> datetime:
        now = self._clock.now() if self._clock is not None else fallback
        if self._last_time is not None and now < self._last_time:
            raise ValueError("simulated gateway time cannot move backwards")
        self._last_time = now
        return now

    def _permissions(self, instrument: InstrumentId, at: datetime) -> Permissions:
        """无时段门时视为连续交易 (工程样例)；有时段门时无登记时段不授予任何权限."""
        if self._session_gate is None:
            return Permissions(True, True, True)
        found = self._session_gate.permissions_at(instrument, at)
        return found if found is not None else Permissions(False, False, False)

    # ------------------------------------------------------------------ ExecutionPort 接口
    def submit(self, order: OrderIntent, epoch: ControlEpoch) -> LocalSendResult:
        """接收订单；到达时刻按时段权限核验，接受或拒绝均生成回报事件."""
        if not isinstance(order, OrderIntent):
            raise TypeError("order must be an OrderIntent")
        if not isinstance(epoch, ControlEpoch):
            raise TypeError("epoch must be a ControlEpoch")
        if order.order_type == OrderType.LIMIT and order.limit_price_ticks is None:
            return LocalSendResult(state=SendState.NOT_SENT, local_code=-4, evidence="limit order without price")

        client_id = order.client_order_id
        if client_id in self._orders:
            return LocalSendResult(
                state=SendState.NOT_SENT,
                local_code=-1,
                evidence="duplicate client_order_id in simulated gateway",
            )

        sent_at = self._now(order.created_at)
        if sent_at < order.created_at:
            raise ValueError("an order cannot be sent before it was created")
        arrival_at = sent_at + self._order_delay
        if self.reactive_submission:
            # 对同一瞬间的回报做出反应而发出的订单：生效时刻严格晚于该瞬间。仅有 OHLC 时，
            # 它不能参与该瞬间开盘的候选成交，而是延至下一完整 Bar 评估 (05 §17 保守路径)。
            arrival_at = arrival_at + timedelta(microseconds=1)

        self._order_ref_counter += 1
        order_ref = str(self._order_ref_counter)
        identity = OrderIdentity(
            account_id=self._account_id,
            exchange=order.instrument.exchange,
            client_order_id=client_id,
            exchange_order_id=f"EX-{order_ref}",
            front_id=self._front_id,
            session_id=self._session_id,
            order_ref=order_ref,
        )

        permissions = self._permissions(order.instrument, arrival_at)
        status = OrderStatus.ACCEPTED if permissions.submit else OrderStatus.REJECTED
        state = _SimulatedOrderState(
            intent=order,
            identity=identity,
            status=status,
            effective_at=arrival_at,
            order_ref_int=self._order_ref_counter,
        )
        self._orders[client_id] = state
        self._identity_map[order_ref] = client_id
        self._emit_order_update(state, arrival_at)

        evidence = (
            "order accepted by simulated gateway"
            if permissions.submit
            else "order rejected on arrival: phase does not permit submission"
        )
        return LocalSendResult(state=SendState.SENT_UNKNOWN, local_code=0, evidence=evidence)

    def cancel(self, ref: OrderIdentity, epoch: ControlEpoch) -> LocalSendResult:
        """撤单请求：按到达时刻核验权限；被拒绝不改变原单，被接受则在到达时刻生效."""
        if not isinstance(ref, OrderIdentity):
            raise TypeError("ref must be an OrderIdentity")
        if not isinstance(epoch, ControlEpoch):
            raise TypeError("epoch must be a ControlEpoch")

        client_id: str | None = None
        if ref.client_order_id and ref.client_order_id in self._orders:
            client_id = ref.client_order_id
        elif ref.order_ref and ref.order_ref in self._identity_map:
            client_id = self._identity_map[ref.order_ref]

        if client_id is None:
            return LocalSendResult(
                state=SendState.NOT_SENT, local_code=-1, evidence="order not found in simulated gateway"
            )

        state = self._orders[client_id]
        if not state.is_active:
            return LocalSendResult(
                state=SendState.NOT_SENT,
                local_code=-2,
                evidence=f"order not active in simulated gateway, status={state.status}",
            )

        sent_at = self._now(state.effective_at)
        arrival_at = sent_at + self._cancel_delay
        permissions = self._permissions(state.intent.instrument, arrival_at)
        if not permissions.cancel:
            # 到达时进入禁止撤单阶段：拒绝，不生成生效事件，原单与预占保持 (A25-04, FR-MATCH-07)
            return LocalSendResult(
                state=SendState.NOT_SENT,
                local_code=-3,
                evidence=f"cancel rejected on arrival at {arrival_at.isoformat()}: phase does not permit cancel",
            )

        self._seq += 1
        self._pending_cancels.append((arrival_at, self._seq, client_id))
        self._pending_cancels.sort(key=lambda item: (item[0], item[1]))
        # 零延迟撤单立即生效；带延迟的撤单在撮合推进到到达时刻时生效
        self.apply_pending_cancels(sent_at)
        return LocalSendResult(
            state=SendState.SENT_UNKNOWN, local_code=0, evidence="cancel accepted by simulated gateway"
        )

    def capabilities(self) -> VersionedValue[CapabilityProfile]:
        """返回仿真环境能力表."""
        profile = CapabilityProfile(
            profile_id="simulated-gateway-profile",
            ctp_version="simulated",
            values={
                "close_today_support": Capability(value=True, verified=True, evidence_ref="simulated-profile"),
                "close_yesterday_support": Capability(value=True, verified=True, evidence_ref="simulated-profile"),
                "unified_close_support": Capability(value=True, verified=True, evidence_ref="simulated-profile"),
            },
        )
        now = datetime(2024, 1, 1, tzinfo=timezone.utc)
        return VersionedValue(
            value=profile,
            source_id=self._source_id,
            version="v1.0",
            effective_from=now,
            available_at=now,
        )

    # ------------------------------------------------------------------ 撤单与过期推进
    def apply_pending_cancels(self, until: datetime) -> None:
        """使到达时刻 <= until 的撤单生效 (到达 != 生效：生效才生成 CANCELLED 回报)."""
        remaining: list[tuple[datetime, int, str]] = []
        for arrival_at, seq, client_id in self._pending_cancels:
            if arrival_at > until:
                remaining.append((arrival_at, seq, client_id))
                continue
            state = self._orders[client_id]
            if state.is_active:
                state.status = OrderStatus.CANCELLED
                self._emit_order_update(state, arrival_at)
        self._pending_cancels = remaining

    def expire_orders(self, at: datetime) -> list[CanonicalEvent]:
        """交易日切换：当日有效订单的未成交部分过期 (GFD)."""
        before = len(self._events)
        self.apply_pending_cancels(at)
        for state in sorted(self._orders.values(), key=lambda st: st.order_ref_int):
            if state.is_active:
                state.status = OrderStatus.EXPIRED
                self._emit_order_update(state, at)
        self._pending_cancels = []
        return self._take_since(before)

    # ------------------------------------------------------------------ Bar 撮合
    def match_bar(
        self,
        bar: Bar,
        *,
        upper_limit: Decimal | None = None,
        lower_limit: Decimal | None = None,
    ) -> list[CanonicalEvent]:
        """按 Bar 撮合未完成订单，返回本次新增的回报事件.

        相一：开盘候选。只考虑在 open_time 前已生效的订单，只用 open 与开盘时点的涨跌停状态。
        相二：Bar 结束估算。只对开盘未成交且在 open_time 前已生效的限价单，用高低价评估；
              在 Bar 内才生效的订单延至下一完整 Bar (保守路径)。
        """
        if not isinstance(bar, Bar):
            raise TypeError("bar must be a Bar instance")
        if bar.instrument.__class__.__name__ != "InstrumentId":
            raise TypeError("only actual contracts can be matched")
        if upper_limit is not None:
            require_decimal(upper_limit, "upper_limit")
        if lower_limit is not None:
            require_decimal(lower_limit, "lower_limit")

        events_before = len(self._events)
        self._last_time = max(self._last_time, bar.open_time) if self._last_time else bar.open_time

        # 到达时刻不晚于开盘的撤单先生效
        self.apply_pending_cancels(bar.open_time)

        if bar.volume <= 0:
            # 零成交量：不成交，也不推断盘中路径 (A11, A20)
            self.apply_pending_cancels(bar.bar_end)
            return self._take_since(events_before)

        budget = int((Decimal(bar.volume) * self._participation_rate).to_integral_value(rounding=ROUND_DOWN))
        if budget <= 0:
            self.apply_pending_cancels(bar.bar_end)
            return self._take_since(events_before)

        # ---- 相一：开盘候选 (FR-MATCH-02)
        open_permitted = self._permissions(bar.instrument, bar.open_time).match
        auction_blocked = bar.includes_auction and self._auction_fill_policy == AuctionFillPolicy.REJECT
        candidates = [st for st in self._ordered_active(bar.instrument) if st.effective_at <= bar.open_time]
        # 只有存在开盘候选时才需要开盘执行参考价；没有候选不查询、不记录降级
        open_price = self._open_reference_price(bar) if candidates and open_permitted and not auction_blocked else None

        if open_price is not None and open_permitted and not auction_blocked:
            can_buy_open, can_sell_open = self._limit_liquidity(open_price, upper_limit, lower_limit)
            for state in candidates:
                if budget <= 0:
                    break
                fill_price = self._open_candidate_price(state.intent, bar, upper_limit, lower_limit, open_price)
                if fill_price is None:
                    continue
                if state.intent.side == Side.BUY and not can_buy_open:
                    continue
                if state.intent.side == Side.SELL and not can_sell_open:
                    continue
                fill_qty = min(state.unfilled_quantity, budget)
                self._execute_fill(state, fill_qty, fill_price, bar.open_time)
                budget -= fill_qty

        # 开盘之后、Bar 结束之前到达的撤单：保守地视为在盘中评估前生效
        self.apply_pending_cancels(bar.bar_end)

        # ---- 相二：Bar 结束估算 (只评估尚未成交、且整根 Bar 内均已生效的限价单)；权限看 Bar 内最后一刻
        if budget > 0 and self._permissions(bar.instrument, bar.bar_end - timedelta(microseconds=1)).match:
            can_buy_close, can_sell_close = self._limit_liquidity(bar.close, upper_limit, lower_limit)
            for state in self._ordered_active(bar.instrument):
                if budget <= 0:
                    break
                order = state.intent
                if order.order_type != OrderType.LIMIT or state.effective_at > bar.open_time:
                    continue
                fill_price = self._intrabar_candidate_price(order, bar, upper_limit, lower_limit)
                if fill_price is None:
                    continue
                if order.side == Side.BUY and not can_buy_close:
                    continue
                if order.side == Side.SELL and not can_sell_close:
                    continue
                fill_qty = min(state.unfilled_quantity, budget)
                self._execute_fill(state, fill_qty, fill_price, bar.bar_end)
                budget -= fill_qty

        return self._take_since(events_before)

    def drain_events(self) -> list[CanonicalEvent]:
        """取出所有未消费的回报事件并清空出站队列."""
        events = list(self._events)
        self._events.clear()
        return events

    def _take_since(self, index: int) -> list[CanonicalEvent]:
        """取出自 index 起新增的事件并从出站队列移除，避免被 drain_events 重复投递."""
        taken = self._events[index:]
        del self._events[index:]
        return taken

    # ------------------------------------------------------------------ 撮合辅助
    def _ordered_active(self, instrument: InstrumentId) -> list[_SimulatedOrderState]:
        return sorted(
            (st for st in self._orders.values() if st.is_active and st.intent.instrument == instrument),
            key=lambda st: (st.effective_at, st.order_ref_int),
        )

    def _limit_liquidity(
        self,
        reference_price: Decimal,
        upper_limit: Decimal | None,
        lower_limit: Decimal | None,
    ) -> tuple[bool, bool]:
        """按候选时点价格与流动性情景给出 (可买, 可卖)."""
        at_upper = upper_limit is not None and reference_price >= upper_limit
        at_lower = lower_limit is not None and reference_price <= lower_limit
        if self._limit_liquidity_scenario == LimitLiquidityScenario.DIRECTION_CONSERVATIVE:
            return (not at_upper, not at_lower)
        if at_upper or at_lower:
            return (False, False)
        return (True, True)

    def _limit_price(self, order: OrderIntent) -> Decimal:
        assert order.limit_price_ticks is not None
        return Decimal(order.limit_price_ticks) * self._price_tick

    def _round_to_tick(self, price: Decimal, side: Side) -> Decimal:
        """成交价对齐价格步长：买单向上、卖单向下取整 (不利方向)."""
        ticks = price / self._price_tick
        rounding = ROUND_HALF_UP
        rounded = ticks.to_integral_value(rounding=rounding)
        if rounded != ticks:
            rounded = ticks.to_integral_value(rounding="ROUND_CEILING" if side == Side.BUY else "ROUND_FLOOR")
        return rounded * self._price_tick

    def _bound_price(
        self,
        price: Decimal,
        side: Side,
        bar: Bar,
        upper_limit: Decimal | None,
        lower_limit: Decimal | None,
    ) -> Decimal:
        """成交价不能超出该 Bar 实际价格域与涨跌停边界."""
        price = min(max(price, bar.low), bar.high)
        if upper_limit is not None:
            price = min(price, upper_limit)
        if lower_limit is not None:
            price = max(price, lower_limit)
        price = self._round_to_tick(price, side)
        return min(max(price, bar.low), bar.high)

    def _open_reference_price(self, bar: Bar) -> Decimal | None:
        """开盘候选价：绑定执行参考价端口时必须取到该时点、该时段、该类型且当时可见的观测，否则降级."""
        if self._execution_reference_port is None:
            return bar.open
        session_id = bar.meta.session_id
        reason: str | None = None
        reference = None
        if session_id is None:
            reason = "bar has no session_id; cannot identify the opening observation"
        else:
            reference = self._execution_reference_port.execution_reference(
                bar.instrument, session_id, bar.open_time, self._execution_price_type, bar.open_time
            )
            if reference is None:
                reason = f"no visible {self._execution_price_type.value} observation at {bar.open_time.isoformat()}"
        if reference is None:
            assert reason is not None
            if self._strict_execution_reference:
                raise MissingRuleError(f"execution reference missing for {bar.instrument}: {reason}")
            self.degradations.append(
                ExecutionDegradation(
                    instrument=bar.instrument,
                    at=bar.open_time,
                    session_id=session_id,
                    price_type=self._execution_price_type.value,
                    reason=reason,
                    action="open_candidates_skipped",
                )
            )
            return None
        self.execution_references_used += 1
        return reference.price

    def _open_candidate_price(
        self,
        order: OrderIntent,
        bar: Bar,
        upper_limit: Decimal | None,
        lower_limit: Decimal | None,
        open_price: Decimal,
    ) -> Decimal | None:
        slip = Decimal(self._slippage_ticks) * self._price_tick
        if order.side == Side.BUY:
            raw = open_price + slip
            if order.order_type == OrderType.LIMIT:
                limit_p = self._limit_price(order)
                if open_price > limit_p:
                    return None
                raw = min(raw, limit_p)
            return self._bound_price(raw, order.side, bar, upper_limit, lower_limit)
        raw = open_price - slip
        if order.order_type == OrderType.LIMIT:
            limit_p = self._limit_price(order)
            if open_price < limit_p:
                return None
            raw = max(raw, limit_p)
        return self._bound_price(raw, order.side, bar, upper_limit, lower_limit)

    def _intrabar_candidate_price(
        self,
        order: OrderIntent,
        bar: Bar,
        upper_limit: Decimal | None,
        lower_limit: Decimal | None,
    ) -> Decimal | None:
        limit_p = self._limit_price(order)
        margin = self._price_tick if self._intrabar_touch_rule == IntrabarTouchRule.CROSS_ONE_TICK else Decimal(0)
        if order.side == Side.BUY:
            if bar.low > limit_p - margin:
                return None
        elif bar.high < limit_p + margin:
            return None
        return self._bound_price(limit_p, order.side, bar, upper_limit, lower_limit)

    # ------------------------------------------------------------------ 事件生成
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _emit_order_update(self, state: _SimulatedOrderState, at: datetime) -> None:
        seq = self._next_seq()
        update = OrderUpdate(
            identity=state.identity,
            instrument=state.intent.instrument,
            side=state.intent.side,
            offset=state.intent.offset,
            status=state.status,
            quantity=state.intent.quantity,
            filled_quantity=state.filled_quantity,
            event_time=at,
            available_at=at,
        )
        self._events.append(
            CanonicalEvent(
                event_id=f"ord-{seq}",
                kind=EventKind.ORDER_REPORT,
                event_time=at,
                available_at=at,
                sequence=seq,
                source_id=self._source_id,
                payload=update,
            )
        )

    def _execute_fill(
        self,
        state: _SimulatedOrderState,
        fill_qty: int,
        fill_price: Decimal,
        fill_time: datetime,
    ) -> None:
        if fill_qty <= 0:
            return
        state.filled_quantity += fill_qty
        state.status = (
            OrderStatus.FILLED if state.filled_quantity >= state.intent.quantity else OrderStatus.PARTIALLY_FILLED
        )

        self._trade_counter += 1
        trade_id = f"T{self._trade_counter}"
        day = self._trading_day
        trade = Trade(
            account_id=self._account_id,
            instrument=state.intent.instrument,
            trading_day=day,
            trade_id=trade_id,
            side=state.intent.side,
            offset=state.intent.offset,
            quantity=fill_qty,
            price=fill_price,
            event_time=fill_time,
            available_at=fill_time,
            deduplication_key=TradeKey(self._account_id, state.intent.instrument.exchange, day, trade_id),
            order_identity=state.identity,
        )
        seq = self._next_seq()
        self._events.append(
            CanonicalEvent(
                event_id=f"trd-{seq}",
                kind=EventKind.TRADE_REPORT,
                event_time=fill_time,
                available_at=fill_time,
                sequence=seq,
                source_id=self._source_id,
                payload=trade,
            )
        )
        self._emit_order_update(state, fill_time)
