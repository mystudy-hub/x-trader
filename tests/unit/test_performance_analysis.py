"""Unit tests for performance analysis, validation and sensitivity (S3-06, S3-07, FR-VAL-01~03)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from qh_trader.analysis.performance import calculate_performance
from qh_trader.analysis.validation import (
    SampleSplit,
    run_sensitivity,
    train_test_split,
    walk_forward_slices,
)
from qh_trader.analysis.visualizer import equity_curve_csv, equity_curve_svg, format_performance_summary
from qh_trader.core.constants import Exchange, Offset, Side
from qh_trader.core.objects import InstrumentId, Trade, TradeKey
from qh_trader.domain.ledger import ClosedTradeRecord
from qh_trader.engine.backtest_engine import BacktestResult, EquitySnapshot

BASE_TIME = datetime(2024, 9, 10, 1, 0, tzinfo=timezone.utc)
DAY_1 = date(2024, 9, 10)
DAY_2 = date(2024, 9, 11)
RB_INST = InstrumentId(Exchange.SHFE, "rb2410")


def snap(hours: int, day: date, balance: str, equity: str, margin: str = "0") -> EquitySnapshot:
    return EquitySnapshot(
        BASE_TIME + timedelta(hours=hours),
        day,
        Decimal(balance),
        Decimal(equity),
        Decimal(margin),
        Decimal(0),
        Decimal(0),
        Decimal(0),
        0,
        0,
        Decimal("3000"),
    )


def trade(tid: str, side: Side, offset: Offset, price: str, day: date) -> Trade:
    return Trade(
        account_id="acc1",
        instrument=RB_INST,
        trading_day=day,
        trade_id=tid,
        side=side,
        offset=offset,
        quantity=1,
        price=Decimal(price),
        event_time=BASE_TIME,
        available_at=BASE_TIME,
        deduplication_key=TradeKey("acc1", Exchange.SHFE, day, tid),
    )


def closed(
    tid: str, close_price: str, open_price: str, day: date, matched: str, commission: str = "5"
) -> ClosedTradeRecord:
    cp, op = Decimal(close_price), Decimal(open_price)
    return ClosedTradeRecord(
        trade_id=tid,
        instrument=RB_INST,
        close_side=Side.SELL,
        offset=Offset.CLOSE,
        close_price=cp,
        quantity=1,
        multiplier=Decimal("10"),
        close_date=day,
        mtm_benchmark_price=op,
        mtm_close_pnl=(cp - op) * 10,
        matched_open_price=op,
        trade_close_pnl=(cp - op) * 10,
        commission=Decimal(commission),
        matched_lot_ids=(matched,),
    )


def make_dummy_result() -> BacktestResult:
    # 两日：第一日 +2000 后回落到 +1000；第二日 +5000。保证金峰值 3050。
    snapshots = (
        snap(0, DAY_1, "100000", "100000"),
        snap(1, DAY_1, "101990", "102000", "3020"),
        snap(2, DAY_2, "100990", "101000", "3050"),
        snap(3, DAY_2, "105000", "105000"),
    )
    trades = (
        trade("t1", Side.BUY, Offset.OPEN, "3000", DAY_1),
        trade("t2", Side.SELL, Offset.CLOSE, "3050", DAY_1),
        trade("t3", Side.BUY, Offset.OPEN, "3100", DAY_1),
        trade("t4", Side.SELL, Offset.CLOSE, "3080", DAY_2),
    )
    # 账本逐笔平仓事实：+500-5 = +495 赢；-200-5 = -205 输
    records = (closed("t2", "3050", "3000", DAY_1, "t1"), closed("t4", "3080", "3100", DAY_2, "t3"))
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
        closed_trades=records,
        first_trading_day=DAY_1,
        last_trading_day=DAY_2,
    )


def test_calculate_performance_uses_ledger_facts_and_daily_frequency() -> None:
    res = make_dummy_result()
    metrics = calculate_performance(res, annual_trading_days=242)
    assert metrics.total_trades == 4
    assert metrics.closed_trades == 2
    assert (metrics.winning_trades, metrics.losing_trades) == (1, 1)
    assert metrics.win_rate == Decimal("0.5")
    assert metrics.profit_loss_ratio == (Decimal("495") / Decimal("205")).quantize(Decimal("0.0001"))
    assert metrics.trading_days == 2
    assert metrics.return_frequency == "daily" and metrics.annual_trading_days == 242
    assert metrics.peak_margin_used == Decimal("3050")
    assert metrics.average_holding_days == Decimal("0.50")  # (0 + 1) / 2
    assert metrics.max_drawdown_amount == Decimal("1000.0")  # 102000 -> 101000
    assert metrics.total_return == Decimal("0.05")
    # 年化按交易日数折算：(1.05)^(242/2) - 1，而不是按 Bar 数
    assert metrics.annualized_return > Decimal("100")
    assert metrics.commission_ratio == (Decimal("20") / Decimal("5020")).quantize(Decimal("0.0001"))
    assert list(metrics.monthly_returns) == ["2024-09"]
    assert metrics.instrument_contributions == {"SHFE.rb2410": Decimal("290")}
    assert metrics.instrument_volume == {"SHFE.rb2410": 4}

    summary = format_performance_summary(metrics)
    assert "105,000.00" in summary and "年化因子=242" in summary
    csv = equity_curve_csv(res.equity_snapshots)
    assert csv.splitlines()[0].startswith("timestamp,trading_day,balance,total_equity,margin_used")
    assert len(csv.splitlines()) == 5
    assert equity_curve_svg(res.equity_snapshots).startswith("<svg")


def test_calculate_performance_handles_empty_result() -> None:
    res = BacktestResult("acc", Decimal("1"), Decimal("1"), Decimal(0), Decimal(0), 0, (), (), ())
    metrics = calculate_performance(res)
    assert metrics.trading_days == 0 and metrics.sharpe_ratio == 0


def test_train_test_split_and_sample_split() -> None:
    items = list(range(10))
    train, test = train_test_split(items, split_ratio=0.7)
    assert train == (0, 1, 2, 3, 4, 5, 6)
    assert test == (7, 8, 9)
    split = SampleSplit(train, test)
    assert len(split) == 10
    with pytest.raises(ValueError):
        train_test_split(items, split_ratio=1.0)


def test_walk_forward_slices_with_purge_gap() -> None:
    items = list(range(10))
    slices = walk_forward_slices(items, train_size=4, test_size=2)
    assert len(slices) == 3
    assert slices[0] == ((0, 1, 2, 3), (4, 5))
    assert slices[1] == ((2, 3, 4, 5), (6, 7))
    purged = walk_forward_slices(items, train_size=4, test_size=2, gap=1)
    assert purged[0] == ((0, 1, 2, 3), (5, 6))
    assert all(max(tr) < min(te) for tr, te in purged)


def test_run_sensitivity_records_failures_and_counts() -> None:
    calls: list[dict] = []

    def runner(params):
        calls.append(dict(params))
        if params["slippage"] == 4:
            raise ValueError("no data at 4 ticks")
        return {"pnl": Decimal(params["commission"]) * -1 - params["slippage"]}

    report = run_sensitivity(
        {"commission": Decimal("5"), "slippage": 0},
        {"commission": [Decimal("0"), Decimal("10")], "slippage": [2, 4]},
        runner,
    )
    assert report.trial_count == 4 and report.failure_count == 1
    failed = [t for t in report.trials if not t.succeeded][0]
    assert failed.parameters == {"commission": "5", "slippage": "4"}
    assert "no data at 4 ticks" in (failed.error or "")
    as_dict = report.as_dict()
    assert as_dict["dimensions"] == {"commission": ["0", "10"], "slippage": ["2", "4"]}
    grid = run_sensitivity({"a": 1, "b": 1}, {"a": [1, 2], "b": [1, 2, 3]}, lambda p: {"ok": 1}, mode="grid")
    assert grid.trial_count == 6
