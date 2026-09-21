"""日线开盘时段重标 (B5 / A21)：有夜盘品种的日线 Open 归属夜盘首笔，假日前夜与无夜盘品种归属日盘."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from qh_trader.core.constants import Exchange, MissingRuleError, QualityFlag
from qh_trader.core.objects import Bar, InstrumentId, RecordMeta
from qh_trader.data.calendar import CHINA_TZ, project_product_calendar
from qh_trader.data.daily_timing import retime_daily_bars

RB = InstrumentId(Exchange.SHFE, "rb2501")
AP = InstrumentId(Exchange.CZCE, "AP2501")


def daily(instrument: InstrumentId, day: date) -> Bar:
    start = datetime.combine(day, time(9), CHINA_TZ)
    end = datetime.combine(day, time(15), CHINA_TZ)
    meta = RecordMeta(
        event_time=end,
        available_at=end,
        ingested_at=end,
        trading_day=day,
        session_id="day_continuous",
        source_id="sina_futures",
        source_version="v",
        ingest_seq=1,
        quality_flags=QualityFlag.TURNOVER_UNAVAILABLE,
    )
    return Bar(
        instrument=instrument,
        meta=meta,
        bar_start=start,
        bar_end=end,
        open_time=start,
        interval="1d",
        open=Decimal(3000),
        high=Decimal(3010),
        low=Decimal(2990),
        close=Decimal(3005),
        volume=10,
        turnover=Decimal(0),
        open_interest=1,
        includes_auction=False,
    )


def template(tmp_path: Path, days: list[date], *, has_night: bool, symbol: str, exchange: str, product: str) -> Path:
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
                "exchange": exchange,
                "symbol": symbol,
                "product": product,
                "has_night": has_night,
                "night_close": "23:00:00" if has_night else None,
                "day_auction_style": "RE_AUCTION" if has_night else "STANDARD",
            }
        ],
        "night_session_exceptions": [],
    }
    path = tmp_path / f"{product}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_night_product_open_moves_to_previous_evening_except_after_holiday(tmp_path: Path) -> None:
    days = [date(2024, 12, 30), date(2024, 12, 31), date(2025, 1, 2), date(2025, 1, 3)]
    calendar = project_product_calendar(
        template(tmp_path, days, has_night=True, symbol="rb2501", exchange="SHFE", product="rb"), "rb", (RB,)
    )
    bars = retime_daily_bars([daily(RB, d) for d in days], calendar, RB)
    opens = [
        (b.meta.trading_day.isoformat(), b.open_time.astimezone(CHINA_TZ).strftime("%m-%d %H:%M"), b.meta.session_id)
        for b in bars
    ]
    assert opens == [
        ("2024-12-30", "12-30 09:00", "day_continuous_1"),  # 覆盖首日无夜盘证据
        ("2024-12-31", "12-30 21:00", "night_continuous"),
        ("2025-01-02", "01-02 09:00", "day_continuous_1"),  # 元旦前夜无夜盘
        ("2025-01-03", "01-02 21:00", "night_continuous"),
    ]
    assert all(b.includes_auction and b.meta.quality_flags & QualityFlag.SYNTHETIC for b in bars)
    assert all(b.bar_end.astimezone(CHINA_TZ).time() == time(15) for b in bars)
    # 序列仍严格有序、不重叠 (可发布)
    assert all(prev.bar_end <= cur.bar_start for prev, cur in zip(bars, bars[1:], strict=False))


def test_product_without_night_keeps_day_open(tmp_path: Path) -> None:
    days = [date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6)]
    calendar = project_product_calendar(
        template(tmp_path, days, has_night=False, symbol="AP2501", exchange="CZCE", product="AP"), "AP", (AP,)
    )
    bars = retime_daily_bars([daily(AP, d) for d in days], calendar, AP)
    assert all(
        b.open_time.astimezone(CHINA_TZ).time() == time(9) and b.meta.session_id == "day_continuous_1" for b in bars
    )


def test_trading_day_missing_from_calendar_fails(tmp_path: Path) -> None:
    days = [date(2025, 1, 2), date(2025, 1, 3)]
    calendar = project_product_calendar(
        template(tmp_path, days, has_night=True, symbol="rb2501", exchange="SHFE", product="rb"), "rb", (RB,)
    )
    with pytest.raises(MissingRuleError):
        retime_daily_bars([daily(RB, date(2025, 1, 6))], calendar, RB)


def test_retime_is_visible_through_the_engine_calendar_check(tmp_path: Path) -> None:
    from qh_trader.data.session_gate import CalendarSessionGate

    days = [date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6)]
    calendar = project_product_calendar(
        template(tmp_path, days, has_night=True, symbol="rb2501", exchange="SHFE", product="rb"), "rb", (RB,)
    )
    gate = CalendarSessionGate(calendar)
    bars = retime_daily_bars([daily(RB, d) for d in days], calendar, RB)
    # 1-06 的 Bar 在 1-03 21:00 开盘，日历把该时刻归属交易日 1-06，与记录一致
    assert gate.trading_day_at(RB, bars[-1].open_time) == date(2025, 1, 6)
    assert bars[-1].open_time == datetime(2025, 1, 3, 13, 0, tzinfo=timezone.utc)
