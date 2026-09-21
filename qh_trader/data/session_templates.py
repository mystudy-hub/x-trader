"""[Data 层] 品种级交易时段模板 (S4-05, FR-CAL-01/02/07/08/10, A25 新增范围).

按"合约 + 规则版本"生成显式 Session，而不是按交易所写死一份：

- 夜盘统一 20:55-20:59 申报、20:59-21:00 撮合、21:00 起连续；收盘分 23:00 / 01:00 / 02:30 三档；
- 日盘统一 09:00-10:15 连续、10:15-10:30 小节休市、10:30-11:30 连续、11:30-13:30 午休、13:30-15:00 连续；
- 日盘竞价风格三选一：上期所/大商所夜盘品种"再竞价"、郑商所夜盘品种"只撤不报"、无夜盘品种"标准日盘竞价"；
- 夜盘交易日归属：夜盘挂在"下一交易日"下；周五夜盘属下周一。

法定假日前第一个工作日无夜盘、节后首日竞价顺延等公告例外需要版本化公告归档；
本模板用"前一交易日到本交易日的间隔 > 3 个自然日"识别长假并整体跳过该交易日的夜盘时段，
把该假设写入 `available_at`/来源，缺口登记在 `config/data_coverage.yaml` (A05/A25-05)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from qh_trader.core.clock import utc_timestamp
from qh_trader.core.constants import MarketPhase
from qh_trader.core.objects import InstrumentId, Permissions, Session, require_date, require_text

CHINA_TZ = ZoneInfo("Asia/Shanghai")

_SUBMIT = Permissions(submit=True, cancel=True, match=False)
_MATCH = Permissions(submit=False, cancel=False, match=True)
_CONTINUOUS = Permissions(submit=True, cancel=True, match=True)
_CANCEL_ONLY = Permissions(submit=False, cancel=True, match=False)
_NONE = Permissions(submit=False, cancel=False, match=False)

# 商品期货日盘固定骨架 (09:00-10:15 / 10:30-11:30 / 13:30-15:00 + 两段休市)。
_DAY_CONTINUOUS_BLOCKS: tuple[tuple[time, time, str], ...] = (
    (time(9, 0), time(10, 15), "day_continuous_1"),
    (time(10, 30), time(11, 30), "day_continuous_2"),
    (time(13, 30), time(15, 0), "day_continuous_3"),
)
_DAY_BREAKS: tuple[tuple[time, time, str], ...] = (
    (time(10, 15), time(10, 30), "morning_break"),
    (time(11, 30), time(13, 30), "lunch_break"),
)


@dataclass(frozen=True, slots=True)
class SessionProfile:
    """一个已登记合约的时段模板；`day_auction_style` 取值见 `DayAuctionStyle`."""

    instrument: InstrumentId
    product: str
    has_night: bool
    night_close: time | None
    day_auction_style: str
    source_id: str
    rule_version: str
    available_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, InstrumentId):
            raise TypeError("session profile requires an actual contract instrument")
        require_text(self.product, "product")
        require_text(self.day_auction_style, "day_auction_style")
        require_text(self.source_id, "source_id")
        require_text(self.rule_version, "rule_version")
        object.__setattr__(self, "available_at", utc_timestamp(self.available_at))
        if self.has_night and self.night_close is None:
            raise ValueError("night-session profile must declare a close time")
        if not self.has_night and self.night_close is not None:
            raise ValueError("profile without night session cannot declare a close time")


def _session(
    profile: SessionProfile,
    trading_day: date,
    start: datetime,
    end: datetime,
    session_id: str,
    phase: MarketPhase,
    permissions: Permissions,
) -> Session:
    return Session(
        instrument=profile.instrument,
        session_id=session_id,
        trading_day=trading_day,
        start=start,
        end=end,
        phase=phase,
        permissions=permissions,
        rule_version=profile.rule_version,
        source_id=profile.source_id,
        available_at=profile.available_at,
    )


def _at(day: date, moment: time) -> datetime:
    return datetime.combine(day, moment, tzinfo=CHINA_TZ)


def holiday_gap_days(trading_day: date, previous_trading_day: date) -> int:
    """两个相邻交易日之间的自然日间隔."""
    require_date(trading_day, "trading_day")
    require_date(previous_trading_day, "previous_trading_day")
    return (trading_day - previous_trading_day).days


def has_night_session(
    trading_day: date,
    previous_trading_day: date,
    *,
    night_exceptions: Mapping[date, bool] | None = None,
) -> bool:
    """交易日 ``trading_day`` 的夜盘 (发生在 ``previous_trading_day`` 晚间) 是否存在.

    规则：法定节假日前最后一个交易日晚无夜盘。两个相邻交易日之间若存在任何非交易的工作日
    (周一至周五)，即视为法定节假日间隔；只隔周末的仍有夜盘 (周五晚夜盘归属下周一)。
    ``night_exceptions`` 为公告归档给出的显式覆盖 {前一交易日: 是否有夜盘}，优先级最高。
    自然日间隔阈值 (旧规则 "> 3 天") 会把元旦等短假前夜错生成夜盘，已弃用 (A05 / A25-05)。
    """
    if night_exceptions and previous_trading_day in night_exceptions:
        return bool(night_exceptions[previous_trading_day])
    day = previous_trading_day + timedelta(days=1)
    while day < trading_day:
        if day.weekday() < 5:
            return False
        day += timedelta(days=1)
    return True


def build_sessions_for_trading_day(
    profile: SessionProfile,
    trading_day: date,
    previous_trading_day: date,
    *,
    night_exceptions: Mapping[date, bool] | None = None,
) -> tuple[Session, ...]:
    """生成一个合约在一个交易日下的全部显式时段 (夜盘 + 日盘)."""
    sessions: list[Session] = []

    if profile.has_night and has_night_session(trading_day, previous_trading_day, night_exceptions=night_exceptions):
        night_day = previous_trading_day
        close = profile.night_close
        assert close is not None  # 由 SessionProfile 保证
        # 23:00 收盘与开市同日；01:00 / 02:30 为次日凌晨。
        close_day = night_day if close.hour >= 20 else night_day + timedelta(days=1)
        sessions.append(
            _session(
                profile,
                trading_day,
                _at(night_day, time(20, 55)),
                _at(night_day, time(20, 59)),
                "night_auction_submit",
                MarketPhase.AUCTION_SUBMIT,
                _SUBMIT,
            )
        )
        sessions.append(
            _session(
                profile,
                trading_day,
                _at(night_day, time(20, 59)),
                _at(night_day, time(21, 0)),
                "night_auction_match",
                MarketPhase.AUCTION_MATCH,
                _MATCH,
            )
        )
        sessions.append(
            _session(
                profile,
                trading_day,
                _at(night_day, time(21, 0)),
                _at(close_day, close),
                "night_continuous",
                MarketPhase.CONTINUOUS,
                _CONTINUOUS,
            )
        )

    # 日盘开盘前的竞价/撤单窗
    style = profile.day_auction_style
    if style == "CANCEL_ONLY":
        sessions.append(
            _session(
                profile,
                trading_day,
                _at(trading_day, time(8, 55)),
                _at(trading_day, time(8, 59)),
                "day_cancel_only",
                MarketPhase.CANCEL_ONLY,
                _CANCEL_ONLY,
            )
        )
        sessions.append(
            _session(
                profile,
                trading_day,
                _at(trading_day, time(8, 59)),
                _at(trading_day, time(9, 0)),
                "day_waiting",
                MarketPhase.WAITING,
                _NONE,
            )
        )
    else:
        sessions.append(
            _session(
                profile,
                trading_day,
                _at(trading_day, time(8, 55)),
                _at(trading_day, time(8, 59)),
                "day_auction_submit",
                MarketPhase.AUCTION_SUBMIT,
                _SUBMIT,
            )
        )
        sessions.append(
            _session(
                profile,
                trading_day,
                _at(trading_day, time(8, 59)),
                _at(trading_day, time(9, 0)),
                "day_auction_match",
                MarketPhase.AUCTION_MATCH,
                _MATCH,
            )
        )

    for start, end, session_id in _DAY_CONTINUOUS_BLOCKS:
        sessions.append(
            _session(
                profile,
                trading_day,
                _at(trading_day, start),
                _at(trading_day, end),
                session_id,
                MarketPhase.CONTINUOUS,
                _CONTINUOUS,
            )
        )
    for start, end, session_id in _DAY_BREAKS:
        sessions.append(
            _session(
                profile,
                trading_day,
                _at(trading_day, start),
                _at(trading_day, end),
                session_id,
                MarketPhase.BREAK,
                _NONE,
            )
        )

    return tuple(sorted(sessions, key=lambda item: item.start))


def build_sessions(
    profiles: tuple[SessionProfile, ...],
    trading_days: tuple[date, ...],
    *,
    night_exceptions: Mapping[date, bool] | None = None,
) -> tuple[Session, ...]:
    """按交易序列展开全部合约时段；`trading_days` 必须严格递增."""
    ordered = tuple(sorted(trading_days))
    expanded: list[Session] = []
    for profile in profiles:
        for index, day in enumerate(ordered):
            if index == 0:
                # 覆盖区间首日没有更早的交易日证据时，不生成夜盘，避免凭猜测归因。
                previous = day - timedelta(days=1)
                sessions = build_sessions_for_trading_day(profile, day, previous, night_exceptions=night_exceptions)
                expanded.extend(s for s in sessions if not s.session_id.startswith("night_"))
                continue
            expanded.extend(
                build_sessions_for_trading_day(profile, day, ordered[index - 1], night_exceptions=night_exceptions)
            )
    return tuple(
        sorted(expanded, key=lambda item: (item.instrument.exchange.value, item.instrument.symbol, item.start))
    )


def parse_night_exceptions(rows: Sequence[Mapping[str, object]] | None) -> dict[date, bool]:
    """解析模板文件的 ``night_session_exceptions``: [{"eve": "YYYY-MM-DD", "has_night": bool, "source": ...}]."""
    exceptions: dict[date, bool] = {}
    for row in rows or ():
        eve = date.fromisoformat(str(row["eve"]))
        exceptions[eve] = bool(row["has_night"])
    return exceptions
