"""Sessions come from explicit versioned inputs, including holidays and cancel-only windows."""

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from qh_trader.core.constants import AmbiguousRuleError, Exchange, MarketPhase, MissingRuleError
from qh_trader.core.objects import InstrumentId, Permissions, Session
from qh_trader.data.calendar import CHINA_TZ, TradingCalendar


def registered_calendar(sessions, days, start, end):
    return TradingCalendar(
        sessions,
        trading_days=days,
        coverage_start=start,
        coverage_end=end,
        version="test-v1",
        source_id="synthetic",
        available_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def session(inst, day, start, end, *, phase=MarketPhase.CONTINUOUS, permissions=Permissions(True, True, True)):
    return Session(
        instrument=inst,
        session_id="fixture",
        trading_day=day,
        start=datetime.fromisoformat(start),
        end=datetime.fromisoformat(end),
        phase=phase,
        permissions=permissions,
        rule_version="test-v1",
        source_id="synthetic",
        available_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def test_calendar_uses_explicit_covered_trading_dates(sample_calendar):
    assert sample_calendar.is_trading_day(date(2024, 9, 9))
    assert not sample_calendar.is_trading_day(date(2024, 9, 14))
    assert sample_calendar.previous_trading_day(date(2024, 9, 10)) == date(2024, 9, 9)
    assert sample_calendar.next_trading_day(date(2024, 9, 9)) == date(2024, 9, 10)
    with pytest.raises(MissingRuleError):
        sample_calendar.is_trading_day(date(2024, 10, 1))
    with pytest.raises(MissingRuleError):
        TradingCalendar().is_trading_day(date(2024, 9, 9))


def test_friday_and_saturday_timestamps_follow_registered_night_session():
    inst = InstrumentId(Exchange.SHFE, "au2412")
    monday = date(2024, 10, 21)
    night = session(inst, monday, "2024-10-18T21:00:00+08:00", "2024-10-19T02:30:00+08:00")
    cal = registered_calendar([night], [monday], date(2024, 10, 18), monday)
    assert cal.get_trading_day(datetime(2024, 10, 18, 22, tzinfo=CHINA_TZ), inst) == monday
    assert cal.get_trading_day(datetime(2024, 10, 19, 1, tzinfo=CHINA_TZ), inst) == monday
    with pytest.raises(MissingRuleError):
        cal.get_trading_day(datetime(2024, 10, 19, 3, tzinfo=CHINA_TZ), inst)
    with pytest.raises(ValueError, match="timezone-aware"):
        cal.get_trading_day(datetime(2024, 10, 19, 1), inst)


def test_holiday_never_gets_manufactured_sessions(sample_instrument):
    cal = registered_calendar([], [date(2024, 9, 30), date(2024, 10, 8)], date(2024, 9, 30), date(2024, 10, 8))
    assert not cal.is_trading_day(date(2024, 10, 1))
    assert cal.build_sessions_for_day(sample_instrument, date(2024, 10, 1)) == ()
    with pytest.raises(MissingRuleError):
        cal.build_sessions_for_day(sample_instrument, date(2024, 10, 8))
    invalid = session(sample_instrument, date(2024, 10, 1), "2024-10-01T09:00:00+08:00", "2024-10-01T15:00:00+08:00")
    with pytest.raises(ValueError, match="closed trading date"):
        registered_calendar([invalid], [], date(2024, 10, 1), date(2024, 10, 1))


def test_cancel_only_and_auction_are_separate_explicit_versions():
    inst = InstrumentId(Exchange.CZCE, "MA2409")
    day = date(2024, 9, 9)
    window = session(
        inst,
        day,
        "2024-09-09T08:55:00+08:00",
        "2024-09-09T08:59:00+08:00",
        phase=MarketPhase.CANCEL_ONLY,
        permissions=Permissions(False, True, False),
    )
    cal = registered_calendar([window], [day], day, day)
    at = datetime(2024, 9, 9, 8, 56, tzinfo=CHINA_TZ)
    selected = cal.sessions_for_day(inst, day)[0]
    assert selected.contains(at)
    assert not selected.permissions.submit and selected.permissions.cancel and not selected.permissions.match
    assert not selected.contains(selected.end)
    with pytest.raises(MissingRuleError):
        cal.sessions_for_day(inst, day, known_at=datetime(2023, 1, 1, tzinfo=timezone.utc))
    with pytest.raises(AmbiguousRuleError):
        registered_calendar([window, replace(window, session_id="conflict")], [day], day, day)
