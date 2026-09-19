"""Unit tests for SimulatedGateway (S3-03, FR-MATCH-01~05, FR-MATCH-07, FR-CAL-07, A11, A20, A25-04, A25-07)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from qh_trader.core.constants import (
    AuctionFillPolicy,
    EventKind,
    Exchange,
    IntrabarTouchRule,
    LimitLiquidityScenario,
    MarketPhase,
    Offset,
    OrderStatus,
    OrderType,
    SendState,
    Side,
)
from qh_trader.core.objects import (
    Bar,
    ControlEpoch,
    InstrumentId,
    OrderIdentity,
    OrderIntent,
    Permissions,
    RecordMeta,
    Session,
)
from qh_trader.core.ports import ExecutionPort, SessionGatePort, SimulatedExecutionPort
from qh_trader.data.calendar import CHINA_TZ, TradingCalendar
from qh_trader.data.session_gate import CalendarSessionGate
from qh_trader.gateway.simulated_gateway import SimulatedGateway

RB_INST = InstrumentId(Exchange.SHFE, "rb2410")
EPOCH = ControlEpoch("controller-1", 1)
TRADING_DAY = date(2024, 9, 10)
BASE_TIME = datetime(2024, 9, 10, 1, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def set(self, now: datetime) -> None:
        self._now = now

    def schedule(self, at: datetime, event) -> None:  # pragma: no cover - not used here
        raise NotImplementedError


def make_bar(
    *,
    open_: str = "3000",
    high: str = "3050",
    low: str = "2980",
    close: str = "3020",
    volume: int = 100,
    open_time: datetime | None = None,
    bar_start: datetime | None = None,
    bar_end: datetime | None = None,
    includes_auction: bool = False,
    trading_day: date = TRADING_DAY,
) -> Bar:
    start = bar_start or BASE_TIME
    end = bar_end or (start + timedelta(hours=1))
    meta = RecordMeta(
        event_time=end,
        available_at=end,
        ingested_at=end,
        trading_day=trading_day,
        source_id="test",
        source_version="v1",
        ingest_seq=1,
    )
    return Bar(
        instrument=RB_INST,
        meta=meta,
        bar_start=start,
        bar_end=end,
        open_time=open_time or start,
        interval="1h",
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
        turnover=Decimal("1000000"),
        open_interest=50000,
        includes_auction=includes_auction,
    )


def intent(
    cid: str,
    side: Side,
    qty: int = 1,
    *,
    limit: int | None = None,
    created_at: datetime = BASE_TIME,
    offset: Offset = Offset.OPEN,
) -> OrderIntent:
    return OrderIntent(
        client_order_id=cid,
        account_id="acc1",
        strategy_id="strat-1",
        instrument=RB_INST,
        side=side,
        offset=offset,
        quantity=qty,
        order_type=OrderType.LIMIT if limit is not None else OrderType.MARKET,
        limit_price_ticks=limit,
        created_at=created_at,
    )


def trades(events) -> list:
    return [e.payload for e in events if e.kind == EventKind.TRADE_REPORT]


def statuses(events) -> list[OrderStatus]:
    return [e.payload.status for e in events if e.kind == EventKind.ORDER_REPORT]


# ---------------------------------------------------------------------- 端口与回报流


def test_simulated_gateway_implements_execution_ports() -> None:
    gw = SimulatedGateway("acc1", TRADING_DAY)
    assert isinstance(gw, ExecutionPort)
    assert isinstance(gw, SimulatedExecutionPort)
    assert gw.capabilities().value.profile_id == "simulated-gateway-profile"
    assumptions = gw.assumptions()
    assert assumptions.order_validity == "GOOD_FOR_TRADING_DAY"
    assert assumptions.limit_liquidity_scenario == LimitLiquidityScenario.DIRECTION_CONSERVATIVE.value


def test_submit_and_cancel_emit_reports_exactly_once() -> None:
    gw = SimulatedGateway("acc1", TRADING_DAY)
    assert gw.submit(intent("ord-1", Side.BUY, 5, limit=3000), EPOCH).state == SendState.SENT_UNKNOWN
    accepted = gw.drain_events()
    assert statuses(accepted) == [OrderStatus.ACCEPTED]
    assert accepted[0].event_time == BASE_TIME  # 虚拟时间，不是墙钟

    ref = OrderIdentity(account_id="acc1", exchange=Exchange.SHFE, client_order_id="ord-1")
    assert gw.cancel(ref, EPOCH).state == SendState.SENT_UNKNOWN
    cancelled = gw.drain_events()
    assert statuses(cancelled) == [OrderStatus.CANCELLED]
    assert gw.drain_events() == []
    # 已撤订单不再参与撮合，撮合返回的事件也不会再出现在 drain_events 里
    assert gw.match_bar(make_bar()) == []
    assert gw.drain_events() == []


# ---------------------------------------------------------------------- A11：零量、因果、盘中到达


def test_zero_volume_no_fill() -> None:
    gw = SimulatedGateway("acc1", TRADING_DAY)
    gw.submit(intent("ord-zero", Side.BUY, 5, limit=3000), EPOCH)
    gw.drain_events()
    assert gw.match_bar(make_bar(volume=0)) == []


def test_order_effective_after_open_cannot_use_that_open_or_intrabar_path() -> None:
    """Bar 内才生效的订单：不能用该 Bar 开盘价，也不能用该 Bar 高低价；延至下一完整 Bar (保守路径)."""
    gw = SimulatedGateway("acc1", TRADING_DAY)
    mid_bar = BASE_TIME + timedelta(minutes=30)
    gw.submit(intent("late", Side.BUY, 1, limit=2985, created_at=mid_bar), EPOCH)
    gw.drain_events()
    first = make_bar(open_="3000", low="2980", high="3050", close="3020")
    assert trades(gw.match_bar(first)) == []
    second = make_bar(open_="3020", low="2980", high="3050", close="3000", bar_start=BASE_TIME + timedelta(hours=1))
    fills = trades(gw.match_bar(second))
    assert [(t.price, t.event_time) for t in fills] == [(Decimal("2985"), second.bar_end)]


def test_open_fill_with_slippage_bounded_by_limit_and_bar_range() -> None:
    gw = SimulatedGateway("acc1", TRADING_DAY, slippage_ticks=2, price_tick=Decimal("1"))
    gw.submit(intent("ord-open", Side.BUY, 3, limit=3010), EPOCH)
    gw.drain_events()
    evts = gw.match_bar(make_bar(open_="3000", volume=100))
    (trade,) = trades(evts)
    assert (trade.quantity, trade.price, trade.event_time) == (3, Decimal("3002"), BASE_TIME)
    assert statuses(evts) == [OrderStatus.FILLED]

    # 滑点不能把成交价推出该 Bar 的实际价格域 (FR-MATCH-04)
    gw2 = SimulatedGateway("acc1", TRADING_DAY, slippage_ticks=5)
    gw2.submit(intent("mkt", Side.BUY, 1), EPOCH)
    gw2.drain_events()
    (trade,) = trades(gw2.match_bar(make_bar(open_="3000", high="3000", low="2980", close="2990")))
    assert trade.price == Decimal("3000")


def test_fill_price_aligned_to_price_tick_in_unfavourable_direction() -> None:
    gw = SimulatedGateway("acc1", TRADING_DAY, price_tick=Decimal("5"), slippage_ticks=0)
    gw.submit(intent("b", Side.BUY, 1), EPOCH)
    gw.submit(intent("s", Side.SELL, 1), EPOCH)
    gw.drain_events()
    fills = {
        t.side: t.price for t in trades(gw.match_bar(make_bar(open_="3002", high="3010", low="2995", close="3000")))
    }
    assert fills[Side.BUY] == Decimal("3005")
    assert fills[Side.SELL] == Decimal("3000")


# ---------------------------------------------------------------------- FR-MATCH-04：共享预算


def test_participation_rate_budget_is_shared_and_never_rounded_up() -> None:
    gw = SimulatedGateway("acc1", TRADING_DAY, participation_rate=Decimal("0.1"))
    gw.submit(intent("a", Side.BUY, 4, limit=3010), EPOCH)
    gw.submit(intent("b", Side.BUY, 4, limit=3010), EPOCH)
    gw.drain_events()
    evts = gw.match_bar(make_bar(open_="3000", volume=50))  # 预算 5 手
    fills = trades(evts)
    assert [(t.order_identity.client_order_id, t.quantity) for t in fills] == [("a", 4), ("b", 1)]
    assert sum(t.quantity for t in fills) == 5

    tiny = SimulatedGateway("acc1", TRADING_DAY, participation_rate=Decimal("0.001"))
    tiny.submit(intent("c", Side.BUY, 5), EPOCH)
    tiny.drain_events()
    assert trades(tiny.match_bar(make_bar(volume=50))) == []  # 预算 0.05 手 -> 0，不凑整为 1

    with pytest.raises(ValueError):
        SimulatedGateway("acc1", TRADING_DAY, participation_rate=Decimal("1.5"))


# ---------------------------------------------------------------------- A20：开盘候选与收盘触板分离


@pytest.mark.parametrize("scenario", list(LimitLiquidityScenario))
def test_a20_open_fill_unchanged_when_only_later_close_touches_limit(scenario: LimitLiquidityScenario) -> None:
    """固定开盘输入，仅改变随后收盘是否触板：既有开盘成交必须不变."""
    results = {}
    for label, close in (("touch", "3100"), ("no_touch", "3050")):
        gw = SimulatedGateway("acc1", TRADING_DAY, limit_liquidity_scenario=scenario)
        gw.submit(intent("buy", Side.BUY, 1), EPOCH)
        gw.drain_events()
        bar = make_bar(open_="3000", high="3100", low="3000", close=close)
        results[label] = [
            (t.price, t.quantity, t.event_time) for t in trades(gw.match_bar(bar, upper_limit=Decimal("3100")))
        ]
    assert results["touch"] == results["no_touch"] == [(Decimal("3000"), 1, BASE_TIME)]


def test_a20_open_at_limit_uses_direction_conservative_or_no_fill() -> None:
    limit_bar = make_bar(open_="3400", high="3400", low="3400", close="3400", volume=100)

    cons = SimulatedGateway("acc1", TRADING_DAY, limit_liquidity_scenario=LimitLiquidityScenario.DIRECTION_CONSERVATIVE)
    cons.submit(intent("buy-1", Side.BUY, 2, limit=3500), EPOCH)
    cons.submit(intent("sell-1", Side.SELL, 2, limit=3300), EPOCH)
    cons.drain_events()
    fills = trades(cons.match_bar(limit_bar, upper_limit=Decimal("3400")))
    assert [(t.side, t.price) for t in fills] == [(Side.SELL, Decimal("3400"))]

    nofill = SimulatedGateway("acc1", TRADING_DAY, limit_liquidity_scenario=LimitLiquidityScenario.TOUCH_LIMIT_NO_FILL)
    nofill.submit(intent("buy-1", Side.BUY, 2, limit=3500), EPOCH)
    nofill.submit(intent("sell-1", Side.SELL, 2, limit=3300), EPOCH)
    nofill.drain_events()
    assert trades(nofill.match_bar(limit_bar, upper_limit=Decimal("3400"))) == []


def test_intrabar_touch_rule_cross_one_tick() -> None:
    touch = SimulatedGateway("acc1", TRADING_DAY, intrabar_touch_rule=IntrabarTouchRule.TOUCH)
    cross = SimulatedGateway("acc1", TRADING_DAY, intrabar_touch_rule=IntrabarTouchRule.CROSS_ONE_TICK)
    for gw in (touch, cross):
        gw.submit(intent("buy", Side.BUY, 1, limit=2980), EPOCH)
        gw.drain_events()
    bar = make_bar(open_="3000", low="2980", high="3050", close="3020")  # low 恰触限价
    assert [t.price for t in trades(touch.match_bar(bar))] == [Decimal("2980")]
    assert trades(cross.match_bar(bar)) == []


# ---------------------------------------------------------------------- GFD 过期


def test_unfilled_orders_expire_at_trading_day_roll() -> None:
    gw = SimulatedGateway("acc1", TRADING_DAY)
    gw.submit(intent("far", Side.BUY, 1, limit=2500), EPOCH)
    gw.drain_events()
    assert trades(gw.match_bar(make_bar())) == []
    expired = gw.expire_orders(BASE_TIME + timedelta(hours=6))
    assert statuses(expired) == [OrderStatus.EXPIRED]
    gw.set_trading_day(date(2024, 9, 20))
    later = make_bar(open_="2600", low="2400", high="2650", close="2500", bar_start=BASE_TIME + timedelta(days=10))
    assert gw.match_bar(later) == []


# ---------------------------------------------------------------------- 时段权限与撤单边界 (A25-04 / A25-07)


def make_session_gate() -> SessionGatePort:
    day = date(2024, 9, 10)
    available = datetime(2024, 1, 1, tzinfo=timezone.utc)
    rows = (
        ("auction_submit", time(8, 55), time(8, 59), MarketPhase.AUCTION_SUBMIT, Permissions(True, True, False)),
        ("auction_match", time(8, 59), time(9, 0), MarketPhase.AUCTION_MATCH, Permissions(False, False, True)),
        ("morning", time(9, 0), time(10, 15), MarketPhase.CONTINUOUS, Permissions(True, True, True)),
        ("break", time(10, 15), time(10, 30), MarketPhase.BREAK, Permissions(False, False, False)),
        ("morning2", time(10, 30), time(11, 30), MarketPhase.CONTINUOUS, Permissions(True, True, True)),
    )
    sessions = [
        Session(
            instrument=RB_INST,
            session_id=name,
            trading_day=day,
            start=datetime.combine(day, start, CHINA_TZ),
            end=datetime.combine(day, end, CHINA_TZ),
            phase=phase,
            permissions=perms,
            rule_version="synthetic-v1",
            source_id="synthetic-calendar",
            available_at=available,
        )
        for name, start, end, phase, perms in rows
    ]
    calendar = TradingCalendar(
        sessions,
        trading_days=[day],
        coverage_start=day,
        coverage_end=day,
        version="synthetic-v1",
        source_id="synthetic-calendar",
        available_at=available,
    )
    return CalendarSessionGate(calendar)


def cst(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2024, 9, 10, hh, mm, ss, tzinfo=CHINA_TZ).astimezone(timezone.utc)


def test_calendar_session_gate_conforms_and_is_left_closed() -> None:
    gate = make_session_gate()
    assert isinstance(gate, SessionGatePort)
    assert gate.permissions_at(RB_INST, cst(8, 58, 59)) == Permissions(True, True, False)
    assert gate.permissions_at(RB_INST, cst(8, 59, 0)) == Permissions(False, False, True)
    assert gate.permissions_at(RB_INST, cst(12, 0)) is None
    assert gate.next_submit_time(RB_INST, cst(10, 20)) == cst(10, 30)
    assert gate.next_session_open(RB_INST, cst(9, 30)) == cst(10, 30)


@pytest.mark.parametrize(
    ("delay_seconds", "cancel_accepted"),
    [(1, True), (2, False), (3, False)],  # 到达 08:58:59 / 08:59:00 / 08:59:01
)
def test_a25_04_cancel_arrival_boundary(delay_seconds: int, cancel_accepted: bool) -> None:
    """撤单 08:58:58 发出；到达时刻进入只撮合阶段 (左闭) 即被拒绝，原单保留并可随后成交."""
    clock = FakeClock(cst(8, 56))
    gw = SimulatedGateway(
        "acc1",
        TRADING_DAY,
        clock=clock,
        session_gate=make_session_gate(),
        cancel_delay=timedelta(seconds=delay_seconds),
        participation_rate=Decimal("0.5"),
    )
    gw.submit(intent("auction", Side.BUY, 2, limit=3000, created_at=cst(8, 56)), EPOCH)
    assert statuses(gw.drain_events()) == [OrderStatus.ACCEPTED]

    clock.set(cst(8, 58, 58))
    result = gw.cancel(OrderIdentity(account_id="acc1", exchange=Exchange.SHFE, client_order_id="auction"), EPOCH)
    if cancel_accepted:
        assert result.state == SendState.SENT_UNKNOWN
        assert gw.drain_events() == []  # 到达 != 生效：到达时刻之前没有 CANCELLED 回报
        gw.apply_pending_cancels(cst(8, 58, 59))
        cancelled = gw.drain_events()
        assert statuses(cancelled) == [OrderStatus.CANCELLED]
        assert cancelled[0].event_time == cst(8, 58, 59)
        return
    assert result.state == SendState.NOT_SENT and result.local_code == -3
    assert gw.drain_events() == []  # 被拒绝的撤单不生成生效事件
    assert [o.client_order_id for o in gw.active_orders()] == ["auction"]

    # 随后的竞价撮合：预算 1 手 -> 部分成交，原单继续存在
    auction_bar = make_bar(
        open_="3000",
        high="3000",
        low="3000",
        close="3000",
        volume=2,
        bar_start=cst(8, 59),
        bar_end=cst(9, 0),
        includes_auction=True,
    )
    clock.set(cst(8, 59))
    evts = gw.match_bar(auction_bar)
    assert [(t.quantity, t.price) for t in trades(evts)] == [(1, Decimal("3000"))]
    assert statuses(evts) == [OrderStatus.PARTIALLY_FILLED]


def test_order_arriving_in_no_submit_phase_is_rejected_on_arrival() -> None:
    clock = FakeClock(cst(8, 58, 59))
    gw = SimulatedGateway(
        "acc1", TRADING_DAY, clock=clock, session_gate=make_session_gate(), order_delay=timedelta(seconds=2)
    )
    res = gw.submit(intent("late", Side.BUY, 1, created_at=cst(8, 58, 59)), EPOCH)
    assert res.state == SendState.SENT_UNKNOWN
    reports = gw.drain_events()
    assert statuses(reports) == [OrderStatus.REJECTED]
    assert reports[0].event_time == cst(8, 59, 1)


def test_a25_01_break_bar_never_matches() -> None:
    clock = FakeClock(cst(9, 30))
    gw = SimulatedGateway("acc1", TRADING_DAY, clock=clock, session_gate=make_session_gate())
    gw.submit(intent("resting", Side.BUY, 1, limit=2900, created_at=cst(9, 30)), EPOCH)
    gw.drain_events()
    break_bar = make_bar(
        open_="2950", high="2960", low="2850", close="2900", bar_start=cst(10, 15), bar_end=cst(10, 30)
    )
    assert trades(gw.match_bar(break_bar)) == []
    continuous_bar = make_bar(
        open_="2950", high="2960", low="2850", close="2900", bar_start=cst(10, 30), bar_end=cst(11, 30)
    )
    assert [t.price for t in trades(gw.match_bar(continuous_bar))] == [Decimal("2900")]


def test_a25_07_auction_bar_open_rejected_when_policy_says_so() -> None:
    gw = SimulatedGateway("acc1", TRADING_DAY, auction_fill_policy=AuctionFillPolicy.REJECT)
    gw.submit(intent("mkt", Side.BUY, 1), EPOCH)
    gw.drain_events()
    auction_bar = make_bar(includes_auction=True)
    assert trades(gw.match_bar(auction_bar)) == []  # 不用连续交易 Bar 冒充竞价成交
    plain = make_bar(bar_start=BASE_TIME + timedelta(hours=1))
    assert [t.price for t in trades(gw.match_bar(plain))] == [plain.open]
