"""Query supplied historical sessions and trading dates; missing rules never grant permissions."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from qh_trader.core.clock import utc_timestamp
from qh_trader.core.constants import AmbiguousRuleError, Exchange, MarketPhase, MissingRuleError
from qh_trader.core.objects import InstrumentId, Permissions, Session, require_date, require_text

CHINA_TZ = ZoneInfo("Asia/Shanghai")


class TradingCalendar:
    """One immutable calendar version, including explicit holidays and announcement exceptions."""

    def __init__(
        self,
        sessions: Sequence[Session] = (),
        *,
        trading_days: Sequence[date] = (),
        coverage_start: date | None = None,
        coverage_end: date | None = None,
        version: str | None = None,
        source_id: str | None = None,
        available_at: datetime | None = None,
    ) -> None:
        self._sessions = tuple(sessions)
        self.trading_days = frozenset(trading_days)
        self.coverage_start = coverage_start
        self.coverage_end = coverage_end
        self.version = version
        self.source_id = source_id
        self.available_at = utc_timestamp(available_at) if available_at is not None else None
        if version is None and not self._sessions and not self.trading_days:
            return
        if version is None or source_id is None or coverage_start is None or coverage_end is None:
            raise ValueError("calendar version, source and coverage must be explicit")
        require_text(version, "calendar version")
        require_text(source_id, "calendar source")
        require_date(coverage_start, "coverage_start")
        require_date(coverage_end, "coverage_end")
        if coverage_start > coverage_end or self.available_at is None:
            raise ValueError("calendar requires a valid coverage interval and availability time")
        for day in self.trading_days:
            require_date(day, "trading_day")
            if not coverage_start <= day <= coverage_end:
                raise ValueError("trading date is outside calendar coverage")
        grouped: dict[InstrumentId, list[Session]] = {}
        for session in self._sessions:
            if not isinstance(session, Session):
                raise TypeError("calendar requires normalized Sessions")
            if session.trading_day not in self.trading_days:
                raise ValueError("a closed trading date cannot contain trading-day sessions")
            if session.rule_version != version or session.source_id != source_id:
                raise ValueError("session provenance must match its calendar version")
            grouped.setdefault(session.instrument, []).append(session)
        for values in grouped.values():
            ordered = sorted(values, key=lambda item: item.start)
            if any(left.end > right.start for left, right in zip(ordered, ordered[1:], strict=False)):
                raise AmbiguousRuleError("overlapping sessions grant ambiguous phase/permissions")

    @classmethod
    def from_file(cls, path: Path | str) -> TradingCalendar:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        schema_version = data.get("schema_version")
        if schema_version == 2:
            return cls.from_profile_file(data, path)
        if schema_version != 1:
            raise ValueError("unsupported calendar schema")
        sessions = []
        for row in data["sessions"]:
            sessions.append(
                Session(
                    instrument=InstrumentId(Exchange(row["exchange"]), row["symbol"]),
                    session_id=row["session_id"],
                    trading_day=date.fromisoformat(row["trading_day"]),
                    start=datetime.fromisoformat(row["start"]),
                    end=datetime.fromisoformat(row["end"]),
                    phase=MarketPhase(row["phase"]),
                    permissions=Permissions(**row["permissions"]),
                    rule_version=data["version"],
                    source_id=data["source_id"],
                    available_at=datetime.fromisoformat(row["available_at"]),
                )
            )
        return cls(
            sessions,
            trading_days=[date.fromisoformat(day) for day in data["trading_days"]],
            coverage_start=date.fromisoformat(data["coverage_start"]),
            coverage_end=date.fromisoformat(data["coverage_end"]),
            version=data["version"],
            source_id=data["source_id"],
            available_at=datetime.fromisoformat(data["available_at"]),
        )

    @classmethod
    def from_profile_file(cls, data: Mapping[str, object], path: Path | str) -> TradingCalendar:
        """从紧凑模板文件展开逐日显式 Session (schema_version 2, S4-05)."""
        from qh_trader.data.session_templates import SessionProfile, build_sessions, parse_night_exceptions

        version = str(data["version"])
        source_id = str(data["source_id"])
        available_at = datetime.fromisoformat(str(data["available_at"]))
        trading_days = tuple(sorted(date.fromisoformat(str(day)) for day in data["trading_days"]))
        night_exceptions = parse_night_exceptions(data.get("night_session_exceptions"))  # type: ignore[arg-type]
        profiles = tuple(
            SessionProfile(
                instrument=InstrumentId(Exchange(str(row["exchange"])), str(row["symbol"])),
                product=str(row["product"]),
                has_night=bool(row["has_night"]),
                night_close=(time.fromisoformat(str(row["night_close"])) if row.get("night_close") else None),
                day_auction_style=str(row["day_auction_style"]),
                source_id=source_id,
                rule_version=version,
                available_at=available_at,
            )
            for row in data["session_profiles"]  # type: ignore[index]
        )
        sessions = build_sessions(profiles, trading_days, night_exceptions=night_exceptions)
        return cls(
            sessions,
            trading_days=trading_days,
            coverage_start=date.fromisoformat(str(data["coverage_start"])),
            coverage_end=date.fromisoformat(str(data["coverage_end"])),
            version=version,
            source_id=source_id,
            available_at=available_at,
        )

    def _require_coverage(self, day: date, known_at: datetime | None = None) -> None:
        require_date(day, "trading_day")
        if self.version is None or self.coverage_start is None or self.coverage_end is None:
            raise MissingRuleError("an explicit versioned trading calendar is required")
        if not self.coverage_start <= day <= self.coverage_end:
            raise MissingRuleError("date is outside supplied calendar coverage")
        if known_at is not None and (self.available_at is None or self.available_at > utc_timestamp(known_at)):
            raise MissingRuleError("calendar version is not visible at the requested time")

    def is_trading_day(self, day: date) -> bool:
        self._require_coverage(day)
        return day in self.trading_days

    def next_trading_day(self, day: date) -> date:
        self._require_coverage(day)
        candidates = [item for item in self.trading_days if item > day]
        if not candidates:
            raise MissingRuleError("next trading day is outside supplied calendar coverage")
        return min(candidates)

    def previous_trading_day(self, day: date) -> date:
        self._require_coverage(day)
        candidates = [item for item in self.trading_days if item < day]
        if not candidates:
            raise MissingRuleError("previous trading day is outside supplied calendar coverage")
        return max(candidates)

    def holiday_starts(self) -> tuple[date, ...]:
        """覆盖区间内每段法定假日的首日 (相邻交易日之间第一个非交易的工作日)；只隔周末不算假日.

        供长假钩子 (FR-RISK-06) 使用：节前最后一个交易日 = 该假日首日之前的最后一个交易日。
        """
        days = sorted(self.trading_days)
        starts: list[date] = []
        for previous, following in zip(days, days[1:], strict=False):
            probe = previous + timedelta(days=1)
            while probe < following:
                if probe.weekday() < 5:
                    starts.append(probe)
                    break
                probe += timedelta(days=1)
        return tuple(starts)

    def sessions_for_day(
        self,
        instrument: InstrumentId,
        trading_day: date,
        *,
        known_at: datetime | None = None,
    ) -> tuple[Session, ...]:
        self._require_coverage(trading_day, known_at)
        if trading_day not in self.trading_days:
            return ()
        cutoff = utc_timestamp(known_at) if known_at is not None else None
        sessions = tuple(
            sorted(
                (
                    row
                    for row in self._sessions
                    if row.instrument == instrument
                    and row.trading_day == trading_day
                    and (cutoff is None or row.available_at <= cutoff)
                ),
                key=lambda row: row.start,
            )
        )
        if not sessions:
            raise MissingRuleError("sessions for this instrument and trading day have not been supplied")
        return sessions

    def build_sessions_for_day(self, instrument: InstrumentId, trading_day: date) -> tuple[Session, ...]:
        """Compatibility name: return registered sessions, never manufacture a default template."""
        return self.sessions_for_day(instrument, trading_day)

    def get_trading_day(
        self,
        timestamp: datetime,
        instrument: InstrumentId,
        *,
        known_at: datetime | None = None,
    ) -> date:
        at = utc_timestamp(timestamp)
        cutoff = utc_timestamp(known_at) if known_at is not None else None
        matches = [
            row
            for row in self._sessions
            if row.instrument == instrument and row.contains(at) and (cutoff is None or row.available_at <= cutoff)
        ]
        if not matches:
            raise MissingRuleError("no registered session covers this instrument and timestamp")
        if len(matches) != 1:
            raise AmbiguousRuleError("multiple registered sessions cover this timestamp")
        self._require_coverage(matches[0].trading_day, known_at)
        return matches[0].trading_day


def project_product_calendar(
    config_path: Path | str,
    product: str,
    instruments: tuple[InstrumentId, ...],
    *,
    window: tuple[date, date] | None = None,
) -> TradingCalendar:
    """把紧凑品种模板投影到实际使用的合约集合上，得到版本化日历 (S4-05/S4-06).

    逐合约展开的日历文件会随合约数线性膨胀，因此模板文件只登记每个品种一个代表合约；
    运行时按品种模板克隆到本次真正交易的合约，日历版本、来源与可用时刻保持与模板一致。
    """
    from qh_trader.data.session_templates import SessionProfile, build_sessions, parse_night_exceptions

    data = json.loads(Path(config_path).read_text(encoding="utf-8"))
    if int(data.get("schema_version", 0)) != 2:
        raise ValueError("project_product_calendar requires a schema_version 2 session template file")
    match = next(
        (row for row in data["session_profiles"] if str(row["product"]).casefold() == product.casefold()),
        None,
    )
    if match is None:
        raise ValueError(f"session template has no profile for product {product}")
    version = str(data["version"])
    source_id = str(data["source_id"])
    available_at = datetime.fromisoformat(str(data["available_at"]))
    trading_days = tuple(sorted(date.fromisoformat(str(day)) for day in data["trading_days"]))
    night_exceptions = parse_night_exceptions(data.get("night_session_exceptions"))
    if window is not None:
        trading_days = tuple(day for day in trading_days if window[0] <= day <= window[1])
    if not trading_days:
        raise ValueError(f"session template has no trading day inside the requested window for {product}")
    night_close = time.fromisoformat(str(match["night_close"])) if match.get("night_close") else None
    profiles = tuple(
        SessionProfile(
            instrument=instrument,
            product=product,
            has_night=bool(match["has_night"]),
            night_close=night_close,
            day_auction_style=str(match["day_auction_style"]),
            source_id=source_id,
            rule_version=version,
            available_at=available_at,
        )
        for instrument in instruments
    )
    return TradingCalendar(
        build_sessions(profiles, trading_days, night_exceptions=night_exceptions),
        trading_days=trading_days,
        coverage_start=trading_days[0],
        coverage_end=trading_days[-1],
        version=version,
        source_id=source_id,
        available_at=available_at,
    )
