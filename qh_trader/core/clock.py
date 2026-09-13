"""[Core 层] UTC 时间规范化与不依赖行情推进的虚拟时钟。"""

from __future__ import annotations

import heapq
from datetime import datetime, timezone
from itertools import count
from typing import Generic, TypeVar

Timestamp = datetime
T = TypeVar("T")


def utc_timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


class VirtualClock(Generic[T]):
    """由调用方显式消费定时事件；同一时刻保持调度顺序。"""

    def __init__(self, start: datetime):
        self._now = utc_timestamp(start)
        self._sequence = count()
        self._scheduled: list[tuple[datetime, int, T]] = []

    def now(self) -> datetime:
        return self._now

    def schedule(self, at: datetime, event: T) -> None:
        at = utc_timestamp(at)
        if event is None:
            raise TypeError("scheduled event cannot be None; None denotes an empty queue")
        if at < self._now:
            raise ValueError("cannot schedule an event in the past")
        heapq.heappush(self._scheduled, (at, next(self._sequence), event))

    def advance_to(self, at: datetime) -> None:
        at = utc_timestamp(at)
        if at < self._now:
            raise ValueError("virtual time cannot move backwards")
        if self._scheduled and self._scheduled[0][0] < at:
            raise ValueError("consume scheduled events before advancing past them")
        self._now = at

    def next_event(self) -> T | None:
        if not self._scheduled:
            return None
        at, _, event = heapq.heappop(self._scheduled)
        self._now = at
        return event

    def next_time(self) -> datetime | None:
        return self._scheduled[0][0] if self._scheduled else None
