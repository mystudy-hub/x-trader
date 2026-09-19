"""Unit tests for BacktestEngine (S3-02, FR-MATCH-01~05, FR-EXEC-01~03)."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from qh_trader.core.constants import Exchange, Offset, OrderType, QualityFlag, Side
from qh_trader.core.objects import Bar, InstrumentId, RecordMeta
from qh_trader.engine.backtest_engine import BacktestEngine
from qh_trader.gateway.simulated_gateway import SimulatedGateway
from qh_trader.strategy.base import StrategyBase, StrategyContext

RB_INST = InstrumentId(Exchange.SHFE, "rb2410")
BASE_START = datetime(2024, 9, 10, 1, 0, tzinfo=timezone.utc)
DAY_1 = date(2024, 9, 10)
DAY_2 = date(2024, 9, 11)


class SimpleBuyAndHoldStrategy(StrategyBase):
    """第 1 根 Bar 开仓买入 1 手，第 3 根 Bar 平仓."""

    def __init__(self, strategy_id: str, context: StrategyContext) -> None:
        super().__init__(strategy_id, context)
        self.bar_count = 0

    def on_bar(self, bar: Bar) -> None:
        self.bar_count += 1
        if self.bar_count == 1:
            # 开多 1 手
            self.context.buy(bar.instrument, quantity=1, offset=Offset.OPEN)
        elif self.bar_count == 3:
            # 平多 1 手
            self.context.sell(bar.instrument, quantity=1, offset=Offset.CLOSE)


def make_test_bars() -> list[Bar]:
    bars = []
    prices = [
        ("3000", "3050", "2990", "3040", DAY_1),
        ("3040", "3100", "3030", "3080", DAY_1),
        ("3080", "3120", "3070", "3110", DAY_2),  # 跨日
        ("3110", "3150", "3100", "3130", DAY_2),
    ]
    for i, (op, hi, lo, cl, d) in enumerate(prices):
        start = BASE_START + timedelta(hours=i)
        end = start + timedelta(hours=1)
        meta = RecordMeta(
            event_time=end,
            available_at=end,
            ingested_at=end,
            trading_day=d,
            source_id="test",
            source_version="v1",
            ingest_seq=i + 1,
        )
        bars.append(
            Bar(
                instrument=RB_INST,
                meta=meta,
                bar_start=start,
                bar_end=end,
                open_time=start,
                interval="1h",
                open=Decimal(op),
                high=Decimal(hi),
                low=Decimal(lo),
                close=Decimal(cl),
                volume=100,
                turnover=Decimal("100000"),
                open_interest=50000,
                includes_auction=False,
            )
        )
    return bars


def test_backtest_engine_run_and_settlement() -> None:
    gateway = SimulatedGateway(
        account_id="acc-test",
        trading_day=DAY_1,
        slippage_ticks=0,
    )
    engine = BacktestEngine(
        account_id="acc-test",
        gateway=gateway,
        start_time=BASE_START,
        initial_capital=Decimal("100000.00"),
        contract_multiplier=Decimal("10"),
        commission_per_lot=Decimal("5.0"),
    )
    strat = SimpleBuyAndHoldStrategy("strat-1", engine)
    engine.add_strategy(strat)

    bars = make_test_bars()
    result = engine.run(bars)

    assert result.account_id == "acc-test"
    assert result.initial_capital == Decimal("100000.00")
    assert len(result.equity_snapshots) == 4

    # 验证交易记录：
    # Bar 1 结束后发出买单；在 Bar 2 开盘 (open=3040) 撮合成交 1 手买开
    # Bar 3 结束后发出卖单；在 Bar 4 开盘 (open=3110) 撮合成交 1 手卖平
    assert result.total_trades == 2
    trade_open, trade_close = result.trades
    assert trade_open.offset == Offset.OPEN
    assert trade_open.price == Decimal("3040")
    assert trade_open.quantity == 1

    assert trade_close.offset == Offset.CLOSE or trade_close.offset == Offset.CLOSE_YESTERDAY
    assert trade_close.price == Decimal("3110")
    assert trade_close.quantity == 1

    # 盈亏计算：
    # 开仓 3040，平仓 3110，价差 70 点 * 10 乘数 * 1 手 = 700 元毛利
    # 手续费 2 笔 * 5 元 = 10 元
    # 净利润 = 690 元
    expected_profit = Decimal("690.00")
    assert result.total_commission == Decimal("10.0")
    assert result.total_pnl == expected_profit
    assert result.final_equity == Decimal("100690.00")
