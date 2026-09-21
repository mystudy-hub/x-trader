"""[Data 层] 日线 Bar 的开盘时段归属重标 (S4-05, FR-EXEC-02, A21).

免费源日线只给出交易日与 OHLC，不说明 Open 属于哪个时段。交易所口径的日线以交易日为单位：
有夜盘的品种，交易日 T 的首笔成交发生在前一自然日 21:00 的夜盘集合竞价；无夜盘或公告取消夜盘时
首笔成交在 T 日 09:00 日盘竞价。引擎若把夜盘首笔价当作 09:00 日盘开盘价撮合，就是 A21 禁止的
"含夜盘日线 Open 冒充日盘 Open"。

本模块按版本化日历把日线 Bar 的 ``open_time`` / ``bar_start`` / ``session_id`` 重标到当日首个可撮合
连续时段的开始时刻，并以 ``QualityFlag.SYNTHETIC`` 与 ``DAILY_OPEN_TIMING_ASSUMPTION`` 明示这是
交易所惯例假设而非来源证据；来源语义核验前不得用于精确核算。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from qh_trader.core.constants import MarketPhase, MissingRuleError, QualityFlag
from qh_trader.core.objects import Bar, InstrumentId
from qh_trader.data.calendar import TradingCalendar

DAILY_OPEN_TIMING_ASSUMPTION = (
    "exchange daily-bar convention: open = first trade of the trading day, i.e. the night-session "
    "auction open when the versioned calendar lists a night session for that trading day, otherwise "
    "the day-session auction open; the source did not state its open semantics (pending verification)"
)


def retime_daily_bars(bars: Sequence[Bar], calendar: TradingCalendar, instrument: InstrumentId) -> list[Bar]:
    """把日线 Bar 的开盘时点重标到日历中当日首个可撮合连续时段；缺时段的交易日明确失败."""
    out: list[Bar] = []
    for bar in bars:
        if bar.instrument != instrument:
            raise ValueError("bar instrument does not match the calendar projection")
        if bar.interval != "1d":
            raise ValueError("only daily bars carry a trading-day open that needs session attribution")
        sessions = calendar.sessions_for_day(instrument, bar.meta.trading_day)
        matchable = [s for s in sessions if s.permissions.match and s.phase == MarketPhase.CONTINUOUS]
        if not matchable:
            raise MissingRuleError(f"no matchable continuous session for {instrument} on {bar.meta.trading_day}")
        first = min(matchable, key=lambda s: s.start)
        last_end = max(s.end for s in matchable)
        meta = replace(
            bar.meta,
            session_id=first.session_id,
            event_time=last_end,
            available_at=last_end,
            quality_flags=bar.meta.quality_flags | QualityFlag.SYNTHETIC,
        )
        out.append(
            replace(
                bar,
                meta=meta,
                bar_start=first.start,
                open_time=first.start,
                bar_end=last_end,
                includes_auction=True,
            )
        )
    return out
