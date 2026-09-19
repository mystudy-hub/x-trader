"""Unit tests for performance analysis and validation (S3-06, S3-07, FR-VAL-01~03)."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from qh_trader.analysis.performance import calculate_performance
from qh_trader.analysis.validation import train_test_split, walk_forward_slices
from qh_trader.analysis.visualizer import format_performance_summary
from qh_trader.engine.backtest_engine import BacktestResult, EquitySnapshot

BASE_TIME = datetime(2024, 9, 10, 1, 0, tzinfo=timezone.utc)
TRADING_DAY = date(2024, 9, 10)


def make_dummy_result() -> BacktestResult:
    snapshots = (
        EquitySnapshot(BASE_TIME, TRADING_DAY, Decimal("100000"), Decimal("100000"), Decimal(0), Decimal(0), Decimal(0), 0, 0, Decimal("3000")),
        EquitySnapshot(BASE_TIME, TRADING_DAY, Decimal("102000"), Decimal("102000"), Decimal("2000"), Decimal("2000"), Decimal("10"), 1, 0, Decimal("3020")),
        EquitySnapshot(BASE_TIME, TRADING_DAY, Decimal("101000"), Decimal("101000"), Decimal("1000"), Decimal("1000"), Decimal("20"), 0, 0, Decimal("3010")),
        EquitySnapshot(BASE_TIME, TRADING_DAY, Decimal("105000"), Decimal("105000"), Decimal("5000"), Decimal("5000"), Decimal("30"), 1, 0, Decimal("3050")),
    )
    return BacktestResult(
        account_id="acc1",
        initial_capital=Decimal("100000"),
        final_equity=Decimal("105000"),
        total_pnl=Decimal("5000"),
        total_commission=Decimal("30"),
        total_trades=4,
        equity_snapshots=snapshots,
        trades=(),
        orders=(),
    )


def test_calculate_performance_metrics() -> None:
    res = make_dummy_result()
    metrics = calculate_performance(res, annual_trading_days=242)
    assert metrics.initial_capital == Decimal("100000")
    assert metrics.final_equity == Decimal("105000")
    assert metrics.total_pnl == Decimal("5000")
    assert metrics.total_return == Decimal("0.05")
    assert metrics.total_trades == 4
    assert metrics.total_commission == Decimal("30")

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
    # slice 0: train=0..3, test=4..5
    assert slices[0] == ((0, 1, 2, 3), (4, 5))
    # slice 1: train=2..5, test=6..7
    assert slices[1] == ((2, 3, 4, 5), (6, 7))
