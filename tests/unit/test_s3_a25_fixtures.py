"""S3-10：A25-02 / A25-03 / A25-05 时段夹具经 CalendarSessionGate + SimulatedGateway + BacktestEngine 执行.

夹具均为合成规则（见各 expected.json 的 oracle.notes），只证明规则边界逻辑，不证明历史制度或当前柜台行为。
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from qh_trader.core.constants import EventKind, Exchange, MissingRuleError, Offset, OrderStatus, PositionSide
from qh_trader.core.objects import Bar, ControlEpoch, InstrumentId, RecordMeta
from qh_trader.data.calendar import CHINA_TZ, TradingCalendar
from qh_trader.data.session_gate import CalendarSessionGate
from qh_trader.domain.risk import HolidayRiskHook, RiskManager
from qh_trader.engine.backtest_engine import BacktestEngine
from qh_trader.engine.base_engine import InstrumentEconomics
from qh_trader.gateway.simulated_gateway import SimulatedGateway
from qh_trader.strategy.base import StrategyBase, StrategyContext

ROOT = Path(__file__).resolve().parents[2]
SESSIONS = ROOT / "tests" / "fixtures" / "sessions"
RB = InstrumentId(Exchange.SHFE, "rb2410")
MA = InstrumentId(Exchange.CZCE, "MA501")
ECO = InstrumentEconomics(Decimal("10"), Decimal("1"), Decimal("5.0"), Decimal("0.1"), "fixture")


def load(case_dir: str):
    calendar = TradingCalendar.from_file(SESSIONS / case_dir / "calendar.json")
    expected = json.loads((SESSIONS / case_dir / "expected.json").read_text(encoding="utf-8"))
    return calendar, {case["id"]: case for case in expected["cases"]}


def cst(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def day_bar(
    instrument: InstrumentId,
    trading_day: date,
    *,
    start: datetime,
    end: datetime,
    session_id: str,
    open_: str,
    volume: int = 100,
    auction: bool = False,
    claimed_day: date | None = None,
) -> Bar:
    op = Decimal(open_)
    meta = RecordMeta(
        event_time=end,
        available_at=end,
        ingested_at=end,
        trading_day=claimed_day or trading_day,
        source_id="fixture",
        source_version="v1",
        ingest_seq=1,
        session_id=session_id,
    )
    return Bar(
        instrument=instrument,
        meta=meta,
        bar_start=start,
        bar_end=end,
        open_time=start,
        interval="session",
        open=op,
        high=op + 20,
        low=op - 20,
        close=op + 5,
        volume=volume,
        turnover=Decimal("1"),
        open_interest=1,
        includes_auction=auction,
    )


def session_bar(instrument: InstrumentId, day: date, kind: str, open_: str, *, volume: int = 100, claimed_day=None) -> Bar:
    """kind: 'day' (09:00-15:00, 属于 day) 或 'night' (前一自然日 21:00-23:00, 属于 day)."""
    if kind == "day":
        start = datetime.combine(day, time(9), CHINA_TZ)
        end = datetime.combine(day, time(15), CHINA_TZ)
        return day_bar(instrument, day, start=start, end=end, session_id="day", open_=open_, volume=volume, auction=True, claimed_day=claimed_day)
    natural = day - timedelta(days=1)
    while natural.weekday() >= 5:
        natural -= timedelta(days=1)
    start = datetime.combine(natural, time(21), CHINA_TZ)
    end = datetime.combine(natural, time(23), CHINA_TZ)
    return day_bar(instrument, day, start=start, end=end, session_id="night_continuous", open_=open_, volume=volume, claimed_day=claimed_day)


def make_engine(calendar: TradingCalendar, start: datetime, **gateway_kwargs) -> tuple[SimulatedGateway, BacktestEngine]:
    gate = CalendarSessionGate(calendar)
    gw = SimulatedGateway("acc", sorted(calendar.trading_days)[0], **gateway_kwargs)
    engine = BacktestEngine(
        account_id="acc",
        gateway=gw,
        start_time=start,
        initial_capital=Decimal("1000000"),
        default_economics=ECO,
        session_gate=gate,
        trading_days=sorted(calendar.trading_days),
    )
    return gw, engine


def order_reports(result, status: OrderStatus | None = None):
    return [
        (e.payload.identity.client_order_id, e.payload.status, e.event_time)
        for e in result.events
        if e.kind == EventKind.ORDER_REPORT and (status is None or e.payload.status == status)
    ]


class BuyOnFirstBar(StrategyBase):
    def __init__(self, strategy_id: str, context: StrategyContext, instrument: InstrumentId, qty: int, limit: int) -> None:
        super().__init__(strategy_id, context)
        self.instrument, self.qty, self.limit = instrument, qty, limit
        self.cid: str | None = None

    def on_bar(self, bar: Bar) -> None:
        if self.cid is None and bar.instrument == self.instrument:
            self.cid = self.buy(self.instrument, self.qty, Offset.OPEN, limit_price_ticks=self.limit)


# ---------------------------------------------------------------------- A25-02


def test_a25_02_shfe_night_partial_fill_carries_into_day_session() -> None:
    calendar, cases = load("A25-02")
    case = cases["shfe_night_partial_fill_carries_into_day"]
    inp, exp = case["inputs"], case["expected"]
    d1, d2 = date(2024, 9, 10), date(2024, 9, 11)
    bars = [
        session_bar(RB, d1, "day", "3000"),
        session_bar(RB, d2, "night", inp["night_bar"]["open"], volume=inp["night_bar"]["volume"]),
        session_bar(RB, d2, "day", inp["day_bar"]["open"], volume=inp["day_bar"]["volume"]),
    ]
    gw, eng = make_engine(calendar, bars[0].bar_start, participation_rate=Decimal(inp["participation_rate"]))
    eng.add_strategy(BuyOnFirstBar("s", eng, RB, inp["order_quantity"], int(inp["limit_price"])))
    res = eng.run(bars)

    assert len(res.orders) == exp["order_count"]
    order = res.orders[0]
    accepted = order_reports(res, OrderStatus.ACCEPTED)
    assert len(accepted) == exp["accepted_reports"]
    assert accepted[0][2] == cst(exp["accepted_at"])  # 15:00 信号持有到 21:00 夜盘才送出
    assert [(t.quantity, t.price, t.event_time) for t in res.trades] == [
        (exp["night_fill"]["quantity"], Decimal(exp["night_fill"]["price"]), bars[1].open_time),
        (exp["day_fill"]["quantity"], Decimal(exp["day_fill"]["price"]), cst(exp["day_fill"]["at"])),
    ]
    assert all(t.trading_day == d2 for t in res.trades)  # 夜盘成交属于次一交易日
    assert all(t.order_identity.client_order_id == order.client_order_id for t in res.trades)  # 原标识不变
    assert order.status == OrderStatus(exp["final_status"]) and order.cum_filled_qty == exp["cum_filled"]
    assert res.total_commission == Decimal(exp["total_commission"])
    assert len(eng.ledger._funds_reservations) == exp["reservations_left"]  # noqa: SLF001
    assert len(eng.position_manager._reservations) == exp["reservations_left"]  # noqa: SLF001
    assert eng.position_manager.get_position(RB, PositionSide.LONG).total_position == exp["cum_filled"]
    # 时段切换没有重复建单：只有一条 ACCEPTED，没有 EXPIRED/REJECTED
    assert not order_reports(res, OrderStatus.EXPIRED) and not order_reports(res, OrderStatus.REJECTED)


class NightThenCancelResubmit(StrategyBase):
    """夜盘挂单；次日 08:56 (只撤不报窗) 撤掉残留单并重新报单."""

    def __init__(self, strategy_id: str, context: StrategyContext, instrument: InstrumentId, qty: int, limit: int, at: datetime) -> None:
        super().__init__(strategy_id, context)
        self.instrument, self.qty, self.limit, self.at = instrument, qty, limit, at
        self.first: str | None = None
        self.second: str | None = None

    def on_bar(self, bar: Bar) -> None:
        if self.first is None:
            self.first = self.buy(self.instrument, self.qty, Offset.OPEN, limit_price_ticks=self.limit)
            self.context.schedule_timer(self.at, "cancel-resubmit")

    def on_timer(self, timer) -> None:
        if timer.timer_id == "cancel-resubmit" and self.first is not None:
            self.context.cancel_order(self.first)
            self.second = self.buy(self.instrument, 1, Offset.OPEN, limit_price_ticks=self.limit)


def test_a25_02_czce_cancel_only_window_accepts_cancel_but_holds_new_orders() -> None:
    calendar, cases = load("A25-02")
    case = cases["czce_cancel_only_window"]
    inp, exp = case["inputs"], case["expected"]
    d1, d2 = date(2024, 9, 10), date(2024, 9, 11)
    bars = [
        session_bar(MA, d1, "day", "3000"),
        session_bar(MA, d2, "night", inp["night_bar"]["open"], volume=inp["night_bar"]["volume"]),
        session_bar(MA, d2, "day", inp["day_bar"]["open"], volume=inp["day_bar"]["volume"]),
    ]
    gw, eng = make_engine(calendar, bars[0].bar_start, participation_rate=Decimal(inp["participation_rate"]))
    strat = NightThenCancelResubmit("s", eng, MA, inp["order_quantity"], int(inp["limit_price"]), cst(inp["cancel_and_resubmit_at"]))
    eng.add_strategy(strat)
    res = eng.run(bars)

    by_id = {o.client_order_id: o for o in res.orders}
    residual = by_id[strat.first]
    assert residual.status == OrderStatus.CANCELLED and residual.cum_filled_qty == exp["residual_cum_filled"]
    assert order_reports(res, OrderStatus.CANCELLED) == [(strat.first, OrderStatus.CANCELLED, cst(exp["residual_cancelled_at"]))]
    new_accepted = [r for r in order_reports(res, OrderStatus.ACCEPTED) if r[0] == strat.second]
    assert new_accepted[0][2] == cst(exp["new_order_accepted_at"])  # 08:56 不能报单，持有到 09:00
    assert [(t.quantity, t.price) for t in res.trades] == [(f["quantity"], Decimal(f["price"])) for f in exp["fills"]]
    window = (cst("2024-09-11T08:55:00+08:00"), cst("2024-09-11T09:00:00+08:00"))
    assert sum(1 for t in res.trades if window[0] <= t.event_time < window[1]) == exp["fills_between_08_55_and_09_00"]
    assert res.total_commission == Decimal(exp["total_commission"])
    assert len(eng.ledger._funds_reservations) == exp["reservations_left"]  # noqa: SLF001


# ---------------------------------------------------------------------- A25-03


class OrderAtTimes(StrategyBase):
    def __init__(self, strategy_id: str, context: StrategyContext, instrument: InstrumentId, times: list[datetime]) -> None:
        super().__init__(strategy_id, context)
        self.instrument, self.times = instrument, times
        self.orders: dict[datetime, str] = {}

    def on_start(self) -> None:
        super().on_start()
        for at in self.times:
            self.context.schedule_timer(at, f"order@{at.isoformat()}")

    def on_bar(self, bar: Bar) -> None:
        pass

    def on_timer(self, timer) -> None:
        if timer.timer_id.startswith("order@"):
            self.orders[self.context.now()] = self.buy(self.instrument, 1, Offset.OPEN)


def a25_03_bars() -> list[Bar]:
    d1, d2 = date(2023, 5, 25), date(2023, 5, 26)
    return [
        session_bar(RB, d1, "night", "3000"),
        session_bar(RB, d1, "day", "3010"),
        session_bar(RB, d2, "night", "3020"),
        session_bar(RB, d2, "day", "3030"),
    ]


def test_a25_03_rule_version_applies_by_trading_day_not_natural_day() -> None:
    calendar, cases = load("A25-03")
    gate = CalendarSessionGate(calendar)
    before, on = cases["before_boundary_no_day_auction"], cases["on_boundary_day_auction_applies"]
    t_before, t_on = cst(before["inputs"]["order_at"]), cst(on["inputs"]["order_at"])
    assert gate.permissions_at(RB, t_before) is None
    perms = gate.permissions_at(RB, t_on)
    assert perms is not None and (perms.submit, perms.cancel, perms.match) == (True, True, False)
    night = cases["night_before_boundary_belongs_to_new_trading_day"]
    assert gate.trading_day_at(RB, cst(night["inputs"]["at"])) == date.fromisoformat(night["expected"]["trading_day"])

    bars = a25_03_bars()
    gw, eng = make_engine(calendar, bars[0].bar_start)
    strat = OrderAtTimes("s", eng, RB, [t_before, t_on])
    eng.add_strategy(strat)
    res = eng.run(bars)
    accepted = {cid: at for cid, _, at in order_reports(res, OrderStatus.ACCEPTED)}
    assert accepted[strat.orders[t_before]] == cst(before["expected"]["accepted_at"])  # 旧规则：持有到 09:00
    assert accepted[strat.orders[t_on]] == cst(on["expected"]["accepted_at"])  # 新规则：08:57 即受理
    fills = {t.order_identity.client_order_id: t.event_time for t in res.trades}
    assert fills[strat.orders[t_before]] == cst(before["expected"]["fill_at"])
    assert fills[strat.orders[t_on]] == cst(on["expected"]["fill_at"])

    # 重复回放得到相同阶段与订单结果
    gw2, eng2 = make_engine(calendar, bars[0].bar_start)
    eng2.add_strategy(OrderAtTimes("s", eng2, RB, [t_before, t_on]))
    assert eng2.run(a25_03_bars()).canonical_hashes() == res.canonical_hashes()


def test_a25_03_bar_claiming_natural_day_as_trading_day_is_rejected() -> None:
    calendar, cases = load("A25-03")
    case = cases["bar_with_natural_day_trading_day_is_rejected"]
    bars = a25_03_bars()
    wrong = session_bar(RB, date(2023, 5, 26), "night", "3020", claimed_day=date.fromisoformat(case["inputs"]["claimed_trading_day"]))
    _, eng = make_engine(calendar, bars[0].bar_start)
    eng.add_strategy(OrderAtTimes("s", eng, RB, []))
    with pytest.raises(MissingRuleError):
        eng.run([bars[0], bars[1], wrong, bars[3]])


# ---------------------------------------------------------------------- A25-05 / A05


class BuyAtBarIndex(StrategyBase):
    def __init__(self, strategy_id: str, context: StrategyContext, index: int) -> None:
        super().__init__(strategy_id, context)
        self.index, self.n = index, 0

    def on_bar(self, bar: Bar) -> None:
        self.n += 1
        if self.n == self.index:
            self.buy(bar.instrument, 1, Offset.OPEN)


def a25_05_bars() -> list[Bar]:
    return [
        session_bar(RB, date(2024, 9, 27), "day", "3000"),
        session_bar(RB, date(2024, 9, 30), "night", "3005"),
        session_bar(RB, date(2024, 9, 30), "day", "3010"),
        session_bar(RB, date(2024, 10, 8), "day", "3050"),
    ]


def test_a25_05_pre_holiday_signal_executes_at_post_holiday_auction_not_cancelled_night() -> None:
    calendar, cases = load("A25-05")
    case = cases["signal_before_holiday_defers_to_post_holiday_auction"]
    exp = case["expected"]
    bars = a25_05_bars()
    _, eng = make_engine(calendar, bars[0].bar_start)
    eng.add_strategy(BuyAtBarIndex("s", eng, 3))  # 09-30 日盘收盘信号
    res = eng.run(bars)
    accepted = order_reports(res, OrderStatus.ACCEPTED)
    assert accepted[0][2] == cst(exp["accepted_at"])
    assert [(t.price, t.event_time) for t in res.trades] == [(Decimal("3050"), cst(exp["fill_at"]))]
    signal_close, accepted_at = cst(case["inputs"]["signal_close"]), cst(exp["accepted_at"])
    between = [e for e in res.events if signal_close < e.event_time < accepted_at and e.kind != EventKind.TIMER]
    assert len(between) == exp["events_between"]
    assert [s.trading_day.isoformat() for s in res.equity_snapshots] == exp["snapshot_trading_days"]
    assert res.last_trading_day == date(2024, 10, 8)


def test_a25_05_holiday_risk_hook_blocks_open_before_holiday() -> None:
    calendar, cases = load("A25-05")
    case = cases["holiday_risk_hook_blocks_open_before_holiday"]
    bars = a25_05_bars()
    gate = CalendarSessionGate(calendar)
    gw = SimulatedGateway("acc", date(2024, 9, 27))
    risk = RiskManager(
        account_id="acc",
        control=ControlEpoch("backtest-controller", 1),
        holiday_hook=HolidayRiskHook(days_before_holiday=case["inputs"]["days_before_holiday"], prevent_new_open=True),
        holiday_dates=[date.fromisoformat(case["inputs"]["holiday_start"])],
    )
    eng = BacktestEngine(
        account_id="acc",
        gateway=gw,
        start_time=bars[0].bar_start,
        initial_capital=Decimal("1000000"),
        default_economics=ECO,
        session_gate=gate,
        risk_manager=risk,
        trading_days=sorted(calendar.trading_days),
    )
    eng.add_strategy(BuyAtBarIndex("s", eng, 3))
    res = eng.run(bars)
    assert res.total_trades == case["expected"]["trades"]
    assert res.rejected_intents[0].stage == case["expected"]["rejected_stage"]
    assert "HolidayRiskHook" in res.rejected_intents[0].reason


def test_a25_05_bar_on_unlisted_trading_day_is_rejected() -> None:
    calendar, cases = load("A25-05")
    case = cases["bar_on_unlisted_trading_day_is_rejected"]
    bars = a25_05_bars()
    holiday = date.fromisoformat(case["inputs"]["claimed_trading_day"])
    bogus = day_bar(RB, holiday, start=datetime.combine(holiday, time(9), CHINA_TZ), end=datetime.combine(holiday, time(15), CHINA_TZ), session_id="day", open_="3020")
    _, eng = make_engine(calendar, bars[0].bar_start)
    eng.add_strategy(BuyAtBarIndex("s", eng, 99))
    with pytest.raises(MissingRuleError):
        eng.run(bars[:3] + [bogus] + bars[3:])
