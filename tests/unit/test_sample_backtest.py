"""Independent PnL expectations and execution timing; no optional local market-data dependency."""

from dataclasses import replace
from decimal import Decimal
from unittest.mock import Mock

import pytest

from qh_trader.core.constants import MissingRuleError
from qh_trader.data.execution_reference import derive_execution_references
from qh_trader.data.storage import ParquetDataStorage
from qh_trader.research.sample_backtest import ResearchPositionBook, simulate_trend
from scripts.run_sample_backtest import run_trend_backtest


def publish(tmp_path, instrument, bars, timings, with_references=True):
    storage = ParquetDataStorage(tmp_path)
    references = derive_execution_references(bars, {value.bar_start: value for value in timings.values()})
    storage.publish_batch(instrument, "1d", bars=bars, execution_references=references if with_references else ())
    return storage


def test_research_book_realizes_partial_closes_and_reversals_once():
    book = ResearchPositionBook(Decimal("1000000"), Decimal(10), Decimal(5))
    book.fill_target(10, Decimal(100))
    assert book.equity(Decimal(100)) == Decimal(999950)
    book.fill_target(6, Decimal(110))
    assert book.realized_pnl == Decimal(400)
    assert book.equity(Decimal(110)) == Decimal(1000930)
    book.fill_target(-2, Decimal(90))
    assert book.realized_pnl == Decimal(-200)
    assert book.cost == Decimal(90)
    assert book.equity(Decimal(90)) == Decimal(999690)
    book.fill_target(0, Decimal(80))
    assert book.realized_pnl == 0
    assert book.equity(Decimal(80)) == Decimal(999880)


def test_two_bars_cannot_backfill_a_trade_after_the_last_signal(
    tmp_path,
    sample_catalog,
    sample_instrument,
    sample_bars,
    timing_rows,
):
    publish(tmp_path, sample_instrument, sample_bars[:2], timing_rows)
    result = run_trend_backtest(storage_dir=tmp_path, catalog=sample_catalog, fast_window=1, slow_window=2)
    assert result["trades_count"] == 0
    assert result["final_equity"] == Decimal("1000000")


def test_first_entry_does_not_earn_pre_entry_price_movement(
    tmp_path,
    sample_catalog,
    sample_instrument,
    sample_bars,
    timing_rows,
):
    bars = sample_bars[:3]
    bars[2] = replace(
        bars[2], open=Decimal(110), high=Decimal(110), low=Decimal(110), close=Decimal(110), turnover=Decimal(11000)
    )
    publish(tmp_path, sample_instrument, bars, timing_rows)
    result = run_trend_backtest(storage_dir=tmp_path, catalog=sample_catalog, fast_window=1, slow_window=2)
    assert result["trades_count"] == 1
    assert result["final_equity"] == Decimal(999950)
    assert result["fills"][0]["at"] == bars[2].open_time.isoformat()
    assert result["fills"][0]["signal_at"] == bars[1].meta.available_at.isoformat()


def test_run_trend_backtest_has_independent_round_trip_expectation(
    tmp_path,
    sample_catalog,
    sample_instrument,
    sample_bars,
    timing_rows,
):
    storage = publish(tmp_path, sample_instrument, sample_bars, timing_rows)
    result = run_trend_backtest(storage_dir=tmp_path, catalog=sample_catalog, fast_window=1, slow_window=2)
    # Buy 10 at 120; close those 10 at 80 and sell 10 at 80. Fees: 50 + 100.
    assert result["realized_pnl"] == Decimal(-4000)
    assert result["fees"] == Decimal(150)
    assert result["final_equity"] == Decimal(995850)
    assert [fill["quantity"] for fill in result["fills"]] == [10, -20]
    assert result["ending_position"] == -10
    assert result["validation_scope"] == "research_prototype"
    assert result["snapshot_id"] == storage.capture_snapshot().snapshot_id


def test_missing_execution_price_fails_instead_of_borrowing_daily_open(
    tmp_path,
    sample_catalog,
    sample_instrument,
    sample_bars,
    timing_rows,
):
    publish(tmp_path, sample_instrument, sample_bars, timing_rows, with_references=False)
    with pytest.raises(MissingRuleError, match="opening price"):
        run_trend_backtest(storage_dir=tmp_path, catalog=sample_catalog, fast_window=1, slow_window=2)


def test_missing_canonical_data_is_a_failure_not_an_empty_success(tmp_path, sample_catalog):
    with pytest.raises(ValueError, match="canonical"):
        run_trend_backtest(storage_dir=tmp_path, catalog=sample_catalog)
    with pytest.raises(MissingRuleError, match="catalog"):
        run_trend_backtest(storage_dir=tmp_path)


def test_empty_port_view_cannot_be_bypassed_by_prefetched_prices(sample_instrument, sample_bars):
    market = Mock()
    market.bars.return_value = ()
    with pytest.raises(ValueError, match="no visible bars"):
        simulate_trend(
            market,
            sample_instrument,
            "1d",
            [bar.meta.available_at for bar in sample_bars],
            [],
            multiplier=Decimal(10),
            fast_window=1,
            slow_window=2,
        )
    assert not market.execution_reference.called


def test_port_returning_future_data_is_rejected(sample_instrument, sample_bars):
    market = Mock()
    market.bars.return_value = sample_bars
    with pytest.raises(ValueError, match="future"):
        simulate_trend(
            market,
            sample_instrument,
            "1d",
            [sample_bars[0].open_time],
            [],
            multiplier=Decimal(10),
            fast_window=1,
            slow_window=2,
        )
