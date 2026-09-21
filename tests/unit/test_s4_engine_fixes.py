"""S4 修复回归：本地拒绝通知策略、委托活跃查询、递延平仓跨日重规划、日线无时段门明确失败 (B2/B3/M5/M7)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from qh_trader.core.constants import EventKind, Exchange, MarketPhase, MissingRuleError, Offset, OrderStatus
from qh_trader.core.objects import Bar, InstrumentId, OrderUpdate, Permissions, RecordMeta, Session
from qh_trader.data.calendar import CHINA_TZ, TradingCalendar
from qh_trader.data.session_gate import CalendarSessionGate
from qh_trader.engine.backtest_engine import BacktestEngine
from qh_trader.engine.base_engine import InstrumentEconomics
from qh_trader.gateway.simulated_gateway import SimulatedGateway
from qh_trader.strategy.base import StrategyBase, StrategyContext

RB = InstrumentId(Exchange.SHFE, "rb2410")
D1, D2, D3 = date(2024, 9, 10), date(2024, 9, 11), date(2024, 9, 12)
ECO = InstrumentEconomics(Decimal("10"), Decimal("1"), Decimal("5.0"), Decimal("0.1"), "test")
BASE = datetime(2024, 9, 10, 1, 0, tzinfo=timezone.utc)


def hourly(i: int, op: str, day: date = D1) -> Bar:
    start = BASE + timedelta(hours=i)
    end = start + timedelta(hours=1)
    p = Decimal(op)
    meta = RecordMeta(
        event_time=end,
        available_at=end,
        ingested_at=end,
        trading_day=day,
        source_id="t",
        source_version="v",
        ingest_seq=i + 1,
    )
    return Bar(
        instrument=RB,
        meta=meta,
        bar_start=start,
        bar_end=end,
        open_time=start,
        interval="1h",
        open=p,
        high=p + 20,
        low=p - 20,
        close=p + 5,
        volume=100,
        turnover=Decimal(1),
        open_interest=1,
        includes_auction=False,
    )


class RecordingStrategy(StrategyBase):
    def __init__(self, strategy_id: str, context: StrategyContext, *, qty: int = 1) -> None:
        super().__init__(strategy_id, context)
        self.qty = qty
        self.reports: list[OrderUpdate] = []
        self.cids: list[str] = []
        self.active_after_submit: list[bool] = []

    def on_bar(self, bar: Bar) -> None:
        if not self.cids:
            cid = self.buy(bar.instrument, self.qty, Offset.OPEN)
            self.cids.append(cid)
            self.active_after_submit.append(self.context.is_order_active(cid))

    def on_order(self, order: OrderUpdate) -> None:
        self.reports.append(order)


def test_local_risk_rejection_is_reported_to_strategy_and_order_becomes_inactive() -> None:
    gw = SimulatedGateway("acc", D1)
    eng = BacktestEngine(
        account_id="acc", gateway=gw, start_time=BASE, initial_capital=Decimal("1000"), default_economics=ECO
    )
    strat = RecordingStrategy("s", eng)
    eng.add_strategy(strat)
    res = eng.run([hourly(0, "3000"), hourly(1, "3010"), hourly(2, "3020")])
    assert res.rejected_intents and res.rejected_intents[0].stage == "risk"
    # 本地拒绝没有网关回报，引擎必须合成 REJECTED 回报，否则策略按委托跟踪的在途状态永久卡住 (B2/B3)
    assert [r.status for r in strat.reports] == [OrderStatus.REJECTED]
    assert strat.reports[0].identity.client_order_id == strat.cids[0]
    assert strat.active_after_submit == [False]
    assert eng.is_order_active(strat.cids[0]) is False
    assert eng.local_rejection_reports and eng.local_rejection_reports[0].event_time == hourly(0, "3000").bar_end


def test_is_order_active_follows_children_and_terminal_reports() -> None:
    gw = SimulatedGateway("acc", D1)
    eng = BacktestEngine(
        account_id="acc", gateway=gw, start_time=BASE, initial_capital=Decimal("100000"), default_economics=ECO
    )

    class OpenThenClose(StrategyBase):
        def __init__(self, sid: str, ctx: StrategyContext) -> None:
            super().__init__(sid, ctx)
            self.n = 0
            self.open_cid = ""
            self.close_cid = ""
            self.seen: list[tuple[int, bool, bool]] = []

        def on_bar(self, bar: Bar) -> None:
            self.n += 1
            if self.n == 1:
                self.open_cid = self.buy(bar.instrument, 1, Offset.OPEN)
            elif self.n == 3:
                self.close_cid = self.sell(bar.instrument, 1, Offset.CLOSE)
            self.seen.append(
                (
                    self.n,
                    self.context.is_order_active(self.open_cid) if self.open_cid else False,
                    self.context.is_order_active(self.close_cid) if self.close_cid else False,
                )
            )

    strat = OpenThenClose("s", eng)
    eng.add_strategy(strat)
    res = eng.run([hourly(i, str(3000 + 10 * i)) for i in range(5)])
    assert res.total_trades == 2
    # Bar1: 开仓单刚提交 -> 活跃；Bar2: 已在 Bar2 开盘成交 -> 不活跃；Bar3: 平仓父单拆子单后仍活跃；Bar4: 成交后不活跃
    assert strat.seen[0][1] is True and strat.seen[1][1] is False
    assert strat.seen[2][2] is True and strat.seen[3][2] is False
    parent = next(o for o in res.orders if o.client_order_id == strat.close_cid)
    assert parent.child_order_ids  # SHFE 平仓被拆成显式平今子单
    assert eng.is_order_active(strat.close_cid) is False


# ---------------------------------------------------------------------- M5：递延平仓跨日后按预占桶重规划开平


def day_night_calendar() -> TradingCalendar:
    available = datetime(2024, 1, 1, tzinfo=timezone.utc)
    sessions = []
    for day in (D1, D2, D3):
        sessions.append(
            Session(
                instrument=RB,
                session_id="day",
                trading_day=day,
                start=datetime.combine(day, time(9), CHINA_TZ),
                end=datetime.combine(day, time(15), CHINA_TZ),
                phase=MarketPhase.CONTINUOUS,
                permissions=Permissions(True, True, True),
                rule_version="v1",
                source_id="syn",
                available_at=available,
            )
        )
        if day != D1:
            eve = day - timedelta(days=1)
            sessions.append(
                Session(
                    instrument=RB,
                    session_id="night",
                    trading_day=day,
                    start=datetime.combine(eve, time(21), CHINA_TZ),
                    end=datetime.combine(eve, time(23), CHINA_TZ),
                    phase=MarketPhase.CONTINUOUS,
                    permissions=Permissions(True, True, True),
                    rule_version="v1",
                    source_id="syn",
                    available_at=available,
                )
            )
    return TradingCalendar(
        sessions,
        trading_days=[D1, D2, D3],
        coverage_start=D1,
        coverage_end=D3,
        version="v1",
        source_id="syn",
        available_at=available,
    )


def session_bar(day: date, kind: str, op: str) -> Bar:
    if kind == "day":
        start, end = datetime.combine(day, time(9), CHINA_TZ), datetime.combine(day, time(15), CHINA_TZ)
        sid = "day"
    else:
        eve = day - timedelta(days=1)
        start, end = datetime.combine(eve, time(21), CHINA_TZ), datetime.combine(eve, time(23), CHINA_TZ)
        sid = "night"
    p = Decimal(op)
    meta = RecordMeta(
        event_time=end,
        available_at=end,
        ingested_at=end,
        trading_day=day,
        source_id="t",
        source_version="v",
        ingest_seq=1,
        session_id=sid,
    )
    return Bar(
        instrument=RB,
        meta=meta,
        bar_start=start,
        bar_end=end,
        open_time=start,
        interval="session",
        open=p,
        high=p + 20,
        low=p - 20,
        close=p + 5,
        volume=100,
        turnover=Decimal(1),
        open_interest=1,
        includes_auction=True,
    )


class OpenDay1CloseDay1Close(StrategyBase):
    """D1 夜盘不存在：D1 日盘开多 (成交于 D2 夜盘开盘)，D2 日盘收盘发平今意图 -> 递延到 D3 夜盘 (交易日 D3) 才送出."""

    def __init__(self, sid: str, ctx: StrategyContext) -> None:
        super().__init__(sid, ctx)
        self.n = 0

    def on_bar(self, bar: Bar) -> None:
        self.n += 1
        if self.n == 1:
            self.buy(bar.instrument, 1, Offset.OPEN)
        elif self.n == 3:
            self.sell(bar.instrument, 1, Offset.CLOSE_TODAY)


def test_deferred_close_today_is_sent_as_close_yesterday_after_day_roll() -> None:
    calendar = day_night_calendar()
    gate = CalendarSessionGate(calendar)
    bars = [
        session_bar(D1, "day", "3000"),
        session_bar(D2, "night", "3010"),
        session_bar(D2, "day", "3020"),
        session_bar(D3, "night", "3030"),
        session_bar(D3, "day", "3040"),
    ]
    gw = SimulatedGateway("acc", D1, session_gate=gate)
    eng = BacktestEngine(
        account_id="acc",
        gateway=gw,
        start_time=bars[0].bar_start,
        initial_capital=Decimal("100000"),
        default_economics=ECO,
        session_gate=gate,
        trading_days=[D1, D2, D3],
    )
    eng.add_strategy(OpenDay1CloseDay1Close("s", eng))
    res = eng.run(bars)
    opened = res.trades[0]
    assert opened.trading_day == D2 and opened.price == Decimal("3010")  # D2 夜盘 (交易日 D2) 开多 -> 今仓
    closed = res.trades[1]
    # 平今意图在 D2 15:00 产生，持有到 D3 夜盘 21:00 送出；此时已跨日，今仓已转昨仓，送出的必须是平昨
    assert closed.offset == Offset.CLOSE_YESTERDAY and closed.trading_day == D3 and closed.price == Decimal("3030")
    assert [(r.planned_offset, r.sent_offset) for r in res.offset_rewrites] == [
        (Offset.CLOSE_TODAY, Offset.CLOSE_YESTERDAY)
    ]
    rewritten = next(o for o in res.orders if o.client_order_id == res.offset_rewrites[0].client_order_id)
    assert rewritten.offset == Offset.CLOSE_YESTERDAY  # 订单记录反映实际送出的开平标志
    assert not eng.ledger._funds_reservations and not eng.position_manager._reservations  # noqa: SLF001
    assert res.final_equity == Decimal("100000") + Decimal("200") - Decimal("10")


# ---------------------------------------------------------------------- M7：日线 Bar 没有时段门时明确失败


def test_daily_bars_without_session_gate_fail_explicitly() -> None:
    gw = SimulatedGateway("acc", D1)
    eng = BacktestEngine(
        account_id="acc", gateway=gw, start_time=BASE, initial_capital=Decimal("100000"), default_economics=ECO
    )
    eng.add_strategy(RecordingStrategy("s", eng))
    with pytest.raises(MissingRuleError, match="session gate"):
        eng.run([session_bar(D1, "day", "3000"), session_bar(D2, "day", "3010")])


# ---------------------------------------------------------------------- 反应式订单不能吃到同一瞬间的开盘价


class ReactOnFill(StrategyBase):
    """开仓成交回报到达的同一瞬间立刻再下一单：该单不得按同一瞬间的开盘价成交."""

    def __init__(self, sid: str, ctx: StrategyContext) -> None:
        super().__init__(sid, ctx)
        self.n = 0
        self.reacted = False

    def on_bar(self, bar: Bar) -> None:
        self.n += 1
        if self.n == 1:
            self.buy(bar.instrument, 1, Offset.OPEN)

    def on_trade(self, trade) -> None:
        if not self.reacted:
            self.reacted = True
            self.buy(trade.instrument, 1, Offset.OPEN)


def test_order_reacting_to_a_fill_is_held_to_the_next_bar_open() -> None:
    gw = SimulatedGateway("acc", D1)
    eng = BacktestEngine(
        account_id="acc", gateway=gw, start_time=BASE, initial_capital=Decimal("100000"), default_economics=ECO
    )
    eng.add_strategy(ReactOnFill("s", eng))
    bars = [hourly(i, str(3000 + 10 * i)) for i in range(4)]
    res = eng.run(bars)
    assert [(t.price, t.event_time) for t in res.trades] == [
        (Decimal("3010"), bars[1].open_time),  # Bar1 收盘意图 -> Bar2 开盘成交
        (Decimal("3020"), bars[2].open_time),  # 对该成交的反应 -> 持有到 Bar3 开盘成交，不吃 Bar2 开盘价
    ]
    accepted = [e for e in res.events if e.kind == EventKind.ORDER_REPORT and e.payload.status == OrderStatus.ACCEPTED]
    assert accepted[1].event_time == bars[2].open_time  # 第二单在 Bar3 开盘才送达网关
    assert "strictly after" in gw.assumptions().reactive_orders
