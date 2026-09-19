"""Unit tests for BacktestEngine / BaseEngine (S3-02, FR-MATCH-01, FR-EXEC-01, FR-RISK-03, FR-CAL-05, FR-VAL-07)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from qh_trader.core.constants import EventKind, Exchange, MissingRuleError, Offset, OrderStatus, SendState
from qh_trader.core.event import TimerEvent
from qh_trader.core.objects import Bar, InstrumentId, RecordMeta
from qh_trader.domain.limits import ExchangeLimits, LimitKind, LimitRule, LimitSource
from qh_trader.engine.backtest_engine import BacktestEngine
from qh_trader.engine.base_engine import InstrumentEconomics
from qh_trader.gateway.simulated_gateway import SimulatedGateway
from qh_trader.infrastructure.memory_journal import MemoryJournal
from qh_trader.strategy.base import StrategyBase, StrategyContext

RB_INST = InstrumentId(Exchange.SHFE, "rb2410")
HC_INST = InstrumentId(Exchange.SHFE, "hc2410")
BASE_START = datetime(2024, 9, 10, 1, 0, tzinfo=timezone.utc)
DAY_1 = date(2024, 9, 10)
DAY_2 = date(2024, 9, 11)
ECO = InstrumentEconomics(Decimal("10"), Decimal("1"), Decimal("5.0"), Decimal("0.1"), "test")


class SimpleBuyAndHoldStrategy(StrategyBase):
    """第 1 根 Bar 开仓买入 1 手，第 3 根 Bar 平仓."""

    def __init__(self, strategy_id: str, context: StrategyContext) -> None:
        super().__init__(strategy_id, context)
        self.bar_count = 0

    def on_bar(self, bar: Bar) -> None:
        self.bar_count += 1
        if self.bar_count == 1:
            self.buy(bar.instrument, quantity=1, offset=Offset.OPEN)
        elif self.bar_count == 3:
            self.sell(bar.instrument, quantity=1, offset=Offset.CLOSE)


def make_bar(
    i: int, op: str, hi: str, lo: str, cl: str, day: date, *, instrument=RB_INST, start=None, volume=100
) -> Bar:
    start = start or (BASE_START + timedelta(hours=i))
    end = start + timedelta(hours=1)
    meta = RecordMeta(
        event_time=end,
        available_at=end,
        ingested_at=end,
        trading_day=day,
        source_id="test",
        source_version="v1",
        ingest_seq=i + 1,
    )
    return Bar(
        instrument=instrument,
        meta=meta,
        bar_start=start,
        bar_end=end,
        open_time=start,
        interval="1h",
        open=Decimal(op),
        high=Decimal(hi),
        low=Decimal(lo),
        close=Decimal(cl),
        volume=volume,
        turnover=Decimal("100000"),
        open_interest=50000,
        includes_auction=False,
    )


def make_test_bars() -> list[Bar]:
    prices = [
        ("3000", "3050", "2990", "3040", DAY_1),
        ("3040", "3100", "3030", "3080", DAY_1),
        ("3080", "3120", "3070", "3110", DAY_2),  # 跨日
        ("3110", "3150", "3100", "3130", DAY_2),
    ]
    return [make_bar(i, *row) for i, row in enumerate(prices)]


def make_engine(**kwargs) -> tuple[SimulatedGateway, BacktestEngine]:
    gateway = SimulatedGateway(account_id="acc-test", trading_day=DAY_1, slippage_ticks=0)
    engine = BacktestEngine(
        account_id="acc-test",
        gateway=gateway,
        start_time=BASE_START,
        initial_capital=Decimal("100000.00"),
        default_economics=ECO,
        **kwargs,
    )
    return gateway, engine


# ---------------------------------------------------------------------- 手工账


def test_backtest_engine_matches_hand_account_and_is_deterministic() -> None:
    """手工账：3040 开多 -> 日终结算 3080 (+400) -> 3110 平多 (盯市 +300)；手续费 2×5；净利 690."""
    journal = MemoryJournal("acc-test")
    _, engine = make_engine(journal=journal)
    engine.add_strategy(SimpleBuyAndHoldStrategy("strat-1", engine))
    result = engine.run(make_test_bars())

    assert result.total_trades == 2
    trade_open, trade_close = result.trades
    assert (trade_open.offset, trade_open.price, trade_open.event_time) == (
        Offset.OPEN,
        Decimal("3040"),
        BASE_START + timedelta(hours=1),
    )
    assert (trade_close.offset, trade_close.price) == (Offset.CLOSE_YESTERDAY, Decimal("3110"))
    assert result.total_commission == Decimal("10.0")
    assert result.total_pnl == Decimal("690.00")
    assert result.final_equity == Decimal("100690.00")
    assert [s.total_equity for s in result.equity_snapshots] == [
        Decimal("100000"),
        Decimal("100395.00"),
        Decimal("100695.00"),
        Decimal("100690.00"),
    ]
    assert [(e.kind, e.amount) for e in result.ledger_entries] == [
        ("COMMISSION", Decimal("-5.00")),
        ("SETTLEMENT", Decimal("400.00")),
        ("COMMISSION", Decimal("-5.00")),
        ("MTM_CLOSE", Decimal("300.00")),
    ]
    # 订单状态经回报进入内核；路由父单本地不发送
    by_id = {o.client_order_id: o for o in result.orders}
    assert by_id["ord-1"].status == OrderStatus.FILLED and by_id["ord-1"].send_state == SendState.CONFIRMED_REMOTE
    assert by_id["ord-3"].status == OrderStatus.FILLED
    assert by_id["ord-2"].child_order_ids == ["ord-3"]
    assert not engine.ledger._funds_reservations and not engine.position_manager._reservations  # noqa: SLF001
    assert result.strategy_of_order["ord-3"] == "strat-1"
    # Journal 收到全部规范事件 (ACCEPTED/TRADE/FILLED ×2)
    assert [e.kind for e in journal.export_events()].count(EventKind.TRADE_REPORT) == 2
    assert journal.head_seq >= 4

    # 同输入重跑：规范哈希一致 (A15)
    _, again = make_engine()
    again.add_strategy(SimpleBuyAndHoldStrategy("strat-1", again))
    assert again.run(make_test_bars()).canonical_hashes() == result.canonical_hashes()


def test_official_settlement_price_is_used_when_supplied() -> None:
    _, engine = make_engine()
    engine.add_strategy(SimpleBuyAndHoldStrategy("strat-1", engine))
    result = engine.run(make_test_bars(), settlement_prices={(RB_INST, DAY_1): Decimal("3090")})
    assert result.settlement_source == "official_settlement"
    assert engine.ledger.settlements(DAY_1, RB_INST)[0].settlement_price == Decimal("3090")
    assert [e.amount for e in result.ledger_entries if e.kind == "SETTLEMENT"][0] == Decimal("500.00")
    assert result.final_equity == Decimal("100690.00")  # 两种视图共享一套事实，权益不变


# ---------------------------------------------------------------------- 撤单 / 过期释放预占


class CancelStrategy(StrategyBase):
    def __init__(self, strategy_id: str, context: StrategyContext, cancel_at_bar: int | None) -> None:
        super().__init__(strategy_id, context)
        self.n = 0
        self.cancel_at_bar = cancel_at_bar
        self.cid = ""

    def on_bar(self, bar: Bar) -> None:
        self.n += 1
        if self.n == 1:
            self.cid = self.buy(bar.instrument, 1, Offset.OPEN, limit_price_ticks=2000)
        elif self.n == self.cancel_at_bar:
            self.context.cancel_order(self.cid)


def test_cancel_releases_reservations_through_gateway_report() -> None:
    _, engine = make_engine()
    engine.add_strategy(CancelStrategy("s", engine, cancel_at_bar=2))
    result = engine.run(make_test_bars())
    order = result.orders[0]
    assert order.status == OrderStatus.CANCELLED and order.send_state == SendState.CONFIRMED_REMOTE
    assert not engine.ledger._funds_reservations  # noqa: SLF001
    assert [e.payload.status for e in result.events if e.kind == EventKind.ORDER_REPORT] == [
        OrderStatus.ACCEPTED,
        OrderStatus.CANCELLED,
    ]
    assert result.events[1].event_time == BASE_START + timedelta(hours=2)
    assert result.final_equity == Decimal("100000.00")


def test_unfilled_order_expires_at_day_roll_and_is_reported() -> None:
    _, engine = make_engine()
    engine.add_strategy(CancelStrategy("s", engine, cancel_at_bar=None))
    result = engine.run(make_test_bars())
    order = result.orders[0]
    assert order.status == OrderStatus.EXPIRED
    assert [o.client_order_id for o in result.unfilled_orders] == ["ord-1"]
    assert not engine.ledger._funds_reservations  # noqa: SLF001
    expired = [e for e in result.events if e.kind == EventKind.ORDER_REPORT and e.payload.status == OrderStatus.EXPIRED]
    assert expired[0].event_time == BASE_START + timedelta(hours=2)  # 第一日最后一根 Bar 结束


# ---------------------------------------------------------------------- 风控在回测中生效并可见


def test_exchange_limit_rejection_is_visible_in_result() -> None:
    limits = ExchangeLimits(
        [
            LimitRule(
                kind=LimitKind.MAX_OPEN_LOTS_PER_DAY,
                scope="rb",
                value=0,
                source=LimitSource.EXCHANGE,
                effective_from=date(2024, 1, 1),
                evidence_ref="test-rule",
            )
        ]
    )
    _, engine = make_engine(exchange_limits=limits)
    engine.add_strategy(SimpleBuyAndHoldStrategy("strat-1", engine))
    result = engine.run(make_test_bars())
    assert result.total_trades == 0
    assert result.rejected_intents[0].stage == "exchange-limit"
    assert result.orders[0].status == OrderStatus.REJECTED
    assert not engine.ledger._funds_reservations  # noqa: SLF001


def test_insufficient_funds_rejected_before_send() -> None:
    gateway = SimulatedGateway(account_id="acc-test", trading_day=DAY_1)
    engine = BacktestEngine(
        account_id="acc-test",
        gateway=gateway,
        start_time=BASE_START,
        initial_capital=Decimal("1000"),
        default_economics=ECO,
    )
    engine.add_strategy(SimpleBuyAndHoldStrategy("strat-1", engine))
    result = engine.run(make_test_bars())
    assert result.total_trades == 0
    assert result.rejected_intents[0].stage == "risk"
    assert "insufficient available funds" in result.rejected_intents[0].reason


def test_missing_economics_fails_fast() -> None:
    gateway = SimulatedGateway(account_id="acc-test", trading_day=DAY_1)
    engine = BacktestEngine(account_id="acc-test", gateway=gateway, start_time=BASE_START)
    with pytest.raises(MissingRuleError):
        engine.run(make_test_bars())


def test_limit_order_requires_price_and_market_forbids_price() -> None:
    _, engine = make_engine()
    engine._set_trading_day(DAY_1)  # noqa: SLF001
    from qh_trader.core.constants import OrderType, Side

    with pytest.raises(ValueError):
        engine.send_order(RB_INST, Side.BUY, Offset.OPEN, 1, OrderType.LIMIT, None)
    with pytest.raises(ValueError):
        engine.send_order(RB_INST, Side.BUY, Offset.OPEN, 1, OrderType.MARKET, 3000)


# ---------------------------------------------------------------------- 多品种经济参数与结算


class TwoInstrumentStrategy(StrategyBase):
    def __init__(self, strategy_id: str, context: StrategyContext) -> None:
        super().__init__(strategy_id, context)
        self.seen: set[InstrumentId] = set()

    def on_bar(self, bar: Bar) -> None:
        if bar.instrument not in self.seen:
            self.seen.add(bar.instrument)
            self.buy(bar.instrument, 1, Offset.OPEN)


def test_multi_instrument_uses_per_contract_economics_and_settles_all() -> None:
    gateway = SimulatedGateway(account_id="acc-test", trading_day=DAY_1)
    engine = BacktestEngine(
        account_id="acc-test", gateway=gateway, start_time=BASE_START, initial_capital=Decimal("1000000")
    )
    engine.register_instrument(
        RB_INST, InstrumentEconomics(Decimal("10"), Decimal("1"), Decimal("5"), Decimal("0.1"), "t")
    )
    engine.register_instrument(
        HC_INST, InstrumentEconomics(Decimal("20"), Decimal("1"), Decimal("7"), Decimal("0.1"), "t")
    )
    engine.add_strategy(TwoInstrumentStrategy("multi", engine))
    rb = make_test_bars()
    hc = [
        make_bar(i, *row, instrument=HC_INST)
        for i, row in enumerate(
            [
                ("100", "110", "95", "105", DAY_1),
                ("105", "115", "100", "110", DAY_1),
                ("110", "120", "105", "115", DAY_2),
                ("115", "125", "110", "120", DAY_2),
            ]
        )
    ]
    result = engine.run(rb + hc)
    assert result.total_commission == Decimal("12")  # 5 + 7
    assert {
        str(s.instrument): s.settlement_price
        for s in (*engine.ledger.settlements(DAY_1, RB_INST), *engine.ledger.settlements(DAY_1, HC_INST))
    } == {
        "SHFE.rb2410": Decimal("3080"),
        "SHFE.hc2410": Decimal("110"),
    }
    # rb: 3040 -> 3130 = +900; hc: 105 -> 120 = +15 × 20 = +300；手续费 12
    assert result.final_equity == Decimal("1000000") + Decimal("900") + Decimal("300") - Decimal("12")


# ---------------------------------------------------------------------- 定时器与空行情时段


class TimerStrategy(StrategyBase):
    def __init__(self, strategy_id: str, context: StrategyContext, fire_at: datetime) -> None:
        super().__init__(strategy_id, context)
        self.fire_at = fire_at
        self.fired: list[datetime] = []
        self.armed = False

    def on_bar(self, bar: Bar) -> None:
        if not self.armed:
            self.armed = True
            self.context.schedule_timer(self.fire_at, "heartbeat", {"n": 1})

    def on_timer(self, timer: TimerEvent) -> None:
        self.fired.append(self.context.now())


def test_timer_fires_inside_empty_market_gap() -> None:
    bars = [
        make_bar(0, "3000", "3050", "2990", "3040", DAY_1),
        make_bar(1, "3040", "3100", "3030", "3080", DAY_1, start=BASE_START + timedelta(hours=5)),  # 4 小时空档
    ]
    _, engine = make_engine()
    fire_at = BASE_START + timedelta(hours=3)
    strat = TimerStrategy("t", engine, fire_at)
    engine.add_strategy(strat)
    result = engine.run(bars)
    assert strat.fired == [fire_at]
    timers = [e for e in result.events if e.kind == EventKind.TIMER]
    assert len(timers) == 1 and timers[0].event_time == fire_at
