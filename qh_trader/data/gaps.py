"""Calendar-based coverage analysis. Reports gaps without filling or rewriting any observations."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any

from qh_trader.core.clock import utc_timestamp
from qh_trader.core.constants import MarketPhase
from qh_trader.core.objects import Bar, InstrumentId, require_date, require_text
from qh_trader.data.calendar import TradingCalendar


@dataclass(frozen=True, slots=True)
class DataIssue:
    code: str
    message: str
    severity: str = "error"
    record_index: int | None = None
    trading_day: date | None = None


@dataclass(frozen=True, slots=True)
class GapEvidence:
    instrument: InstrumentId
    trading_day: date
    start: datetime
    end: datetime
    kind: str
    source_id: str
    evidence_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, InstrumentId):
            raise TypeError("gap evidence requires an actual instrument")
        require_date(self.trading_day, "trading_day")
        require_text(self.source_id, "evidence source")
        require_text(self.evidence_ref, "evidence reference")
        object.__setattr__(self, "start", utc_timestamp(self.start))
        object.__setattr__(self, "end", utc_timestamp(self.end))
        if self.start >= self.end or self.kind not in {"no_trades", "disconnected"}:
            raise ValueError("gap evidence must describe a nonempty interval and an explicit cause")


@dataclass(frozen=True, slots=True)
class GapSegment:
    trading_day: date
    session_id: str
    start: datetime
    end: datetime
    kind: str
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GapReport:
    instrument: str
    calendar_version: str | None
    start_day: date
    end_day: date
    active_sessions: int
    closed_sessions: int
    auction_sessions: int
    closed_days: int
    gaps: tuple[GapSegment, ...]
    zero_volume_observations: tuple[tuple[datetime, datetime], ...]
    issues: tuple[DataIssue, ...]

    @property
    def passed(self) -> bool:
        return not self.issues and all(gap.kind == "no_trades" for gap in self.gaps)

    def as_dict(self) -> dict[str, Any]:
        return {"schema_version": 1, "scope": "calendar_coverage", "passed": self.passed, **asdict(self)}


def _classify(
    day: date,
    session_id: str,
    start: datetime,
    end: datetime,
    evidence: Sequence[GapEvidence],
) -> list[GapSegment]:
    relevant = [row for row in evidence if row.start < end and row.end > start]
    boundaries = {start, end}
    for row in relevant:
        boundaries.update((max(start, row.start), min(end, row.end)))
    points = sorted(boundaries)
    result = []
    for left, right in zip(points, points[1:], strict=False):
        covering = [row for row in relevant if row.start <= left and row.end >= right]
        causes = {row.kind for row in covering}
        kind = next(iter(causes)) if len(causes) == 1 else "conflicting_evidence" if causes else "missing"
        result.append(
            GapSegment(day, session_id, left, right, kind, tuple(sorted({row.evidence_ref for row in covering})))
        )
    return result


def scan_gaps(
    bars: Sequence[Bar],
    instrument: InstrumentId,
    calendar: TradingCalendar,
    *,
    start_day: date,
    end_day: date,
    evidence: Sequence[GapEvidence] = (),
) -> GapReport:
    require_date(start_day, "start_day")
    require_date(end_day, "end_day")
    if start_day > end_day:
        raise ValueError("coverage dates are reversed")
    issues: list[DataIssue] = []
    gaps: list[GapSegment] = []
    active = closed = auctions = 0
    try:
        calendar.is_trading_day(start_day)
        calendar.is_trading_day(end_day)
    except (ValueError, LookupError):
        issues.append(DataIssue("calendar_coverage_missing", "calendar does not cover the requested dates"))
        return GapReport(str(instrument), calendar.version, start_day, end_day, 0, 0, 0, 0, (), (), tuple(issues))
    days = sorted(day for day in calendar.trading_days if start_day <= day <= end_day)
    selected = [bar for bar in bars if start_day <= bar.meta.trading_day <= end_day]
    zero_volume = tuple((bar.bar_start, bar.bar_end) for bar in selected if bar.volume == 0)
    for index, bar in enumerate(selected):
        if bar.instrument != instrument:
            issues.append(
                DataIssue("wrong_instrument", "bar does not match the selected instrument", record_index=index)
            )
        if bar.meta.trading_day not in calendar.trading_days:
            issues.append(
                DataIssue(
                    "nontrading_day",
                    "bar is assigned to a calendar-closed trading date",
                    record_index=index,
                    trading_day=bar.meta.trading_day,
                )
            )
    for day in days:
        try:
            sessions = calendar.sessions_for_day(instrument, day)
        except (ValueError, LookupError):
            issues.append(
                DataIssue(
                    "sessions_missing", "calendar has no registered sessions for this instrument/date", trading_day=day
                )
            )
            continue
        windows = [
            (bar.bar_start, bar.bar_end)
            for bar in selected
            if bar.instrument == instrument and bar.meta.trading_day == day
        ]
        causes = [row for row in evidence if row.instrument == instrument and row.trading_day == day]
        for session in sessions:
            if session.phase == MarketPhase.UNKNOWN:
                issues.append(
                    DataIssue("unknown_phase", "unknown calendar phase is not a normal closure", trading_day=day)
                )
                continue
            if session.phase in {MarketPhase.AUCTION_SUBMIT, MarketPhase.AUCTION_MATCH}:
                auctions += 1
                continue
            if session.phase != MarketPhase.CONTINUOUS or not session.permissions.match:
                closed += 1
                continue
            active += 1
            coverage = sorted(
                (max(left, session.start), min(right, session.end))
                for left, right in windows
                if left < session.end and right > session.start
            )
            cursor = session.start
            for left, right in coverage:
                if left > cursor:
                    gaps.extend(_classify(day, session.session_id, cursor, left, causes))
                cursor = max(cursor, right)
            if cursor < session.end:
                gaps.extend(_classify(day, session.session_id, cursor, session.end, causes))
    closed_days = (end_day - start_day).days + 1 - len(days)
    return GapReport(
        str(instrument),
        calendar.version,
        start_day,
        end_day,
        active,
        closed,
        auctions,
        closed_days,
        tuple(gaps),
        zero_volume,
        tuple(issues),
    )
