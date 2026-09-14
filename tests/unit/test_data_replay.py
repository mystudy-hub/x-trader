"""Exact execution observations and snapshot/visibility boundaries."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from qh_trader.core.constants import PriceType
from qh_trader.core.ports import MarketDataPort
from qh_trader.data.execution_reference import derive_execution_references
from qh_trader.data.replay import HistoricalMarketDataAdapter
from qh_trader.data.storage import ParquetDataStorage


def test_market_data_visibility_and_port_conformance(tmp_path, sample_instrument, sample_bars):
    storage = ParquetDataStorage(tmp_path)
    storage.save_bars(sample_bars, sample_instrument, "1d")
    adapter = HistoricalMarketDataAdapter(storage)
    assert isinstance(adapter, MarketDataPort)
    at = sample_bars[0].meta.available_at
    assert adapter.bars(sample_instrument, "1d", at - timedelta(microseconds=1)) == ()
    assert adapter.bars(sample_instrument, "1d", at) == (sample_bars[0],)


def test_reader_pins_snapshot_before_loading_prices(tmp_path, sample_instrument, sample_bars):
    storage = ParquetDataStorage(tmp_path)
    storage.save_bars(sample_bars, sample_instrument, "1d")
    adapter = HistoricalMarketDataAdapter(storage)
    changed = replace(sample_bars[0], open=Decimal(101), high=Decimal(101), low=Decimal(101), close=Decimal(101))
    storage.save_bars([changed], sample_instrument, "1d")
    until = sample_bars[-1].meta.available_at
    assert adapter.bars(sample_instrument, "1d", until)[0] == sample_bars[0]
    assert HistoricalMarketDataAdapter(storage).bars(sample_instrument, "1d", until)[0] == changed


def test_bar_open_is_not_an_execution_observation_without_timing_evidence(tmp_path, sample_instrument, sample_bars):
    storage = ParquetDataStorage(tmp_path)
    storage.save_bars(sample_bars, sample_instrument, "1d")
    adapter = HistoricalMarketDataAdapter(storage, execution_interval="1d")
    bar = sample_bars[0]
    assert (
        adapter.execution_reference(
            sample_instrument, bar.meta.session_id, bar.open_time, PriceType.BAR_OPEN, bar.meta.available_at
        )
        is None
    )


def test_execution_reference_matches_time_session_type_and_visibility(
    tmp_path, sample_instrument, sample_bars, timing_rows
):
    storage = ParquetDataStorage(tmp_path)
    references = derive_execution_references(sample_bars, {row.bar_start: row for row in timing_rows.values()})
    storage.publish_batch(sample_instrument, "1d", bars=sample_bars, execution_references=references)
    adapter = HistoricalMarketDataAdapter(storage, execution_interval="1d")
    bar = sample_bars[0]
    at = bar.open_time
    reference = adapter.execution_reference(sample_instrument, bar.meta.session_id, at, PriceType.BAR_OPEN, at)
    assert reference.price == bar.open
    assert reference.available_volume is None
    assert (
        adapter.execution_reference(
            sample_instrument,
            "afternoon",
            at + timedelta(hours=4, minutes=30),
            PriceType.BAR_OPEN,
            bar.meta.available_at,
        )
        is None
    )
    assert adapter.execution_reference(sample_instrument, "wrong-session", at, PriceType.BAR_OPEN, at) is None
    assert (
        adapter.execution_reference(sample_instrument, bar.meta.session_id, at, PriceType.DAY_SESSION_OPEN, at) is None
    )
    assert (
        adapter.execution_reference(
            sample_instrument, bar.meta.session_id, at, PriceType.BAR_OPEN, at - timedelta(microseconds=1)
        )
        is None
    )


def test_unknown_or_late_open_publication_does_not_get_backdated(tmp_path, sample_instrument, sample_bars, timing_rows):
    timings = {row.bar_start: replace(row, open_available_at=None) for row in timing_rows.values()}
    assert derive_execution_references(sample_bars, timings) == []
    timings = {row.bar_start: replace(row, open_available_at=row.bar_end) for row in timing_rows.values()}
    references = derive_execution_references(sample_bars, timings)
    storage = ParquetDataStorage(tmp_path)
    storage.save_execution_references(references, sample_instrument, "1d")
    adapter = HistoricalMarketDataAdapter(storage, execution_interval="1d")
    bar = sample_bars[0]
    assert (
        adapter.execution_reference(
            sample_instrument, bar.meta.session_id, bar.open_time, PriceType.BAR_OPEN, bar.open_time
        )
        is None
    )
