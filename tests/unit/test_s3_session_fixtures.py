"""S3-10：A25 时段夹具经 CalendarSessionGate + SimulatedGateway + BacktestEngine 执行 (A25-01, A25-04)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from qh_trader.core.constants import EventKind, Exchange, Offset, OrderStatus, PositionSide, SendState
from qh_trader.core.objects import Bar, InstrumentId, RecordMeta
from qh_trader.data.calendar import TradingCalendar
from qh_trader.data.session_gate import CalendarSessionGate
from qh_trader.engine.backtest_engine import BacktestEngine
from qh_trader.engine.base_engine import InstrumentEconomics
from qh_trader.gateway.simulated_gateway import SimulatedGateway
from qh_trader.strategy.base import StrategyBase, StrategyContext

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "sessions" / "A25-04"
RB = InstrumentId(Exchange.SHFE, "rb2410")
DAY = date(2024, 9, 10)
ECO = InstrumentEconomics(Decimal("10"), Decimal("1"), Decimal("5.0"), Decimal("0.1"), "fixture")


def load():
    calendar = TradingCalendar.from_file(FIXTURE / "calendar.json")
    expected = json.loads((FIXTURE / "expected.json").read_text(encoding="utf-8"))
    return calendar, {case["id"]: case for case in expected["cases"]}


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def bar(start: datetime, end: datetime, *, op: str, hi: str, lo: str, cl: str, vol: int, auction: bool = False) -> Bar:
    meta = RecordMeta(
        event_time=end,
        available_at=end,
        ingested_at=end,
        trading_day=DAY,
        source_id="fixture",
        source_version="v1",
        ingest_seq=1,
    )
    return Bar(
        instrument=RB,
        meta=meta,
        bar_start=start,
        bar_end=end,
        open_time=start,
        interval="1m",
        open=Decimal(op),
        high=Decimal(hi),
        low=Decimal(lo),
        close=Decimal(cl),
        volume=vol,
        turnover=Decimal("1"),
        open_interest=1,
        includes_auction=auction,
    )


class OneOrderThenCancel(StrategyBase):
    """第一根 Bar 收盘下限价买单；到达 cancel_at 定时器时撤单."""

    def __init__(
        self, strategy_id: str, context: StrategyContext, *, qty: int, limit: int, cancel_at: datetime | None
    ) -> None:
        super().__init__(strategy_id, context)
        self.qty, self.limit, self.cancel_at = qty, limit, cancel_at
        self.cid: str | None = None

    def on_bar(self, b: Bar) -> None:
        if self.cid is None:
            self.cid = self.buy(b.instrument, self.qty, Offset.OPEN, limit_price_ticks=self.limit)
            if self.cancel_at is not None:
                self.context.schedule_timer(self.cancel_at, "cancel")

    def on_timer(self, timer) -> None:
        if timer.timer_id == "cancel" and self.cid is not None:
            self.context.cancel_order(self.cid)


@pytest.mark.parametrize(
    "case_id", ["cancel_arrives_before_cutoff", "cancel_arrives_at_cutoff", "cancel_arrives_after_cutoff"]
)
def test_a25_04_cancel_boundary_through_engine(case_id: str) -> None:
    calendar, cases = load()
    case = cases[case_id]
    inputs, expected = case["inputs"], case["expected"]
    gate = CalendarSessionGate(calendar)

    pre_open = at("2024-09-10T08:55:00+08:00")
    # 一根 08:55~08:56 的“预热” Bar 让策略在 08:56 下单；竞价 Bar 08:59~09:00 (含竞价，有量)
    warm = bar(pre_open, at(inputs["order_created_at"]), op="3000", hi="3000", lo="3000", cl="3000", vol=0)
    auction = bar(
        at("2024-09-10T08:59:00+08:00"),
        at("2024-09-10T09:00:00+08:00"),
        op="3000",
        hi="3000",
        lo="3000",
        cl="3000",
        vol=2,
        auction=True,
    )

    gw = SimulatedGateway(
        "acc", DAY, cancel_delay=timedelta(seconds=inputs["cancel_delay_seconds"]), participation_rate=Decimal("0.5")
    )
    eng = BacktestEngine(
        account_id="acc",
        gateway=gw,
        start_time=pre_open,
        initial_capital=Decimal("100000"),
        default_economics=ECO,
        session_gate=gate,
        trading_days=[DAY],
    )
    eng.add_strategy(OneOrderThenCancel("s", eng, qty=2, limit=3000, cancel_at=at(inputs["cancel_sent_at"])))
    res = eng.run([warm, auction])
    order = res.orders[0]
    reports = [(e.payload.status, e.event_time) for e in res.events if e.kind == EventKind.ORDER_REPORT]

    if expected["cancel_accepted"]:
        assert order.status == OrderStatus.CANCELLED
        assert (OrderStatus.CANCELLED, at(expected["cancel_effective_at"])) in reports
        assert res.total_trades == 0
        assert not eng.ledger._funds_reservations  # noqa: SLF001
        return

    # 被拒绝：没有 CANCELLED 回报，原单保留，撤单拒绝原因留痕，随后竞价按预算部分成交并只入账一次
    assert all(status != OrderStatus.CANCELLED for status, _ in reports)
    assert order.cancel_reject_reason and "does not permit cancel" in order.cancel_reject_reason
    assert res.total_trades == expected["unique_trade_postings"]
    assert res.trades[0].quantity == expected["auction_fill_quantity"]
    assert order.cum_filled_qty == expected["auction_fill_quantity"]
    assert order.send_state == SendState.CONFIRMED_REMOTE
    # 回测结束时未成交部分过期并释放预占；成交部分已入账
    assert order.status == OrderStatus.EXPIRED
    assert not eng.ledger._funds_reservations  # noqa: SLF001
    assert eng.position_manager.get_position(RB, PositionSide.LONG).total_position == 1


def test_a25_01_break_bar_never_matches_through_engine() -> None:
    calendar, cases = load()
    case = cases["break_bar_never_matches"]
    gate = CalendarSessionGate(calendar)
    t0 = at("2024-09-10T09:29:00+08:00")
    signal_bar = bar(t0, at(case["inputs"]["order_created_at"]), op="2950", hi="2960", lo="2940", cl="2950", vol=10)
    break_bar = bar(
        at("2024-09-10T10:15:00+08:00"),
        at("2024-09-10T10:30:00+08:00"),
        op="2950",
        hi="2960",
        lo="2850",
        cl="2900",
        vol=10,
    )
    cont_bar = bar(
        at("2024-09-10T10:30:00+08:00"),
        at("2024-09-10T11:30:00+08:00"),
        op="2950",
        hi="2960",
        lo="2850",
        cl="2900",
        vol=10,
    )

    gw = SimulatedGateway("acc", DAY)
    eng = BacktestEngine(
        account_id="acc",
        gateway=gw,
        start_time=t0,
        initial_capital=Decimal("100000"),
        default_economics=ECO,
        session_gate=gate,
        trading_days=[DAY],
    )
    eng.add_strategy(OneOrderThenCancel("s", eng, qty=1, limit=int(case["inputs"]["limit_price"]), cancel_at=None))
    res = eng.run([signal_bar, break_bar, cont_bar])
    fills = [(t.price, t.event_time) for t in res.trades]
    assert len(fills) == case["expected"]["fills_in_continuous_bar"]
    assert fills[0][0] == Decimal(case["expected"]["fill_price"])
    assert fills[0][1] == cont_bar.bar_end  # 盘中触价成交记在 Bar 结束估算时刻，不在休市 Bar
