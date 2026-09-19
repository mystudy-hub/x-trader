"""[Gateway 适配器] 模拟撮合网关 (S3-03, FR-MATCH-01~05, FR-EXEC-01~03).

实现 ExecutionPort 协议，支持：
1. Bar 级确定性撮合（开盘撮合与盘中触价撮合）；
2. 涨跌停流动性情景 (LimitLiquidityScenario: 方向保守与触板无成交)；
3. 参与率共享预算 (participation_rate)；
4. 严格时间因果律与滑点跳数；
5. 生成规范的 CanonicalEvent[OrderUpdate] 与 CanonicalEvent[Trade] 事件流。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from qh_trader.core.constants import (
    EventKind,
    Exchange,
    LimitLiquidityScenario,
    Offset,
    OrderStatus,
    OrderType,
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
    Trade,
    TradeKey,
    VersionedValue,
    require_decimal,
    require_enum,
    require_int,
    require_text,
)
from qh_trader.core.ports import ExecutionPort


@dataclass
class _SimulatedOrderState:
    intent: OrderIntent
    identity: OrderIdentity
    status: OrderStatus
    filled_quantity: int = 0

    @property
    def unfilled_quantity(self) -> int:
        return self.intent.quantity - self.filled_quantity

    @property
    def is_active(self) -> bool:
        return self.status in (OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED)


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
        front_id: int = 1,
        session_id: int = 1,
        source_id: str = "simulated-gateway",
    ) -> None:
        require_text(account_id, "account_id")
        require_int(slippage_ticks, "slippage_ticks", 0)
        require_decimal(price_tick, "price_tick", Decimal("0.000001"))
        require_decimal(participation_rate, "participation_rate", Decimal(0))
        require_enum(limit_liquidity_scenario, LimitLiquidityScenario)
        require_text(source_id, "source_id")

        self._account_id = account_id
        self._trading_day = trading_day
        self._slippage_ticks = slippage_ticks
        self._price_tick = price_tick
        self._participation_rate = participation_rate
        self._limit_liquidity_scenario = limit_liquidity_scenario
        self._front_id = front_id
        self._session_id = session_id
        self._source_id = source_id

        self._seq = 0
        self._trade_counter = 0
        self._sys_counter = 0
        self._order_ref_counter = 0

        self._orders: dict[str, _SimulatedOrderState] = {}  # client_order_id -> state
        self._identity_map: dict[str, str] = {}  # order_ref -> client_order_id
        self._events: list[CanonicalEvent] = []

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

    def set_trading_day(self, day: date) -> None:
        self._trading_day = day

    # ------------------------------------------------------------------ ExecutionPort 接口
    def submit(self, order: OrderIntent, epoch: ControlEpoch) -> LocalSendResult:
        """接收订单并立即确认接受（生成 ACCEPTED 事件流）."""
        if not isinstance(order, OrderIntent):
            raise TypeError("order must be an OrderIntent")
        if not isinstance(epoch, ControlEpoch):
            raise TypeError("epoch must be a ControlEpoch")

        client_id = order.client_order_id
        if client_id in self._orders:
            return LocalSendResult(
                state=SendState.NOT_SENT,
                local_code=-1,
                evidence="duplicate client_order_id in simulated gateway",
            )

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

        state = _SimulatedOrderState(
            intent=order,
            identity=identity,
            status=OrderStatus.ACCEPTED,
            filled_quantity=0,
        )
        self._orders[client_id] = state
        self._identity_map[order_ref] = client_id

        # 生成远端接受回报 (OrderUpdate)
        now = order.created_at
        self._next_seq()
        update = OrderUpdate(
            identity=identity,
            instrument=order.instrument,
            side=order.side,
            offset=order.offset,
            status=OrderStatus.ACCEPTED,
            quantity=order.quantity,
            filled_quantity=0,
            event_time=now,
            available_at=now,
        )
        event = CanonicalEvent(
            event_id=f"ord-{self._seq}",
            kind=EventKind.ORDER_REPORT,
            event_time=now,
            available_at=now,
            sequence=self._seq,
            source_id=self._source_id,
            payload=update,
        )
        self._events.append(event)

        return LocalSendResult(
            state=SendState.SENT_UNKNOWN,
            local_code=0,
            evidence="order accepted by simulated gateway",
        )

    def cancel(self, ref: OrderIdentity, epoch: ControlEpoch) -> LocalSendResult:
        """撤单处理：如果订单处于活跃状态则将其置为 CANCELLED."""
        if not isinstance(ref, OrderIdentity):
            raise TypeError("ref must be an OrderIdentity")
        if not isinstance(epoch, ControlEpoch):
            raise TypeError("epoch must be a ControlEpoch")

        client_id: str | None = None
        if ref.client_order_id and ref.client_order_id in self._orders:
            client_id = ref.client_order_id
        elif ref.order_ref and ref.order_ref in self._identity_map:
            client_id = self._identity_map[ref.order_ref]

        if client_id is None or client_id not in self._orders:
            return LocalSendResult(
                state=SendState.NOT_SENT,
                local_code=-1,
                evidence="order not found in simulated gateway",
            )

        state = self._orders[client_id]
        if not state.is_active:
            return LocalSendResult(
                state=SendState.NOT_SENT,
                local_code=-2,
                evidence=f"order not active in simulated gateway, status={state.status}",
            )

        state.status = OrderStatus.CANCELLED
        now = datetime.now(timezone.utc)
        self._next_seq()
        update = OrderUpdate(
            identity=state.identity,
            instrument=state.intent.instrument,
            side=state.intent.side,
            offset=state.intent.offset,
            status=OrderStatus.CANCELLED,
            quantity=state.intent.quantity,
            filled_quantity=state.filled_quantity,
            event_time=now,
            available_at=now,
        )
        event = CanonicalEvent(
            event_id=f"ord-{self._seq}",
            kind=EventKind.ORDER_REPORT,
            event_time=now,
            available_at=now,
            sequence=self._seq,
            source_id=self._source_id,
            payload=update,
        )
        self._events.append(event)

        return LocalSendResult(
            state=SendState.SENT_UNKNOWN,
            local_code=0,
            evidence="cancel accepted by simulated gateway",
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

    # ------------------------------------------------------------------ Bar 撮合
    def match_bar(
        self,
        bar: Bar,
        *,
        upper_limit: Decimal | None = None,
        lower_limit: Decimal | None = None,
    ) -> list[CanonicalEvent]:
        """按 Bar 撮合未完成订单.

        按顺序处理：
        1. 零成交量判断；
        2. 涨跌停流动性与方向限制；
        3. 开盘撮合 (created_at <= bar.open_time 的订单)；
        4. 盘中撮合 (高低价触及限价的订单)；
        5. 共享参与率预算消耗；
        6. 返回本次撮合生成的所有事件流。
        """
        if not isinstance(bar, Bar):
            raise TypeError("bar must be a Bar instance")

        events_before = len(self._events)

        # 1. 零成交量：零量不成交 (A11, A20, FR-MATCH-01)
        if bar.volume <= 0:
            return []

        # 2. 参与率共享预算 (FR-MATCH-04)
        budget = int(Decimal(bar.volume) * self._participation_rate)
        if budget <= 0 and self._participation_rate > 0:
            # 至少支持 1 手，若 volume 极小但 participation_rate 非零
            budget = max(1, int(Decimal(bar.volume) * self._participation_rate))

        # 3. 涨跌停流动性判定 (FR-MATCH-03)
        can_buy = True
        can_sell = True
        is_upper = (upper_limit is not None and bar.close >= upper_limit)
        is_lower = (lower_limit is not None and bar.close <= lower_limit)

        if self._limit_liquidity_scenario == LimitLiquidityScenario.DIRECTION_CONSERVATIVE:
            if is_upper:
                can_buy = False  # 涨停封死，买单不可成交，卖单可成交
            if is_lower:
                can_sell = False  # 跌停封死，卖单不可成交，买单可成交
        elif self._limit_liquidity_scenario == LimitLiquidityScenario.TOUCH_LIMIT_NO_FILL:
            if is_upper or is_lower:
                can_buy = False
                can_sell = False

        # 筛选与本 Bar 合约一致的活跃订单
        active_orders = [
            st for st in self._orders.values()
            if st.is_active and st.intent.instrument == bar.instrument
        ]
        if not active_orders or budget <= 0:
            return []

        # 4. 阶段一：开盘撮合 (针对在 bar.open_time 或之前创建的订单)
        # FR-MATCH-02: 必须在开盘时点前已生效，才能使用该 Bar 的开盘价
        open_time = bar.open_time
        for state in active_orders:
            if budget <= 0:
                break
            order = state.intent
            if order.created_at > open_time:
                continue

            # 撮合判定
            fill_qty = 0
            fill_price = Decimal(0)

            if order.side == Side.BUY:
                if not can_buy:
                    continue
                # 买单限价
                if order.order_type == OrderType.LIMIT:
                    assert order.limit_price_ticks is not None
                    limit_p = Decimal(order.limit_price_ticks) * self._price_tick
                    if bar.open <= limit_p:
                        # 触及开盘价，加滑点
                        raw_fill_p = bar.open + Decimal(self._slippage_ticks) * self._price_tick
                        fill_price = min(limit_p, raw_fill_p)
                        if upper_limit is not None:
                            fill_price = min(fill_price, upper_limit)
                        fill_qty = min(state.unfilled_quantity, budget)
                else:
                    # 市价
                    fill_price = bar.open + Decimal(self._slippage_ticks) * self._price_tick
                    if upper_limit is not None:
                        fill_price = min(fill_price, upper_limit)
                    fill_qty = min(state.unfilled_quantity, budget)

            elif order.side == Side.SELL:
                if not can_sell:
                    continue
                # 卖单限价
                if order.order_type == OrderType.LIMIT:
                    assert order.limit_price_ticks is not None
                    limit_p = Decimal(order.limit_price_ticks) * self._price_tick
                    if bar.open >= limit_p:
                        raw_fill_p = bar.open - Decimal(self._slippage_ticks) * self._price_tick
                        fill_price = max(limit_p, raw_fill_p)
                        if lower_limit is not None:
                            fill_price = max(fill_price, lower_limit)
                        fill_qty = min(state.unfilled_quantity, budget)
                else:
                    # 市价
                    fill_price = bar.open - Decimal(self._slippage_ticks) * self._price_tick
                    if lower_limit is not None:
                        fill_price = max(fill_price, lower_limit)
                    fill_qty = min(state.unfilled_quantity, budget)

            if fill_qty > 0:
                self._execute_fill(state, fill_qty, fill_price, open_time)
                budget -= fill_qty

        # 5. 阶段二：盘中限价撮合 (针对未在开盘全部成交的订单，使用 high/low 撮合)
        if budget > 0:
            match_time = bar.bar_end
            for state in active_orders:
                if budget <= 0 or not state.is_active:
                    continue
                order = state.intent
                if order.order_type != OrderType.LIMIT:
                    continue
                assert order.limit_price_ticks is not None
                limit_p = Decimal(order.limit_price_ticks) * self._price_tick

                fill_qty = 0
                fill_price = Decimal(0)

                if order.side == Side.BUY:
                    if not can_buy:
                        continue
                    # 盘中触价: low <= limit_price
                    if bar.low <= limit_p:
                        fill_price = limit_p
                        if upper_limit is not None:
                            fill_price = min(fill_price, upper_limit)
                        fill_qty = min(state.unfilled_quantity, budget)

                elif order.side == Side.SELL:
                    if not can_sell:
                        continue
                    # 盘中触价: high >= limit_price
                    if bar.high >= limit_p:
                        fill_price = limit_p
                        if lower_limit is not None:
                            fill_price = max(fill_price, lower_limit)
                        fill_qty = min(state.unfilled_quantity, budget)

                if fill_qty > 0:
                    self._execute_fill(state, fill_qty, fill_price, match_time)
                    budget -= fill_qty

        return self._events[events_before:]

    def drain_events(self) -> list[CanonicalEvent]:
        """取出所有未消费的事件流并清空内部队列."""
        evts = list(self._events)
        self._events.clear()
        return evts

    # ------------------------------------------------------------------ 内部辅助
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _execute_fill(
        self,
        state: _SimulatedOrderState,
        fill_qty: int,
        fill_price: Decimal,
        fill_time: datetime,
    ) -> None:
        state.filled_quantity += fill_qty
        if state.filled_quantity >= state.intent.quantity:
            state.status = OrderStatus.FILLED
        else:
            state.status = OrderStatus.PARTIALLY_FILLED

        # 1. 生成 Trade 事件
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
        self._next_seq()
        trade_event = CanonicalEvent(
            event_id=f"trd-{self._seq}",
            kind=EventKind.TRADE_REPORT,
            event_time=fill_time,
            available_at=fill_time,
            sequence=self._seq,
            source_id=self._source_id,
            payload=trade,
        )
        self._events.append(trade_event)

        # 2. 生成 OrderUpdate 事件
        self._next_seq()
        update = OrderUpdate(
            identity=state.identity,
            instrument=state.intent.instrument,
            side=state.intent.side,
            offset=state.intent.offset,
            status=state.status,
            quantity=state.intent.quantity,
            filled_quantity=state.filled_quantity,
            event_time=fill_time,
            available_at=fill_time,
        )
        order_event = CanonicalEvent(
            event_id=f"ord-{self._seq}",
            kind=EventKind.ORDER_REPORT,
            event_time=fill_time,
            available_at=fill_time,
            sequence=self._seq,
            source_id=self._source_id,
            payload=update,
        )
        self._events.append(order_event)
