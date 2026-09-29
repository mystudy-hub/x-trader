"""[Data 适配器] 按显式会话聚合可信闭合 Bar (S5-05, FR-DATA-02, FR-CAL-03)。

这是行情快照聚合器，不恢复丢失逐笔成交，也不填充无行情区间。首个中途接入的桶、
断线受损桶均丢弃。调用方必须把断线或队列丢包交给 ``mark_gap``，并定时 ``advance``。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from qh_trader.core.clock import utc_timestamp
from qh_trader.core.constants import MarketPhase, QualityFlag
from qh_trader.core.objects import Bar, InstrumentId, RecordMeta, Session, Tick


class LiveBarDataError(ValueError):
    """行情或时钟不可信；当前未闭合桶已作废，调用方应记录质量缺口。"""


@dataclass(slots=True)
class _Bucket:
    session: Session
    start: datetime
    end: datetime
    first: Tick
    last: Tick
    high: Decimal
    low: Decimal
    volume: int
    turnover: Decimal
    complete: bool
    count: int = 1


class LiveBarAggregator:
    """每个实际合约一个实例，时间由调用方注入，默认生成会话对齐的 1h Bar。

    会话与桶均为左闭右开；每个连续竞价会话独立分桶，尾桶可短于 ``interval``。
    ``max_gap`` 是允许的快照间隔，不是自动补数权限；不活跃合约也保守丢弃超时桶。
    首条累计量只建立基线；跨休市的累计差不归入下一会话。中途接入须等下一个完整桶。
    """

    def __init__(
        self,
        *,
        instrument: InstrumentId,
        sessions: Sequence[Session],
        interval: timedelta = timedelta(hours=1),
        max_tick_age: timedelta = timedelta(seconds=5),
        max_gap: timedelta = timedelta(seconds=10),
    ) -> None:
        if not isinstance(instrument, InstrumentId):
            raise TypeError("live bars require an actual instrument")
        for name, value in (("interval", interval), ("max_tick_age", max_tick_age), ("max_gap", max_gap)):
            if not isinstance(value, timedelta) or value <= timedelta(0):
                raise ValueError(f"{name} must be a positive timedelta")
        supplied = tuple(sessions)
        if not supplied or any(not isinstance(row, Session) or row.instrument != instrument for row in supplied):
            raise ValueError("explicit sessions for the actual instrument are required")
        values = tuple(sorted(supplied, key=lambda row: row.start))
        if any(left.end > right.start for left, right in zip(values, values[1:], strict=False)):
            raise ValueError("sessions must not overlap")
        self.instrument = instrument
        self.sessions = values
        self.interval = interval
        self.max_tick_age = max_tick_age
        self.max_gap = max_gap
        self._bucket: _Bucket | None = None
        self._last_tick: Tick | None = None
        self._last_session: Session | None = None
        self._last_event: datetime | None = None
        self._last_now: datetime | None = None
        self._closed_through: datetime | None = None
        self._sequence = 0
        self.discarded_bars = 0
        self.quality_gaps = 0

    def mark_gap(self) -> None:
        """断线、队列丢包或质量失败后切断累计量基线，未完成桶不可恢复。"""
        if self._bucket is not None:
            self.discarded_bars += 1
        self.quality_gaps += 1
        self._bucket = None
        self._last_tick = None
        self._last_session = None

    def _reject(self, reason: str) -> None:
        self.mark_gap()
        raise LiveBarDataError(reason)

    def _clock(self, now: datetime) -> datetime:
        at = utc_timestamp(now)
        if self._last_now is not None and at < self._last_now:
            self._reject("observation clock moved backwards")
        self._last_now = at
        return at

    def on_tick(self, tick: Tick, *, now: datetime) -> tuple[Bar, ...]:
        """接收已归一化 Tick，返回由本条可信行情证明已闭合的 Bar。"""
        at = self._clock(now)
        if not isinstance(tick, Tick) or tick.instrument != self.instrument:
            self._reject("tick instrument does not match aggregator")
        event = tick.meta.event_time
        if tick.meta.quality_flags != QualityFlag.OK or tick.last_price is None:
            self._reject("tick price or quality is not usable")
        if event > at or at - event > self.max_tick_age:
            self._reject("tick is stale or future-dated")
        if tick.meta.available_at < event or max(tick.meta.available_at, tick.meta.ingested_at) > at:
            self._reject("tick availability is inconsistent with observation time")
        if self._last_event is not None and event <= self._last_event:
            self._reject("duplicate or out-of-order tick")
        if self._closed_through is not None and event < self._closed_through:
            self._reject("tick arrived after its bar was closed")
        session = next((row for row in self.sessions if row.contains(event)), None)
        if session is None or session.available_at > at:
            self._reject("no visible registered session covers tick")
        if session.phase != MarketPhase.CONTINUOUS or not session.permissions.match:
            self._reject("tick is outside a continuous matching session")
        if tick.meta.trading_day != session.trading_day:
            self._reject("tick trading day does not match registered session")
        if tick.meta.session_id is not None and tick.meta.session_id != session.session_id:
            self._reject("tick session does not match registered session")
        if tick.phase not in (MarketPhase.UNKNOWN, session.phase):
            self._reject("tick phase does not match registered session")

        previous = self._last_tick
        same_session = previous is not None and self._last_session == session
        if previous is not None and previous.meta.trading_day == tick.meta.trading_day:
            if (
                tick.cumulative_volume < previous.cumulative_volume
                or tick.cumulative_turnover < previous.cumulative_turnover
            ):
                self._reject("cumulative counters moved backwards within trading day")
        if same_session and event - previous.meta.event_time > self.max_gap:
            self._reject("market data gap exceeds configured maximum")

        closed = self._finish(at) if self._bucket is not None and event >= self._bucket.end else ()
        start = session.start + ((event - session.start) // self.interval) * self.interval
        end = min(start + self.interval, session.end)
        volume = tick.cumulative_volume - previous.cumulative_volume if same_session else 0
        turnover = tick.cumulative_turnover - previous.cumulative_turnover if same_session else Decimal(0)
        if self._bucket is None:
            # 精确边界首条可建立桶内基线；中途首条无法证明已观察整个桶。
            complete = event == start or bool(same_session and previous.meta.event_time < start)
            self._bucket = _Bucket(
                session, start, end, tick, tick, tick.last_price, tick.last_price, volume, turnover, complete
            )
        else:
            bucket = self._bucket
            bucket.last = tick
            bucket.high = max(bucket.high, tick.last_price)
            bucket.low = min(bucket.low, tick.last_price)
            bucket.volume += volume
            bucket.turnover += turnover
            bucket.count += 1
        self._last_tick = tick
        self._last_session = session
        self._last_event = event
        return closed

    def advance(self, now: datetime) -> tuple[Bar, ...]:
        """以可信实时时钟闭合尾桶；不制造空桶，也不发布早已过期的信号。"""
        at = self._clock(now)
        bucket = self._bucket
        if bucket is None:
            return ()
        if at >= bucket.end:
            return self._finish(at)
        if at - bucket.last.meta.event_time > self.max_gap:
            self.mark_gap()
        return ()

    def _finish(self, at: datetime) -> tuple[Bar, ...]:
        bucket = self._bucket
        if bucket is None:
            return ()
        self._bucket = None
        self._closed_through = bucket.end
        missing_tail = bucket.end - bucket.last.meta.event_time > self.max_gap
        late_close = at - bucket.end > self.max_tick_age
        if missing_tail or late_close:
            self.quality_gaps += 1
        if not bucket.complete or bucket.count < 2 or missing_tail or late_close:
            self.discarded_bars += 1
            return ()
        self._sequence += 1
        last = bucket.last
        seconds = self.interval.total_seconds()
        if seconds % 3600 == 0:
            interval = f"{int(seconds // 3600)}h"
        elif seconds % 60 == 0:
            interval = f"{int(seconds // 60)}m"
        else:
            interval = f"{seconds:g}s"
        return (
            Bar(
                instrument=self.instrument,
                meta=RecordMeta(
                    event_time=bucket.end,
                    available_at=at,
                    ingested_at=at,
                    receive_time=at,
                    trading_day=bucket.session.trading_day,
                    source_id=f"live-bars:{last.meta.source_id}",
                    source_version=f"1:{last.meta.source_version}:{bucket.session.rule_version}",
                    ingest_seq=self._sequence,
                    session_id=bucket.session.session_id,
                ),
                bar_start=bucket.start,
                bar_end=bucket.end,
                interval=interval,
                open=bucket.first.last_price,
                high=bucket.high,
                low=bucket.low,
                close=last.last_price,
                volume=bucket.volume,
                turnover=bucket.turnover,
                open_interest=last.open_interest,
                open_time=bucket.first.meta.event_time,
                includes_auction=False,
            ),
        )
