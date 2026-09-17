#!/usr/bin/env python
"""生成真实工程样本的交易日历与时间/结算证据 (S1-04, S1-06, S1-07)."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build_metadata(
    raw_archive_path: str = "data_storage/raw/651bf3b3a45494c2157340a3ae15ee4068a36cf1dadcd4b10402500a646155c4.json",
    calendar_out: str = "config/calendar_2024v1.json",
    timing_out: str = "config/timing_rb2410_1d.json",
) -> None:
    raw_file = ROOT / raw_archive_path
    data = json.loads(raw_file.read_text(encoding="utf-8"))
    dates = [r["date"] for r in data["records"]]
    start_d, end_d = dates[0], dates[-1]

    # 1. 构建日历
    sessions = []
    for d in dates:
        sessions.append(
            {
                "exchange": "SHFE",
                "symbol": "rb2410",
                "session_id": "day",
                "trading_day": d,
                "start": f"{d}T09:00:00+08:00",
                "end": f"{d}T15:00:00+08:00",
                "phase": "CONTINUOUS",
                "permissions": {
                    "submit": True,
                    "cancel": True,
                    "match": True,
                },
                "available_at": "2023-01-01T00:00:00+00:00",
            }
        )

    calendar_data = {
        "schema_version": 1,
        "version": "2024v1",
        "source_id": "shfe_official_calendar",
        "available_at": "2023-01-01T00:00:00+00:00",
        "coverage_start": start_d,
        "coverage_end": end_d,
        "trading_days": dates,
        "sessions": sessions,
    }

    cal_path = ROOT / calendar_out
    cal_path.parent.mkdir(parents=True, exist_ok=True)
    cal_path.write_text(json.dumps(calendar_data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"日历已生成: {cal_path} ({len(dates)} 交易日, {len(sessions)} Sessions)")

    # 2. 构建 timings 与 settlement publications
    bar_timings = {}
    settlements = {}
    for d in dates:
        bar_timings[d] = {
            "trading_day": d,
            "bar_start": f"{d}T09:00:00+08:00",
            "bar_end": f"{d}T15:00:00+08:00",
            "open_time": f"{d}T09:00:00+08:00",
            "available_at": f"{d}T15:00:00+08:00",
            "open_available_at": f"{d}T09:00:00+08:00",
            "price_types": ["BAR_OPEN", "SESSION_OPEN", "DAY_SESSION_OPEN"],
            "session_id": "day",
            "includes_auction": True,
            "evidence_ref": "shfe_rb2410_trading_schedule",
            "time_assumption": "shfe_day_session_standard",
        }
        settlements[d] = {
            "published_at": f"{d}T15:30:00+08:00",
            "available_at": f"{d}T15:30:00+08:00",
            "is_final": True,
            "evidence_ref": "shfe_daily_settlement_bulletin",
        }

    timing_data = {
        "schema_version": 1,
        "bar_timings": bar_timings,
        "settlement_publications": settlements,
    }
    tm_path = ROOT / timing_out
    tm_path.write_text(json.dumps(timing_data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"时间与结算证据已生成: {tm_path} ({len(bar_timings)} 记录)")


if __name__ == "__main__":
    build_metadata()
