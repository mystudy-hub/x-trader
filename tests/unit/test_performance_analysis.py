"""Unit tests for performance analysis and validation (S3-06, S3-07, FR-VAL-01~03)."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from qh_trader.analysis.performance import _compute_paired_trade_pnls, calculate_performance
from qh_trader.analysis.validation import train_test_split, walk_forward_slices
from qh_trader.analysis.visualizer import format_performance_summary
from qh_trader.core.constants import Exchange, Offset, Side
from qh_trader.core.objects import InstrumentId, Trade, TradeKey
from qh_trader.engine.backtest_engine import BacktestResult, EquitySnapshot

BASE_TIME = datetime(2024, 9, 10, 1, 0, tzinfo=timezone.utc)
TRADING_DAY = date(2024, 9, 10)
RB_INST = InstrumentId(Exchange.SHFE, "rb2410")


def make_dummy_result() -> BacktestResult:
    snapshots = (
        EquitySnapshot(BASE_TIME, TRADING_DAY, Decimal("100000"), Decimal("100000"), Decimal(0), Decimal(0), Decimal(0), 0, 0, Decimal("3000")),
        EquitySnapshot(BASE_TIME, TRADING_DAY, Decimal("102000"), Decimal("102000"), Decimal("2000"), Decimal("2000"), Decimal("10"), 1, 0, Decimal("3020")),
        EquitySnapshot(BASE_TIME, TRADING_DAY, Decimal("101000"), Decimal("101000"), Decimal("1000"), Decimal("1000"), Decimal("20"), 0, 0, Decimal("3010")),
        EquitySnapshot(BASE_TIME, TRADING_DAY, Decimal("105000"), Decimal("105000"), Decimal("5000"), Decimal("5000"), Decimal("30"), 1, 0, Decimal("3050")),
    )
    # 构造 4 笔真实成交：
    # Trade 1: 买开 1 手 3000
    # Trade 2: 卖平 1 手 3050 -> 盈利 (3050-3000)*10 - 10 = +490
    # Trade 3: 买开 1 手 3100
    # Trade 4: 卖平 1 手 3080 -> 亏损 (3080-3100)*10 - 10 = -210
    # 2 笔平仓：1 赢 1 输，胜率 50%，盈亏比 490 / 210 = 2.3333
    trades = (
        Trade(
            account_id="acc1",
            instrument=RB_INST,
            trading_day=TRADING_DAY,
            trade_id="t1",
            side=Side.BUY,
            offset=Offset.OPEN,
            quantity=1,
            price=Decimal("3000"),
            event_time=BASE_TIME,
            available_at=BASE_TIME,
            deduplication_key=TradeKey("acc1", Exchange.SHFE, TRADING_DAY, "t1"),
        ),
        Trade(
            account_id="acc1",
            instrument=RB_INST,
            trading_day=TRADING_DAY,
            trade_id="t2",
            side=Side.SELL,
            offset=Offset.CLOSE,
            quantity=1,
            price=Decimal("3050"),
            event_time=BASE_TIME,
            available_at=BASE_TIME,
            deduplication_key=TradeKey("acc1", Exchange.SHFE, TRADING_DAY, "t2"),
        ),
        Trade(
            account_id="acc1",
            instrument=RB_INST,
            trading_day=TRADING_DAY,
            trade_id="t3",
            side=Side.BUY,
            offset=Offset.OPEN,
            quantity=1,
            price=Decimal("3100"),
            event_time=BASE_TIME,
            available_at=BASE_TIME,
            deduplication_key=TradeKey("acc1", Exchange.SHFE, TRADING_DAY, "t3"),
        ),
        Trade(
            account_id="acc1",
            instrument=RB_INST,
            trading_day=TRADING_DAY,
            trade_id="t4",
            side=Side.SELL,
            offset=Offset.CLOSE,
            quantity=1,
            price=Decimal("3080"),
            event_time=BASE_TIME,
            available_at=BASE_TIME,
            deduplication_key=TradeKey("acc1", Exchange.SHFE, TRADING_DAY, "t4"),
        ),
    )
    return BacktestResult(
        account_id="acc1",
        initial_capital=Decimal("100000"),
        final_equity=Decimal("105000"),
        total_pnl=Decimal("5000"),
        total_commission=Decimal("20"),
        total_trades=4,
        equity_snapshots=snapshots,
        trades=trades,
        orders=(),
    )


def test_calculate_performance_metrics() -> None:
    res = make_dummy_result()
    metrics = calculate_performance(res, annual_trading_days=242)
    assert metrics.initial_capital == Decimal("100000")
    assert metrics.final_equity == Decimal("105000")
    assert metrics.total_pnl == Decimal("5000")
    assert metrics.total_trades == 4

    # 真实的逐笔配对胜率检验：2 笔平仓中 1 赢 1 输
    assert metrics.winning_trades == 1
    assert metrics.losing_trades == 1
    assert metrics.win_rate == Decimal("0.5")
    # 490 / 210 = 2.3333
    assert abs(metrics.profit_loss_ratio - Decimal("2.3333")) < Decimal("0.001")

    summary = format_performance_summary(metrics)
    assert "回测绩效分析报告" in summary
    assert "105,000.00" in summary


def test_train_test_split() -> None:
    items = list(range(10))
    train, test = train_test_split(items, split_ratio=0.7)
    assert train == (0, 1, 2, 3, 4, 5, 6)
    assert test == (7, 8, 9)


def test_walk_forward_slices() -> None:
    items = list(range(10))
    slices = walk_forward_slices(items, train_size=4, test_size=2)
    assert len(slices) == 3
    assert slices[0] == ((0, 1, 2, 3), (4, 5))
    assert slices[1] == ((2, 3, 4, 5), (6, 7))
