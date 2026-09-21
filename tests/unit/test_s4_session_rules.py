"""S4 时段规则回归：法定假日前夜无夜盘 (按交易日序列)、公告例外覆盖、假日首日识别 (M2)."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from pathlib import Path

from qh_trader.core.constants import Exchange
from qh_trader.core.objects import InstrumentId
from qh_trader.data.calendar import CHINA_TZ, TradingCalendar, project_product_calendar
from qh_trader.data.session_gate import CalendarSessionGate
from qh_trader.data.session_templates import has_night_session

RB = InstrumentId(Exchange.SHFE, "rb2601")


def test_night_session_rule_uses_weekday_gaps_not_calendar_day_count() -> None:
    # 2024-12-31 (二) -> 2025-01-02 (四)：中间 01-01 是非交易工作日 => 12-31 晚无夜盘 (旧规则按间隔 2 天误生成夜盘)
    assert has_night_session(date(2025, 1, 2), date(2024, 12, 31)) is False
    # 周五 -> 周一：只隔周末，周五晚有夜盘
    assert has_night_session(date(2025, 1, 6), date(2025, 1, 3)) is True
    # 周五 -> 下周三 (周一、二为假日)：周五晚无夜盘
    assert has_night_session(date(2025, 4, 9), date(2025, 4, 4)) is False
    # 公告例外优先
    assert has_night_session(date(2025, 1, 6), date(2025, 1, 3), night_exceptions={date(2025, 1, 3): False}) is False


def _template(tmp_path: Path, days: list[date], exceptions: list[dict] | None = None) -> Path:
    payload = {
        "schema_version": 2,
        "version": "t-v1",
        "source_id": "t",
        "available_at": "2024-01-01T00:00:00+00:00",
        "coverage_start": days[0].isoformat(),
        "coverage_end": days[-1].isoformat(),
        "trading_days": [d.isoformat() for d in days],
        "session_profiles": [
            {
                "exchange": "SHFE",
                "symbol": "rb2601",
                "product": "rb",
                "has_night": True,
                "night_close": "23:00:00",
                "day_auction_style": "RE_AUCTION",
            }
        ],
        "night_session_exceptions": exceptions or [],
    }
    path = tmp_path / "t.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_projected_calendar_drops_night_before_a_weekday_holiday_and_marks_holiday_start(tmp_path: Path) -> None:
    days = [date(2024, 12, 30), date(2024, 12, 31), date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6)]
    calendar = project_product_calendar(_template(tmp_path, days), "rb", (RB,), window=(days[0], days[-1]))
    gate = CalendarSessionGate(calendar)
    assert (
        gate.permissions_at(RB, datetime.combine(date(2024, 12, 30), time(21, 30), CHINA_TZ)) is not None
    )  # 12-30 晚有夜盘
    assert (
        gate.permissions_at(RB, datetime.combine(date(2024, 12, 31), time(21, 30), CHINA_TZ)) is None
    )  # 元旦前夜无夜盘
    assert (
        gate.permissions_at(RB, datetime.combine(date(2025, 1, 3), time(21, 30), CHINA_TZ)) is not None
    )  # 周五晚有夜盘
    assert calendar.holiday_starts() == (date(2025, 1, 1),)


def test_announcement_exception_overrides_the_rule(tmp_path: Path) -> None:
    days = [date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6)]
    template = _template(
        tmp_path, days, exceptions=[{"eve": "2025-01-03", "has_night": False, "source": "synthetic notice"}]
    )
    calendar = TradingCalendar.from_file(template)
    gate = CalendarSessionGate(calendar)
    assert gate.permissions_at(RB, datetime(2025, 1, 3, 13, 30, tzinfo=timezone.utc)) is None  # 21:30 CST 无夜盘
    assert gate.permissions_at(RB, datetime(2025, 1, 2, 13, 30, tzinfo=timezone.utc)) is not None
