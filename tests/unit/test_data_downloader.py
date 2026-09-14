"""Archive-only and validated publication are distinct, deterministic paths."""

import json
from dataclasses import replace
from datetime import date
from typing import get_type_hints
from unittest.mock import MagicMock, patch

import pytest

from qh_trader.core.constants import MissingRuleError
from qh_trader.data.downloader import FuturesDataDownloader, parse_instrument
from qh_trader.data.schemas import DataValidationError, SettlementPublication, parse_time
from qh_trader.data.sources import SinaFuturesDataSource


def configured(tmp_path, sample_catalog, sample_calendar, timing_rows):
    return FuturesDataDownloader(
        "sina", tmp_path, resolver=sample_catalog, calendar=sample_calendar, timings=timing_rows
    )


def test_parse_instrument_needs_exchange_or_catalog(sample_catalog, sample_instrument):
    assert parse_instrument("SHFE.rb2410") == sample_instrument
    assert parse_instrument("rb2410", resolver=sample_catalog) == sample_instrument
    with pytest.raises(ValueError, match="exchange"):
        parse_instrument("unknown2410")
    with pytest.raises(ValueError, match="short codes"):
        parse_instrument("CZCE.TA405")
    assert get_type_hints(FuturesDataDownloader.download_batch)["return"]


def test_raw_observations_are_archived_without_becoming_canonical(tmp_path, source_records, sample_instrument):
    downloader = FuturesDataDownloader("sina", tmp_path)
    records = [dict(source_records[0], turnover=None)]
    with patch.object(downloader.data_source, "fetch_daily_bars", return_value=records):
        raw = downloader.download_raw(sample_instrument)
    assert raw.path.is_file()
    assert not raw.quality.is_clean
    payload = json.loads(raw.path.read_text(encoding="utf-8"))
    assert payload["records"][0]["turnover"] is None
    assert payload["status"] == "raw_observation"
    assert downloader.storage.read_bars(sample_instrument, "1d") == []
    with pytest.raises(MissingRuleError, match="catalog"):
        downloader.download_bars(sample_instrument)


def test_downloader_publishes_complete_metadata_and_exact_references(
    tmp_path,
    source_records,
    sample_instrument,
    sample_catalog,
    sample_calendar,
    timing_rows,
):
    downloader = configured(tmp_path, sample_catalog, sample_calendar, timing_rows)
    with patch.object(downloader.data_source, "fetch_daily_bars", return_value=source_records):
        path, quality, bars = downloader.download_bars(sample_instrument)
    assert quality.is_clean and path.is_file()
    assert downloader.storage.read_bars(sample_instrument, "1d") == bars
    assert len(downloader.storage.read_execution_references(sample_instrument, "1d")) == len(bars)
    entry = next(iter(downloader.storage.capture_snapshot().datasets.values()))
    assert entry["provenance"]["catalog_version"] == "synthetic-v1"
    assert entry["provenance"]["time_assumptions"] == ("synthetic test timing",)


def test_lenient_diagnostics_still_cannot_publish_invalid_prices(
    tmp_path,
    source_records,
    sample_instrument,
    sample_catalog,
    sample_calendar,
    timing_rows,
):
    downloader = configured(tmp_path, sample_catalog, sample_calendar, timing_rows)
    bad = [dict(source_records[0], open="-100", high="-100", low="-100", close="-100")]
    with patch.object(downloader.data_source, "fetch_daily_bars", return_value=bad):
        with pytest.raises(DataValidationError):
            downloader.download_bars(sample_instrument, strict_quality=False)
    assert not downloader.storage.manifest_path.exists()
    assert len(list((tmp_path / "raw").glob("*.json"))) == 1


def test_settlement_requires_evidence_and_is_published_in_same_snapshot(
    tmp_path,
    source_records,
    sample_instrument,
    sample_catalog,
    sample_calendar,
    timing_rows,
):
    downloader = configured(tmp_path, sample_catalog, sample_calendar, timing_rows)
    records = [dict(source_records[0], settlement_price="100.5")]
    with patch.object(downloader.data_source, "fetch_daily_bars", return_value=records):
        with pytest.raises(ValueError, match="publication"):
            downloader.download_bars(sample_instrument)
        assert not downloader.storage.manifest_path.exists()
        downloader.publications = {
            "2024-09-09": SettlementPublication(
                published_at=parse_time("2024-09-09T17:10:00+08:00"),
                available_at=parse_time("2024-09-09T17:11:00+08:00"),
                is_final=True,
                evidence_ref="synthetic-publication",
            )
        }
        downloader.download_bars(sample_instrument)
    snapshot = downloader.storage.capture_snapshot()
    assert len(snapshot.datasets) == 3
    settlements = downloader.storage.read_settlements(sample_instrument, snapshot=snapshot)
    assert len(settlements) == 1 and settlements[0].pre_settlement_price is None


def test_source_records_must_be_inside_the_catalog_lifetime(
    tmp_path,
    source_records,
    sample_instrument,
    sample_catalog,
    sample_calendar,
    timing_rows,
):
    downloader = configured(tmp_path, sample_catalog, sample_calendar, timing_rows)
    downloader.timings["2024-10-16"] = replace(timing_rows["2024-09-09"], trading_day=date(2024, 10, 16))
    with patch.object(
        downloader.data_source, "fetch_daily_bars", return_value=[dict(source_records[0], date="2024-10-16")]
    ):
        with pytest.raises(MissingRuleError, match="no contract"):
            downloader.download_bars(sample_instrument)
    assert not downloader.storage.manifest_path.exists()


def test_invalid_provider_body_is_retained_as_failure_evidence(tmp_path, sample_instrument):
    source = SinaFuturesDataSource()
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b"<invalid-provider-response>"
    downloader = FuturesDataDownloader(source, tmp_path)
    with patch("qh_trader.data.sources.urllib.request.urlopen", return_value=response):
        with pytest.raises(ValueError):
            downloader.download_raw(sample_instrument)
    path = next((tmp_path / "raw").glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "parse_or_fetch_failed"
    assert payload["captures"][0]["body"] == "<invalid-provider-response>"
    assert not downloader.storage.manifest_path.exists()


def test_publish_cli_requires_evidence_before_downloading():
    from scripts.download_data import main

    with pytest.raises(SystemExit) as error:
        main(["--publish", "--symbols", "SHFE.rb2410"])
    assert error.value.code == 2
