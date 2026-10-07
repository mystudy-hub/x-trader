"""TDX 研究回测的时序与完整性边界；人工价格只用于验证，不作为收益证据。"""

from datetime import date, datetime, time, timedelta

import pytest

from qh_trader.core.constants import Exchange, MarketPhase
from qh_trader.core.objects import InstrumentId
from qh_trader.data.calendar import CHINA_TZ, TradingCalendar
from qh_trader.data.session_templates import SessionProfile, build_sessions
from scripts.run_tdx_ema_research import (
    check_published_quality,
    inspect_days,
    latest_complete_days,
    normalize_minutes,
)


def test_trading_day_label_orders_friday_night_before_monday_day():
    days = [date(2026, 9, 17), date(2026, 9, 18), date(2026, 9, 21)]
    rows = [
        {"datetime": value}
        for value in (
            "2026-09-17T01:01:00+00:00",
            "2026-09-21T01:01:00+00:00",
            "2026-09-21T13:01:00+00:00",
        )
    ]
    normalized, skipped = normalize_minutes(rows, days)
    assert skipped == 1
    assert normalized[0]["datetime"] == datetime(2026, 9, 18, 21, 1, tzinfo=CHINA_TZ)
    assert normalized[1]["datetime"] == datetime(2026, 9, 21, 9, 1, tzinfo=CHINA_TZ)
    assert {row["trading_day"] for row in normalized} == {date(2026, 9, 21)}
    assert normalized[0]["source_datetime"] == "2026-09-21T13:01:00+00:00"


def sample_calendar():
    instrument = InstrumentId(Exchange.SHFE, "rb2701")
    days = (date(2026, 9, 18), date(2026, 9, 21))
    known = datetime(2026, 1, 1, tzinfo=CHINA_TZ)
    profile = SessionProfile(instrument, "rb", True, time(23), "RE_AUCTION", "test", "test-v1", known)
    calendar = TradingCalendar(
        build_sessions((profile,), days),
        trading_days=days,
        coverage_start=days[0],
        coverage_end=days[-1],
        version="test-v1",
        source_id="test",
        available_at=known,
    )
    return instrument, days, calendar


def test_missing_minute_is_not_hidden_by_matching_daily_volume():
    instrument, days, calendar = sample_calendar()
    rows = []
    for session in calendar.sessions_for_day(instrument, days[-1]):
        if session.phase != MarketPhase.CONTINUOUS:
            continue
        at = session.start + timedelta(minutes=1)
        while at <= session.end:
            rows.append(
                {
                    "datetime": at,
                    "trading_day": days[-1],
                    "open": "1000",
                    "high": "1000",
                    "low": "1000",
                    "close": "1000",
                    "volume": 1,
                    "open_interest": 100,
                }
            )
            at += timedelta(minutes=1)
    daily = [
        {"date": str(days[0])},
        {
            "date": str(days[-1]),
            "open": "1000",
            "high": "1000",
            "low": "1000",
            "close": "1000",
            "volume": 345,
            "open_interest": 100,
        },
    ]
    assert inspect_days(rows, daily, calendar, instrument)[0]["complete"]
    rows.pop(50)
    daily[-1]["volume"] = 344
    report = inspect_days(rows, daily, calendar, instrument)[0]
    assert all(report["daily_checks"].values())
    assert report["missing_minutes"] == 1 and not report["complete"]


def test_only_planned_session_tail_is_exempted_from_gap_rejection():
    instrument, days, calendar = sample_calendar()
    session = next(s for s in calendar.sessions_for_day(instrument, days[-1]) if s.session_id == "day_continuous_1")
    gap = {
        "trading_day": str(days[-1]),
        "session_id": session.session_id,
        "kind": "missing",
        "start": session.end - timedelta(minutes=15),
        "end": session.end,
    }
    quality = {"issues": [{"severity": "error", "code": "coverage_gaps"}], "summary": {"coverage": {"gaps": [gap]}}}
    check_published_quality(quality, calendar, instrument)
    gap["start"] -= timedelta(minutes=30)
    with pytest.raises(ValueError, match="unexplained coverage gap"):
        check_published_quality(quality, calendar, instrument)


def test_suffix_selection_never_skips_an_incomplete_middle_day():
    reports = [
        {"trading_day": f"2026-09-{day}", "complete": valid}
        for day, valid in ((21, True), (22, False), (23, True), (24, True))
    ]
    assert latest_complete_days(reports) == [date(2026, 9, 23), date(2026, 9, 24)]
    reports[-1]["complete"] = False
    with pytest.raises(ValueError, match="latest trading day"):
        latest_complete_days(reports)
