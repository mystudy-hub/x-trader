"""[Data 层] 通达信研究发布的缺字段、时间证据和结算隔离 (S1-12, A16)。"""

import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from qh_trader.core.constants import QualityFlag
from qh_trader.data.downloader import FuturesDataDownloader
from qh_trader.data.schemas import (
    DataValidationError,
    convert_daily_records_to_bars,
    convert_daily_records_to_settlements,
    parse_time,
    validate_ohlc_records,
)
from qh_trader.data.sources import SinaFuturesDataSource
from scripts.report import generate_markdown_report


class ResearchSource(SinaFuturesDataSource):
    """用固定响应隔离发布契约，禁止连接服务器。"""

    source_id = "tdx_exhq"
    research_metadata = {"research_only": True, "missing_fields": ["turnover", "official_settlement"]}


def test_research_publish_preserves_missing_turnover_and_provenance(
    tmp_path, source_records, sample_instrument, sample_catalog, sample_calendar, timing_rows
):
    source = ResearchSource()
    records = [dict(source_records[0], turnover=None, settlement_price=None, price="101.0")]
    downloader = FuturesDataDownloader(
        source, tmp_path, resolver=sample_catalog, calendar=sample_calendar, timings=timing_rows
    )
    with patch.object(source, "fetch_daily_bars", return_value=records):
        with pytest.raises(DataValidationError):
            downloader.download_bars(sample_instrument)
        assert not downloader.storage.manifest_path.exists()
        downloader.require_turnover = False
        path, quality, bars = downloader.download_bars(sample_instrument)
    assert path.is_file() and quality.valid_count == 1 and not quality.has_critical_errors
    assert bars[0].meta.quality_flags & QualityFlag.TURNOVER_UNAVAILABLE
    assert downloader.storage.read_bars(sample_instrument, "1d") == bars
    assert downloader.storage.read_settlements(sample_instrument) == []
    entry = next(iter(downloader.storage.capture_snapshot().datasets.values()))
    assert entry["provenance"]["source_metadata"]["research_only"] is True
    assert "TURNOVER_UNAVAILABLE" in entry["provenance"]["quality_warnings"]
    raw_path = tmp_path / entry["provenance"]["raw"]["path"]
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    assert payload["records"][0]["turnover"] is None
    assert payload["records"][0]["settlement_price"] is None


@pytest.mark.parametrize("turnover", [-1, "NaN", "", True])
def test_research_mode_only_exempts_absent_turnover(source_records, turnover):
    with pytest.raises(DataValidationError):
        validate_ohlc_records([dict(source_records[0], turnover=turnover)], require_turnover=False)


def test_research_conversion_still_rejects_duplicate_times(
    source_records, sample_instrument, sample_calendar, timing_rows
):
    row = dict(source_records[0], turnover=None)
    with pytest.raises(DataValidationError):
        convert_daily_records_to_bars(
            [row, row],
            sample_instrument,
            timings=timing_rows,
            calendar=sample_calendar,
            source_id="tdx_exhq",
            source_version="offline",
            require_turnover=False,
        )


def test_proxy_cannot_be_published_as_official_settlement(source_records, sample_instrument):
    with pytest.raises(ValueError, match="not an official settlement"):
        convert_daily_records_to_settlements(
            [dict(source_records[0], settlement_price="101.0")],
            sample_instrument,
            publications={},
            source_id="tdx_exhq",
            source_version="offline",
        )


def test_source_aggregation_boundaries_cannot_be_overwritten_by_timings(
    tmp_path, source_records, sample_instrument, sample_catalog, sample_calendar, timing_rows
):
    start = parse_time("2024-09-09T09:00:00+08:00")
    end = parse_time("2024-09-09T09:30:00+08:00")
    timing = replace(
        timing_rows["2024-09-09"],
        bar_start=start,
        bar_end=end,
        open_time=start,
        available_at=end,
        session_id="morning1",
    )
    row = dict(
        source_records[0],
        datetime=end,
        bar_start=start,
        bar_end=end,
        turnover=None,
        trading_day=timing.trading_day,
        session_id=timing.session_id,
    )
    source = ResearchSource()
    downloader = FuturesDataDownloader(
        source,
        tmp_path,
        resolver=sample_catalog,
        calendar=sample_calendar,
        timings={end.isoformat(): replace(timing, bar_start=parse_time("2024-09-09T09:01:00+08:00"))},
        require_turnover=False,
    )
    with patch.object(source, "fetch_minute_bars", return_value=[row]):
        with pytest.raises(ValueError, match="aggregation and source timing disagree"):
            downloader.download_bars(sample_instrument, "30m")
    assert not downloader.storage.manifest_path.exists()


@pytest.mark.parametrize("label", ["TURNOVER_UNAVAILABLE", "SYNTHETIC|TURNOVER_UNAVAILABLE"])
def test_report_prominently_discloses_tdx_quality(label):
    report = generate_markdown_report(
        {
            "inputs": {
                "data": {"source_ids": ["tdx_exhq"]},
                "execution": {"data_quality": {"flag_counts": {label: 5}}},
            }
        }
    )
    assert report.index("数据质量警告") < report.index("实验元数据")
    assert "不能用于精确核算" in report and "不能用于结算核验" in report
