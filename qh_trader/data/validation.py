"""Read-only dataset validation, input fingerprints and explicit quarantine manifests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from functools import partial
from pathlib import Path
from typing import Any

from qh_trader.core.clock import utc_timestamp
from qh_trader.core.constants import MarketPhase, Offset, PriceType, QualityFlag
from qh_trader.core.objects import InstrumentId, VersionedValue, require_decimal, require_text
from qh_trader.core.ports import RuleStorePort
from qh_trader.data.calendar import TradingCalendar
from qh_trader.data.contracts import ContractResolver
from qh_trader.data.gaps import DataIssue, GapEvidence, scan_gaps
from qh_trader.data.schemas import BarTiming, validate_ohlc_records
from qh_trader.data.storage import ParquetDataStorage, normalize_interval


def json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {member.name: getattr(value, member.name) for member in fields(value)}
    raise TypeError("quality reports contain only normalized public metadata")


def report_json(value: Any) -> str:
    return json.dumps(
        value, default=json_default, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_reference(path: Path | str, root: Path) -> dict[str, str]:
    actual = Path(path).resolve()
    return {
        "path": actual.relative_to(root.resolve()).as_posix() if actual.is_relative_to(root.resolve()) else actual.name,
        "sha256": file_hash(actual),
    }


@dataclass(frozen=True, slots=True)
class ExecutionCheck:
    reference_time: datetime
    session_id: str
    price_type: PriceType
    known_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "reference_time", utc_timestamp(self.reference_time))
        object.__setattr__(self, "known_at", utc_timestamp(self.known_at))
        require_text(self.session_id, "session_id")
        if not isinstance(self.price_type, PriceType):
            raise TypeError("execution check requires an explicit price type")


@dataclass(frozen=True, slots=True)
class ValidationReport:
    scope: str
    mode: str
    input_refs: Mapping[str, Any]
    summary: Mapping[str, Any]
    issues: tuple[DataIssue, ...]

    @property
    def passed(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def as_dict(self) -> dict[str, Any]:
        errors = [issue for issue in self.issues if issue.severity == "error"]
        return {
            "schema_version": 1,
            "scope": self.scope,
            "mode": self.mode,
            "passed": self.passed,
            "ready_for_exact": self.passed and self.mode == "exact" and self.scope == "canonical_dataset",
            "input_refs": dict(self.input_refs),
            "summary": dict(self.summary),
            "issues": [asdict(issue) for issue in self.issues],
            "quarantine": [
                {
                    "status": "blocked_for_use",
                    "input_refs": dict(self.input_refs),
                    "codes": sorted({issue.code for issue in errors}),
                    "record_indices": sorted(
                        {issue.record_index for issue in errors if issue.record_index is not None}
                    ),
                }
            ]
            if errors
            else [],
        }


def validate_raw_archive(path: Path | str, *, expected_sha256: str | None = None) -> ValidationReport:
    path = Path(path).resolve()
    issues: list[DataIssue] = []
    references: dict[str, Any] = {}
    summary: dict[str, Any] = {"enabled_datasets": ["bar"], "canonical_publication_checked": False}
    try:
        digest = file_hash(path)
        references["raw"] = {"path": path.name, "sha256": digest}
        expected = expected_sha256 or (path.stem if len(path.stem) == 64 else None)
        if expected is None or digest != expected.lower():
            issues.append(DataIssue("raw_hash_unbound", "raw archive is not bound to the expected content hash"))
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("schema_version") != 1 or document.get("status") != "raw_observation":
            raise ValueError("not a successful raw observation archive")
        symbol = document["instrument"]
        from qh_trader.core.constants import Exchange

        exchange, code = symbol.split(".", 1)
        instrument = InstrumentId(Exchange(exchange), code)
        interval = normalize_interval(document["interval"])
        records = document["records"]
        quality = validate_ohlc_records(
            records,
            "date" if interval == "1d" else "datetime",
            strict=False,
            instrument=instrument,
            source_timezone=document.get("source_timezone"),
        )
        if not records:
            issues.append(DataIssue("empty_source", "raw archive contains no observations"))
        for issue in quality.issues:
            issues.append(DataIssue(issue.issue_type, issue.message, record_index=issue.index))
        for capture in document.get("captures", ()):
            body = capture["body"].encode(capture.get("encoding", "utf-8"))
            if hashlib.sha256(body).hexdigest() != capture["sha256"]:
                issues.append(
                    DataIssue("response_hash_mismatch", "archived provider response has a different checksum")
                )
        summary.update(
            instrument=str(instrument),
            interval=interval,
            record_count=len(records),
            valid_count=quality.valid_count,
            source_id=document["source_id"],
        )
        issues.append(
            DataIssue("raw_scope_only", "raw checks do not validate sessions, rules or execution coverage", "warning")
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        issues.append(DataIssue("raw_archive_invalid", "raw archive structure, source identity or parsing failed"))
    return ValidationReport("raw_observation", "raw", references, summary, tuple(issues))


def validate_dataset(
    storage: ParquetDataStorage,
    instrument: InstrumentId,
    interval: str,
    *,
    calendar: TradingCalendar,
    catalog: ContractResolver,
    timings: Mapping[str, BarTiming],
    mode: str = "exact",
    snapshot_id: str | None = None,
    start_day: date | None = None,
    end_day: date | None = None,
    rules: RuleStorePort | None = None,
    profile: str | None = None,
    limits: Mapping[date, VersionedValue[tuple[Decimal, Decimal]]] | None = None,
    execution_checks: Sequence[ExecutionCheck] | None = None,
    gap_evidence: Sequence[GapEvidence] = (),
    input_refs: Mapping[str, Any] | None = None,
) -> ValidationReport:
    if mode not in {"exact", "research"}:
        raise ValueError("validation mode must be exact or research")
    interval = normalize_interval(interval)
    issues: list[DataIssue] = []
    inputs = dict(input_refs or {})
    summary: dict[str, Any] = {
        "instrument": str(instrument),
        "interval": interval,
        "enabled_datasets": ["bar", "settlement", "execution_reference"],
        "tick_checks": "not_enabled",
        "rule_checks": "execution_and_bar_boundary_times",
    }
    severity = "error" if mode == "exact" else "warning"
    try:
        snapshot = storage.capture_snapshot(snapshot_id)
        summary["snapshot_id"] = snapshot.snapshot_id
        if snapshot.snapshot_id is None:
            issues.append(
                DataIssue("no_committed_snapshot", "no canonical snapshot is committed; legacy files are not validated")
            )
            return ValidationReport("canonical_dataset", mode, inputs, summary, tuple(issues))
        inputs["manifest"] = {"snapshot_id": snapshot.snapshot_id}
        for key, entry in snapshot.datasets.items():
            if entry.get("instrument") == str(instrument):
                inputs[key] = {"path": entry["relative_path"], "sha256": entry["sha256"]}
        bars = storage.read_bars(instrument, interval, snapshot=snapshot)
        settlements = storage.read_settlements(instrument, snapshot=snapshot)
        references = storage.read_execution_references(instrument, interval, snapshot=snapshot)
    except (OSError, ValueError, TypeError, KeyError):
        issues.append(DataIssue("snapshot_invalid", "manifest, file hashes or stored data contracts failed validation"))
        return ValidationReport("canonical_dataset", mode, inputs, summary, tuple(issues))
    if not bars:
        issues.append(DataIssue("bars_missing", "selected committed dataset contains no bars"))
        return ValidationReport("canonical_dataset", mode, inputs, summary, tuple(issues))
    if (start_day is None) != (end_day is None):
        raise ValueError("both coverage dates must be supplied")
    declared_range = start_day is not None
    start_day = start_day if start_day is not None else min(bar.meta.trading_day for bar in bars)
    end_day = end_day if end_day is not None else max(bar.meta.trading_day for bar in bars)
    bars = [bar for bar in bars if start_day <= bar.meta.trading_day <= end_day]
    summary.update(
        start_day=start_day,
        end_day=end_day,
        range_basis="declared" if declared_range else "observed",
        record_count=len(bars),
        calendar_version=calendar.version,
        catalog_version=catalog.catalog_version,
    )
    if not bars:
        issues.append(DataIssue("bars_missing", "requested date range contains no bars"))
        return ValidationReport("canonical_dataset", mode, inputs, summary, tuple(issues))
    if not declared_range:
        issues.append(
            DataIssue("observed_range_only", "coverage outside the observed date range was not requested", "warning")
        )
    by_start: dict[datetime, BarTiming] = {}
    for declared_timing in timings.values():
        if start_day <= declared_timing.trading_day <= end_day:
            if declared_timing.bar_start in by_start:
                issues.append(DataIssue("timing_conflict", "multiple source timings describe one bar start"))
            by_start[declared_timing.bar_start] = declared_timing
    actual_starts = {bar.bar_start for bar in bars}
    for expected in by_start.values():
        if expected.bar_start not in actual_starts:
            issues.append(
                DataIssue("expected_bar_missing", "a declared source bar is absent", trading_day=expected.trading_day)
            )
    for index, bar in enumerate(bars):
        timing = by_start.get(bar.bar_start)
        if timing is None:
            issues.append(DataIssue("timing_missing", "bar has no explicit source timing evidence", record_index=index))
        else:
            expected_fields = (
                timing.bar_end,
                timing.open_time,
                timing.available_at,
                timing.session_id,
                timing.trading_day,
                timing.includes_auction,
            )
            actual = (
                bar.bar_end,
                bar.open_time,
                bar.meta.available_at,
                bar.meta.session_id,
                bar.meta.trading_day,
                bar.includes_auction,
            )
            if actual != expected_fields:
                issues.append(
                    DataIssue(
                        "timing_mismatch", "stored bar differs from the declared source timing", record_index=index
                    )
                )
        if bar.meta.quality_flags & QualityFlag.SYNTHETIC or (
            timing is not None and timing.time_assumption is not None
        ):
            issues.append(
                DataIssue(
                    "synthetic_or_assumed", "bar includes synthetic data or declared time assumptions", severity, index
                )
            )
        try:
            spec = catalog.get_spec(str(instrument), as_of=bar.meta.trading_day)
            for name in ("open", "high", "low", "close"):
                price = getattr(bar, name)
                if price <= 0 or price % spec.price_tick != 0:
                    issues.append(
                        DataIssue(
                            "price_tick_or_domain",
                            "actual-contract price violates its catalog tick/domain",
                            record_index=index,
                        )
                    )
                    break
            sessions = calendar.sessions_for_day(instrument, bar.meta.trading_day)
            opening = [
                session
                for session in sessions
                if session.session_id == bar.meta.session_id and session.contains(bar.open_time)
            ]
            if len(opening) != 1 or not opening[0].permissions.match:
                issues.append(
                    DataIssue("opening_session", "bar opening is outside a unique matching session", record_index=index)
                )
            if interval != "1d":
                maximum = 3600 if interval == "1h" else int(interval.removesuffix("m")) * 60
                trading_seconds = sum(
                    (min(bar.bar_end, session.end) - max(bar.bar_start, session.start)).total_seconds()
                    for session in sessions
                    if session.phase == MarketPhase.CONTINUOUS
                    and session.permissions.match
                    and session.start < bar.bar_end
                    and session.end > bar.bar_start
                )
                if trading_seconds > maximum:
                    issues.append(
                        DataIssue(
                            "interval_duration",
                            "bar covers more trading time than its declared interval",
                            record_index=index,
                        )
                    )
        except (ValueError, LookupError, TypeError):
            issues.append(
                DataIssue(
                    "catalog_or_calendar",
                    "contract lifetime or registered sessions are unavailable",
                    record_index=index,
                )
            )
        bound = (limits or {}).get(bar.meta.trading_day)
        if bound is None:
            issues.append(
                DataIssue("limits_missing", "actual trading-day price boundaries were not supplied", severity, index)
            )
        else:
            try:
                lower, upper = bound.value
                require_decimal(lower, "lower_limit")
                require_decimal(upper, "upper_limit")
                if (
                    lower > upper
                    or not bound.effective_at(bar.open_time)
                    or not bound.effective_at(bar.bar_end - timedelta(microseconds=1))
                ):
                    raise ValueError("limit interval")
                if not bound.visible_at(bar.open_time):
                    issues.append(
                        DataIssue(
                            "limits_not_visible", "price boundaries were not visible at the opening", severity, index
                        )
                    )
                if bar.low < lower or bar.high > upper:
                    issues.append(
                        DataIssue("price_outside_limits", "OHLC exceeds actual price boundaries", record_index=index)
                    )
            except (ValueError, TypeError):
                issues.append(
                    DataIssue(
                        "invalid_limits", "price boundary values or effective interval are invalid", record_index=index
                    )
                )
    coverage = scan_gaps(bars, instrument, calendar, start_day=start_day, end_day=end_day, evidence=gap_evidence)
    summary["coverage"] = coverage.as_dict()
    issues.extend(coverage.issues)
    if not coverage.passed:
        issues.append(
            DataIssue("coverage_gaps", "active-session coverage contains missing/disconnected or unknown intervals")
        )
    checks = (
        list(execution_checks)
        if execution_checks is not None
        else [
            ExecutionCheck(bar.open_time, bar.meta.session_id, PriceType.BAR_OPEN, bar.open_time)
            for bar in bars
            if bar.meta.session_id is not None
        ]
    )
    if not checks:
        issues.append(DataIssue("execution_requirements_missing", "selected execution times were not specified"))
    for check in checks:
        matches = [
            row
            for row in references
            if row.reference_time == check.reference_time
            and row.session_id == check.session_id
            and row.price_type == check.price_type
            and row.meta.available_at <= check.known_at
        ]
        if len(matches) != 1:
            issues.append(
                DataIssue(
                    "execution_price_missing_or_ambiguous",
                    "selected session/time/type has no unique visible execution price",
                )
            )
        else:
            quote = matches[0]
            try:
                spec = catalog.get_spec(str(instrument), as_of=quote.meta.trading_day)
                if quote.price <= 0 or quote.price % spec.price_tick != 0:
                    raise ValueError("execution price tick/domain")
                bound = (limits or {}).get(quote.meta.trading_day)
                if bound is not None and not bound.value[0] <= quote.price <= bound.value[1]:
                    raise ValueError("execution price limit")
            except (ValueError, LookupError, TypeError):
                issues.append(
                    DataIssue("execution_price_invalid", "execution observation violates catalog or price boundaries")
                )
    summary["execution_checks"] = len(checks)
    summary["unknown_execution_volume"] = sum(row.available_volume is None for row in references)
    expected_days = {bar.meta.trading_day for bar in bars}
    settlement_days = {row.meta.trading_day for row in settlements if row.is_final}
    for day in sorted(expected_days - settlement_days):
        issues.append(
            DataIssue(
                "final_settlement_missing",
                "final settlement is absent for a used trading day",
                severity,
                trading_day=day,
            )
        )
    for row in settlements:
        if row.meta.trading_day in expected_days and row.settlement_price <= 0:
            issues.append(
                DataIssue(
                    "settlement_price_invalid",
                    "actual-contract settlement must be positive",
                    trading_day=row.meta.trading_day,
                )
            )
    versions = set()
    if rules is None or not profile:
        issues.append(
            DataIssue("rules_missing", "an explicit account profile and rule store were not supplied", severity)
        )
    else:
        points = {bar.open_time for bar in bars} | {bar.bar_end - timedelta(microseconds=1) for bar in bars}
        points.update(check.reference_time for check in checks)
        for point in sorted(points):
            queries: list[tuple[str, Callable[[], VersionedValue[Any]]]] = [
                ("contract", partial(rules.contract_rule, instrument, profile, point, point)),
                ("margin", partial(rules.margin_rule, instrument, profile, point, point)),
            ]
            queries.extend(
                (
                    f"commission:{offset.value}",
                    partial(rules.commission_rule, instrument, profile, offset, point, point),
                )
                for offset in Offset
            )
            for kind, query in queries:
                try:
                    rule = query()
                    versions.add((kind, rule.source_id, rule.version))
                except (ValueError, LookupError):
                    issues.append(DataIssue("rule_coverage", "no unique visible rule at a used execution/bar boundary"))
    summary["rule_versions_used"] = sorted(versions)
    summary["settlement_records"] = len(settlements)
    return ValidationReport("canonical_dataset", mode, inputs, summary, tuple(issues))
