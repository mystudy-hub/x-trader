"""Meaningful validation, temporal boundary and complete provenance round-trip checks."""

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

import pyarrow as pa
import pytest

from qh_trader.core.constants import MarketPhase, SeriesKind
from qh_trader.core.objects import Permissions, SeriesId, Session
from qh_trader.data.calendar import TradingCalendar
from qh_trader.data.schemas import (
    BarTiming,
    DataValidationError,
    SettlementPublication,
    arrow_table_to_bars,
    bars_to_arrow_table,
    convert_daily_records_to_bars,
    convert_daily_records_to_settlements,
    convert_minute_records_to_bars,
    parse_time,
    validate_ohlc_records,
)


def test_validate_ohlc_clean_records(source_records, sample_instrument):
    report = validate_ohlc_records(source_records, instrument=sample_instrument)
    assert report.is_clean and report.valid_count == len(source_records)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("volume", None),
        ("volume", 1.9),
        ("volume", True),
        ("volume", -1),
        ("open_interest", None),
        ("open_interest", "2.2"),
        ("turnover", None),
        ("turnover", "NaN"),
        ("turnover", -1),
        ("open", 0),
        ("close", "Infinity"),
        ("date", "not-a-date"),
    ],
)
def test_missing_or_invalid_data_is_not_clean(source_records, sample_instrument, field, bad):
    record = dict(source_records[0], **{field: bad})
    report = validate_ohlc_records([record], strict=False, instrument=sample_instrument)
    assert not report.is_clean and report.valid_count == 0
    with pytest.raises(DataValidationError):
        validate_ohlc_records([record], instrument=sample_instrument)


def test_ohlc_and_duplicate_source_times_are_rejected(source_records):
    with pytest.raises(DataValidationError):
        validate_ohlc_records([dict(source_records[0], low="101", high="99")])
    with pytest.raises(DataValidationError):
        validate_ohlc_records([source_records[0], source_records[0]])


def test_derived_series_price_domain_is_explicit(source_records):
    row = dict(source_records[0], open="-1", high="0", low="-2", close="-1")
    assert validate_ohlc_records([row], instrument=SeriesId("spread-fixture", SeriesKind.SPREAD)).is_clean
    with pytest.raises(DataValidationError):
        validate_ohlc_records([row])


def test_aware_time_is_not_relabelled_and_naive_time_needs_a_source_timezone():
    assert parse_time("2024-10-11T02:00:00+00:00") == datetime(2024, 10, 11, 2, tzinfo=timezone.utc)
    assert parse_time("2024-10-11 10:00:00", "Asia/Shanghai") == datetime(2024, 10, 11, 2, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="source timezone"):
        parse_time("2024-10-11 10:00:00")


def test_friday_bar_uses_explicit_monday_trading_day(sample_instrument, source_records):
    day = date(2024, 9, 9)
    opening = parse_time("2024-09-06T21:00:00+08:00")
    end = parse_time("2024-09-06T22:00:00+08:00")
    known = datetime(2024, 1, 1, tzinfo=timezone.utc)
    night = Session(
        instrument=sample_instrument,
        session_id="night",
        trading_day=day,
        start=opening,
        end=end,
        phase=MarketPhase.CONTINUOUS,
        permissions=Permissions(True, True, True),
        rule_version="night-test",
        source_id="synthetic",
        available_at=known,
    )
    calendar = TradingCalendar(
        [night],
        trading_days=[day],
        coverage_start=date(2024, 9, 6),
        coverage_end=day,
        version="night-test",
        source_id="synthetic",
        available_at=known,
    )
    timing = BarTiming(
        trading_day=day,
        bar_start=opening,
        bar_end=end,
        open_time=opening,
        available_at=end,
        session_id="night",
        includes_auction=False,
        evidence_ref="synthetic-night",
    )
    record = dict(source_records[0], datetime="2024-09-06 22:00:00")
    bars = convert_minute_records_to_bars(
        [record],
        sample_instrument,
        timings={end.isoformat(): timing},
        calendar=calendar,
        source_id="synthetic",
        source_version="v1",
        source_timezone="Asia/Shanghai",
    )
    assert bars[0].meta.trading_day == day
    with pytest.raises(ValueError, match="timing is missing"):
        convert_minute_records_to_bars(
            [record],
            sample_instrument,
            timings={},
            calendar=calendar,
            source_id="synthetic",
            source_version="v1",
            source_timezone="Asia/Shanghai",
        )


def test_source_trading_minutes_can_span_breaks_without_overlapping_bars(
    source_records, sample_instrument, sample_calendar
):
    intervals = [
        ("10:00", "11:15", "morning1"),
        ("11:15", "14:15", "morning2"),
        ("14:15", "15:00", "afternoon"),
    ]
    records, timings = [], {}
    for opening, ending, session in intervals:
        start = parse_time(f"2024-09-09T{opening}:00+08:00")
        end = parse_time(f"2024-09-09T{ending}:00+08:00")
        records.append(dict(source_records[0], datetime=end.isoformat()))
        timings[end.isoformat()] = BarTiming(
            trading_day=date(2024, 9, 9),
            bar_start=start,
            bar_end=end,
            open_time=start,
            available_at=end,
            session_id=session,
            includes_auction=False,
            evidence_ref="synthetic-trading-minute-bounds",
        )
    kwargs = dict(timings=timings, calendar=sample_calendar, source_id="synthetic", source_version="v1")
    bars = convert_minute_records_to_bars(records, sample_instrument, **kwargs)
    assert bars[1].bar_start == bars[0].bar_end
    assert bars[2].bar_start == bars[1].bar_end
    assert (bars[1].bar_end - bars[1].bar_start).total_seconds() == 3 * 3600
    key = records[-1]["datetime"]
    timings[key] = replace(timings[key], bar_start=parse_time("2024-09-09T14:00:00+08:00"))
    with pytest.raises(ValueError, match="overlap"):
        convert_minute_records_to_bars(records, sample_instrument, **kwargs)


def test_daily_conversion_requires_explicit_source_boundaries(
    source_records, timing_rows, sample_calendar, sample_instrument
):
    with pytest.raises(ValueError, match="timing is missing"):
        convert_daily_records_to_bars(
            source_records,
            sample_instrument,
            timings={},
            calendar=sample_calendar,
            source_id="synthetic",
            source_version="v1",
        )
    bad = dict(timing_rows)
    bad[source_records[0]["date"]] = replace(bad[source_records[0]["date"]], session_id="invented-session")
    with pytest.raises(ValueError, match="registered"):
        convert_daily_records_to_bars(
            source_records,
            sample_instrument,
            timings=bad,
            calendar=sample_calendar,
            source_id="synthetic",
            source_version="v1",
        )


def test_arrow_roundtrip_preserves_every_core_field(sample_bars):
    first = sample_bars[0]
    meta = replace(first.meta, receive_time=first.bar_end, source_seq=777, ingest_seq=999, schema_version=7)
    record = replace(first, meta=meta)
    restored = arrow_table_to_bars(bars_to_arrow_table([record]))
    assert restored == [record]
    assert restored[0].meta.ingested_at.year == 2026
    assert restored[0].meta.ingested_at != restored[0].meta.available_at
    series = replace(record, instrument=SeriesId("adjusted-fixture", SeriesKind.ADJUSTED))
    assert arrow_table_to_bars(bars_to_arrow_table([series])) == [series]


def test_legacy_schema_is_rejected_instead_of_inventing_metadata():
    with pytest.raises(ValueError, match="legacy"):
        arrow_table_to_bars(pa.table({"symbol": ["rb2410"]}))


def test_settlement_publication_and_previous_price_are_not_guessed(source_records, sample_instrument):
    records = [dict(source_records[0], settlement_price="100"), dict(source_records[2], settlement_price="120")]
    publications = {
        row["date"]: SettlementPublication(
            published_at=parse_time(row["date"] + "T17:10:00+08:00"),
            available_at=parse_time(row["date"] + "T17:12:00+08:00"),
            is_final=False,
            evidence_ref="synthetic-publication",
        )
        for row in records
    }
    with pytest.raises(ValueError, match="publication"):
        convert_daily_records_to_settlements(
            records, sample_instrument, publications={}, source_id="synthetic", source_version="v1"
        )
    settlements = convert_daily_records_to_settlements(
        records, sample_instrument, publications=publications, source_id="synthetic", source_version="v1"
    )
    assert [row.pre_settlement_price for row in settlements] == [None, None]
    assert not settlements[0].is_final
    assert settlements[0].settlement_price == Decimal(100)
    assert settlements[0].published_at == publications[records[0]["date"]].published_at
