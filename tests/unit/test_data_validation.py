"""Independent data-quality and calendar-gap scenarios; no dependence on local downloaded samples."""

import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from qh_trader.core.constants import MarketPhase, Offset, QualityFlag
from qh_trader.core.objects import CommissionRule, MarginRule, Permissions, Session, Settlement, VersionedValue
from qh_trader.data.calendar import TradingCalendar
from qh_trader.data.execution_reference import derive_execution_references
from qh_trader.data.gaps import GapEvidence, scan_gaps
from qh_trader.data.schemas import parse_time
from qh_trader.data.storage import ParquetDataStorage
from qh_trader.data.validation import validate_dataset, validate_raw_archive
from qh_trader.infrastructure.rule_store import RuleStore

FIRST = date(2024, 9, 9)
LAST = date(2024, 9, 13)
D = Decimal


def published(tmp_path, instrument, bars, timings, *, references=True, settlements=()):
    storage = ParquetDataStorage(tmp_path)
    refs = derive_execution_references(bars, {item.bar_start: item for item in timings.values()}) if references else ()
    storage.publish_batch(instrument, bars[0].interval, bars=bars, execution_references=refs, settlements=settlements)
    return storage


def test_gaps_ignore_known_breaks_and_weekends(sample_bars, sample_instrument, sample_calendar):
    result = scan_gaps(sample_bars, sample_instrument, sample_calendar, start_day=FIRST, end_day=date(2024, 9, 15))
    assert result.passed and not result.gaps
    assert result.active_sessions == 15
    assert result.closed_sessions == 10 and result.closed_days == 2


def test_missing_day_is_only_missing_during_registered_active_sessions(sample_bars, sample_instrument, sample_calendar):
    result = scan_gaps(
        sample_bars[:1] + sample_bars[2:], sample_instrument, sample_calendar, start_day=FIRST, end_day=LAST
    )
    assert not result.passed
    assert len(result.gaps) == 3
    assert {gap.session_id for gap in result.gaps} == {"morning1", "morning2", "afternoon"}
    assert all(gap.trading_day == date(2024, 9, 10) for gap in result.gaps)


def test_no_trade_and_disconnect_need_explicit_evidence(sample_bars, sample_instrument, sample_calendar):
    bars = sample_bars[:1] + sample_bars[2:]
    no_trade = GapEvidence(
        sample_instrument,
        date(2024, 9, 10),
        parse_time("2024-09-10T09:00:00+08:00"),
        parse_time("2024-09-10T15:00:00+08:00"),
        "no_trades",
        "synthetic-source",
        "verified-synthetic-no-trades",
    )
    confirmed = scan_gaps(bars, sample_instrument, sample_calendar, start_day=FIRST, end_day=LAST, evidence=[no_trade])
    assert confirmed.passed and all(gap.kind == "no_trades" for gap in confirmed.gaps)
    disconnect = replace(
        no_trade,
        start=parse_time("2024-09-10T10:30:00+08:00"),
        end=parse_time("2024-09-10T11:00:00+08:00"),
        kind="disconnected",
        evidence_ref="connection-record",
    )
    partial = scan_gaps(bars, sample_instrument, sample_calendar, start_day=FIRST, end_day=LAST, evidence=[disconnect])
    assert not partial.passed
    assert {gap.kind for gap in partial.gaps} == {"missing", "disconnected"}
    conflict = scan_gaps(
        bars, sample_instrument, sample_calendar, start_day=FIRST, end_day=LAST, evidence=[no_trade, disconnect]
    )
    assert not conflict.passed and any(gap.kind == "conflicting_evidence" for gap in conflict.gaps)


def test_zero_volume_is_an_observation_not_a_missing_bar(sample_bars, sample_instrument, sample_calendar):
    bars = [replace(sample_bars[0], volume=0), *sample_bars[1:]]
    result = scan_gaps(bars, sample_instrument, sample_calendar, start_day=FIRST, end_day=LAST)
    assert result.passed and not result.gaps
    assert len(result.zero_volume_observations) == 1


def test_unknown_phase_cannot_be_treated_as_normal_closure(sample_calendar, sample_instrument):
    original = sample_calendar.sessions_for_day(sample_instrument, FIRST)[0]
    unknown = replace(original, phase=MarketPhase.UNKNOWN, permissions=Permissions(False, False, False))
    calendar = TradingCalendar(
        [unknown],
        trading_days=[FIRST],
        coverage_start=FIRST,
        coverage_end=FIRST,
        version=unknown.rule_version,
        source_id=unknown.source_id,
        available_at=unknown.available_at,
    )
    result = scan_gaps([], sample_instrument, calendar, start_day=FIRST, end_day=FIRST)
    assert not result.passed and result.issues[0].code == "unknown_phase"


def test_friday_night_does_not_create_a_weekend_gap(sample_bars, sample_instrument, sample_calendar):
    source = sample_calendar.sessions_for_day(sample_instrument, FIRST)
    night = Session(
        instrument=sample_instrument,
        session_id="night",
        trading_day=FIRST,
        start=parse_time("2024-09-06T21:00:00+08:00"),
        end=parse_time("2024-09-06T23:00:00+08:00"),
        phase=MarketPhase.CONTINUOUS,
        permissions=Permissions(True, True, True),
        rule_version=source[0].rule_version,
        source_id=source[0].source_id,
        available_at=source[0].available_at,
    )
    calendar = TradingCalendar(
        [night, *source],
        trading_days=[FIRST],
        coverage_start=date(2024, 9, 6),
        coverage_end=FIRST,
        version=night.rule_version,
        source_id=night.source_id,
        available_at=night.available_at,
    )
    bar = replace(
        sample_bars[0],
        bar_start=night.start,
        open_time=night.start,
        meta=replace(sample_bars[0].meta, session_id="night"),
    )
    result = scan_gaps([bar], sample_instrument, calendar, start_day=FIRST, end_day=FIRST)
    assert result.passed and result.active_sessions == 4 and not result.gaps


def test_research_validation_does_not_claim_exact_readiness(
    tmp_path,
    sample_instrument,
    sample_calendar,
    sample_catalog,
    sample_bars,
    timing_rows,
):
    storage = published(tmp_path, sample_instrument, sample_bars, timing_rows)
    report = validate_dataset(
        storage,
        sample_instrument,
        "1d",
        calendar=sample_calendar,
        catalog=sample_catalog,
        timings=timing_rows,
        mode="research",
        start_day=FIRST,
        end_day=LAST,
    )
    assert report.passed
    assert not report.as_dict()["ready_for_exact"]
    assert {"rules_missing", "limits_missing", "final_settlement_missing", "synthetic_or_assumed"} <= {
        issue.code for issue in report.issues
    }
    exact = validate_dataset(
        storage,
        sample_instrument,
        "1d",
        calendar=sample_calendar,
        catalog=sample_catalog,
        timings=timing_rows,
        mode="exact",
        start_day=FIRST,
        end_day=LAST,
    )
    assert not exact.passed and exact.as_dict()["quarantine"]


def test_complete_explicit_inputs_can_pass_exact_checks(
    tmp_path,
    sample_instrument,
    sample_calendar,
    sample_catalog,
    sample_bars,
    timing_rows,
):
    bars = [replace(bar, meta=replace(bar.meta, quality_flags=QualityFlag.OK)) for bar in sample_bars]
    timings = {key: replace(value, time_assumption=None) for key, value in timing_rows.items()}
    settlements = []
    for bar in bars:
        publication = bar.bar_end + timedelta(hours=2)
        meta = replace(bar.meta, event_time=publication, available_at=publication)
        settlements.append(
            Settlement(
                instrument=sample_instrument,
                meta=meta,
                settlement_price=bar.close,
                pre_settlement_price=None,
                published_at=publication,
                is_final=True,
            )
        )
    storage = published(tmp_path, sample_instrument, bars, timings, settlements=settlements)
    epoch = datetime(2024, 1, 1, tzinfo=timezone.utc)
    limits = {
        bar.meta.trading_day: VersionedValue(
            value=(D(50), D(150)), source_id="synthetic-bounds", version="v1", effective_from=epoch, available_at=epoch
        )
        for bar in bars
    }
    with RuleStore() as rules:
        rules.register_contract_rule(
            sample_instrument,
            "fixture",
            sample_catalog.get_spec(str(sample_instrument)),
            "synthetic",
            "contract-v1",
            epoch,
            epoch,
        )
        rules.register_margin_rule(
            sample_instrument, "fixture", MarginRule(D("0.1"), D(0)), "synthetic", "margin-v1", epoch, epoch
        )
        for offset in Offset:
            rules.register_commission_rule(
                sample_instrument,
                "fixture",
                offset,
                CommissionRule(D(1), D(0), D("0.01"), "ROUND_HALF_UP"),
                "synthetic",
                f"fee-{offset.value}",
                epoch,
                epoch,
            )
        report = validate_dataset(
            storage,
            sample_instrument,
            "1d",
            calendar=sample_calendar,
            catalog=sample_catalog,
            timings=timings,
            rules=rules,
            profile="fixture",
            limits=limits,
            start_day=FIRST,
            end_day=LAST,
        )
    assert report.passed, report.issues
    assert report.as_dict()["ready_for_exact"]
    assert len(report.summary["rule_versions_used"]) == 5


def test_missing_execution_is_an_error_even_in_research_mode(
    tmp_path,
    sample_instrument,
    sample_calendar,
    sample_catalog,
    sample_bars,
    timing_rows,
):
    storage = published(tmp_path, sample_instrument, sample_bars, timing_rows, references=False)
    report = validate_dataset(
        storage,
        sample_instrument,
        "1d",
        calendar=sample_calendar,
        catalog=sample_catalog,
        timings=timing_rows,
        mode="research",
        start_day=FIRST,
        end_day=LAST,
    )
    assert not report.passed
    assert any(issue.code == "execution_price_missing_or_ambiguous" for issue in report.issues)


def test_bar_spanning_too_much_trading_time_cannot_be_labelled_hourly(
    tmp_path,
    sample_instrument,
    sample_calendar,
    sample_catalog,
    sample_bars,
    timing_rows,
):
    bars = [replace(bar, interval="1h") for bar in sample_bars]
    storage = published(tmp_path, sample_instrument, bars, timing_rows)
    report = validate_dataset(
        storage,
        sample_instrument,
        "1h",
        calendar=sample_calendar,
        catalog=sample_catalog,
        timings=timing_rows,
        mode="research",
        start_day=FIRST,
        end_day=LAST,
    )
    assert not report.passed and any(issue.code == "interval_duration" for issue in report.issues)


def test_validator_does_not_modify_corrupt_or_legacy_inputs(
    tmp_path,
    sample_instrument,
    sample_calendar,
    sample_catalog,
    sample_bars,
    timing_rows,
):
    storage = published(tmp_path, sample_instrument, sample_bars, timing_rows)
    path = storage.get_bar_file_path(sample_instrument, "1d")
    path.write_bytes(b"corrupt")
    report = validate_dataset(
        storage,
        sample_instrument,
        "1d",
        calendar=sample_calendar,
        catalog=sample_catalog,
        timings=timing_rows,
        mode="research",
        start_day=FIRST,
        end_day=LAST,
    )
    assert not report.passed and report.issues[0].code == "snapshot_invalid"
    assert path.read_bytes() == b"corrupt"


def test_raw_quality_report_binds_hash_and_never_fills_missing_turnover(tmp_path, source_records):
    document = {
        "schema_version": 1,
        "status": "raw_observation",
        "instrument": "SHFE.rb2410",
        "interval": "1d",
        "source_id": "synthetic",
        "records": [dict(source_records[0], turnover=None)],
        "captures": [],
    }
    payload = json.dumps(document).encode("utf-8")
    path = tmp_path / (hashlib.sha256(payload).hexdigest() + ".json")
    path.write_bytes(payload)
    result = validate_raw_archive(path)
    assert not result.passed
    assert any(issue.code == "INVALID_TURNOVER" for issue in result.issues)
    assert result.as_dict()["quarantine"][0]["record_indices"] == [0]
    assert path.read_bytes() == payload


def test_readonly_rules_do_not_create_or_mutate_databases(tmp_path, sample_instrument):
    missing = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        RuleStore(missing, readonly=True)
    assert not missing.exists()
    path = tmp_path / "rules.db"
    at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    with RuleStore(path) as writer:
        writer.migrate()
        writer.register_margin_rule(sample_instrument, "fixture", MarginRule(D("0.1"), D(0)), "test", "v1", at, at)
    with RuleStore(path, readonly=True) as reader:
        with reader.read_snapshot():
            assert reader.margin_rule(sample_instrument, "fixture", at, at).value.ratio == D("0.1")
            with pytest.raises(RuntimeError, match="read-only"):
                reader.register_margin_rule(
                    sample_instrument, "fixture", MarginRule(D("0.2"), D(0)), "test", "v2", at, at
                )
        with pytest.raises(RuntimeError, match="read-only"):
            reader.migrate()
