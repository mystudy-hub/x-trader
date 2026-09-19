"""[Data 层] 基于版本化交易日历的时段权限门 (S3-10, FR-CAL-07/09).

只回答"某合约在某时刻是否允许报单 / 撤单 / 撮合"，不制造默认模板：
未登记的时刻返回 None，由调用方按"不授予任何权限"处理。
"""

from __future__ import annotations

from datetime import datetime

from qh_trader.core.clock import utc_timestamp
from qh_trader.core.constants import MarketPhase, MissingRuleError
from qh_trader.core.objects import InstrumentId, Permissions, Session
from qh_trader.data.calendar import CHINA_TZ, TradingCalendar


class CalendarSessionGate:
    """把 TradingCalendar 的 Sessions 暴露为 SessionGatePort."""

    def __init__(self, calendar: TradingCalendar, *, known_at: datetime | None = None) -> None:
        if calendar.version is None:
            raise MissingRuleError("session gate requires an explicit versioned calendar")
        self._calendar = calendar
        self._known_at = utc_timestamp(known_at) if known_at is not None else None
        self._sessions: dict[InstrumentId, tuple[Session, ...]] = {}
        for session in calendar._sessions:  # noqa: SLF001 - read-only view of one immutable calendar version
            if self._known_at is not None and session.available_at > self._known_at:
                continue
            self._sessions.setdefault(session.instrument, ())
            self._sessions[session.instrument] = self._sessions[session.instrument] + (session,)
        self._sessions = {key: tuple(sorted(value, key=lambda s: s.start)) for key, value in self._sessions.items()}

    def version(self) -> str:
        return str(self._calendar.version)

    def session_at(self, instrument: InstrumentId, at: datetime) -> Session | None:
        moment = utc_timestamp(at)
        for session in self._sessions.get(instrument, ()):
            if session.start <= moment < session.end:
                return session
        return None

    def permissions_at(self, instrument: InstrumentId, at: datetime) -> Permissions | None:
        session = self.session_at(instrument, at)
        return session.permissions if session is not None else None

    def next_submit_time(self, instrument: InstrumentId, after: datetime) -> datetime | None:
        """严格晚于 after 的下一个允许报单的时刻 (当前时段允许则返回 after 本身)."""
        moment = utc_timestamp(after)
        current = self.session_at(instrument, moment)
        if current is not None and current.permissions.submit:
            return moment
        for session in self._sessions.get(instrument, ()):
            if session.start > moment and session.permissions.submit:
                return session.start
        return None

    def next_session_open(
        self,
        instrument: InstrumentId,
        after: datetime,
        *,
        day_session_only: bool = False,
    ) -> datetime | None:
        """严格晚于 after 的下一个可撮合连续交易/竞价撮合时段的开始时刻."""
        moment = utc_timestamp(after)
        for session in self._sessions.get(instrument, ()):
            if session.start <= moment or not session.permissions.match:
                continue
            if session.phase not in (MarketPhase.CONTINUOUS, MarketPhase.AUCTION_MATCH):
                continue
            if day_session_only and session.start.astimezone(CHINA_TZ).hour >= 20:
                continue
            return session.start
        return None
