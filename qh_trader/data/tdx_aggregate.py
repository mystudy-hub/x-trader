"""[Data 层] 按显式会话将通达信分钟结束时戳聚合为完整窗口 (S1-12, FR-DATA-02)。

每根来源记录代表 ``(datetime - 1m, datetime]``。休市前后的分钟不拼接，
缺少任意一分钟、会话尾部不足目标周期的桶均丢弃；交易日只取显式 Session。
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from qh_trader.core.constants import MarketPhase, MissingRuleError
from qh_trader.core.objects import InstrumentId
from qh_trader.data.calendar import TradingCalendar
from qh_trader.data.schemas import DataValidationError, decimal_value, integer_value, parse_time, validate_ohlc_records

MINUTE = timedelta(minutes=1)


def aggregate_tdx_minutes(
    records: Sequence[Mapping[str, Any]],
    *,
    instrument: InstrumentId,
    calendar: TradingCalendar,
    period: int = 30,
) -> list[dict[str, Any]]:
    """聚合 OHLC/增量成交量，取桶末持仓；原始缺成交额和官方结算价保持 None。"""
    if isinstance(period, bool) or period not in {1, 5, 15, 30, 60}:
        raise ValueError("TDX aggregation period must be 1, 5, 15, 30 or 60 minutes")
    if not isinstance(instrument, InstrumentId):
        raise TypeError("TDX aggregation requires a resolved actual contract")
    if calendar.version is None:
        raise MissingRuleError("TDX minute aggregation requires an explicit versioned calendar")
    ordered: dict[datetime, dict[str, Any]] = {}
    for raw in records:
        row = dict(raw)
        stamp = parse_time(row["datetime"], "Asia/Shanghai")
        if stamp.second or stamp.microsecond:
            raise ValueError("TDX source timestamps must be exact minute ends")
        row["datetime"] = stamp
        if stamp in ordered and ordered[stamp] != row:
            raise ValueError(f"conflicting TDX minute observations at {stamp.isoformat()}")
        ordered[stamp] = row
    rows = [ordered[key] for key in sorted(ordered)]
    report = validate_ohlc_records(
        rows, "datetime", strict=False, instrument=instrument, source_timezone="Asia/Shanghai", require_turnover=False
    )
    if any(issue.issue_type != "TURNOVER_UNAVAILABLE" for issue in report.issues):
        raise DataValidationError(report)
    if not rows:
        return []
    # 先建会话索引，避免对每根分钟线线性扫描整段历史日历。
    sessions = sorted(
        (
            session
            for session in calendar.sessions_in_window(instrument, rows[0]["datetime"] - MINUTE, rows[-1]["datetime"])
            if session.phase == MarketPhase.CONTINUOUS and session.permissions.match
        ),
        key=lambda session: session.start,
    )
    starts = [session.start for session in sessions]
    duration = MINUTE * period
    buckets: dict[tuple[int, datetime], list[dict[str, Any]]] = {}
    for row in rows:
        stamp = row["datetime"]
        minute_start = stamp - MINUTE
        index = bisect_right(starts, minute_start) - 1
        if index < 0 or not sessions[index].contains(minute_start) or stamp > sessions[index].end:
            raise MissingRuleError(f"no continuous session covers TDX minute ending {stamp.isoformat()}")
        session = sessions[index]
        if session.start.second or session.start.microsecond or session.end.second or session.end.microsecond:
            raise ValueError("TDX aggregation requires minute-aligned session boundaries")
        bucket_start = session.start + ((minute_start - session.start) // duration) * duration
        if bucket_start + duration > session.end:
            continue
        buckets.setdefault((index, bucket_start), []).append(row)
    result: list[dict[str, Any]] = []
    for (index, start), minutes in sorted(buckets.items(), key=lambda item: item[0][1]):
        if len(minutes) != period or any(
            row["datetime"] != start + MINUTE * offset for offset, row in enumerate(minutes, 1)
        ):
            continue
        session = sessions[index]
        end = start + duration
        result.append(
            {
                "datetime": end,
                "bar_start": start,
                "bar_end": end,
                "trading_day": session.trading_day,
                "session_id": session.session_id,
                "open": decimal_value(minutes[0]["open"], "open"),
                "high": max(decimal_value(row["high"], "high") for row in minutes),
                "low": min(decimal_value(row["low"], "low") for row in minutes),
                "close": decimal_value(minutes[-1]["close"], "close"),
                "volume": sum(integer_value(row["volume"], "volume") for row in minutes),
                "open_interest": integer_value(minutes[-1]["open_interest"], "open_interest"),
                "turnover": None,
                "settlement_price": None,
                "source_interval": "1m",
                "aggregation": "session_aligned_complete_minutes",
            }
        )
    return result
