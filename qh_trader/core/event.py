"""[Core 层] 规范事件与显式排序策略；不负责撮合、记账或回报去重。"""

from __future__ import annotations

import heapq
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Generic, TypeAlias, TypeVar

from .clock import utc_timestamp
from .constants import EventKind, ReplayOrder
from .objects import (
    ControlRecord,
    OrderUpdate,
    Trade,
    TradeKey,
    freeze_payload,
    normalize_times,
    require_enum,
    require_int,
    require_text,
)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class TimerEvent:
    timer_id: str
    payload: object = None

    def __post_init__(self) -> None:
        require_text(self.timer_id, "timer_id")
        object.__setattr__(self, "payload", freeze_payload(self.payload))


@dataclass(frozen=True, kw_only=True)
class CanonicalEvent(Generic[T]):
    event_id: str
    kind: EventKind
    event_time: datetime
    available_at: datetime
    sequence: int
    source_id: str
    payload: T

    def __post_init__(self) -> None:
        require_text(self.event_id, "event_id")
        require_text(self.source_id, "source_id")
        require_enum(self.kind, EventKind)
        require_int(self.sequence, "sequence")
        normalize_times(self, "event_time", "available_at")
        expected = {EventKind.ORDER_REPORT: OrderUpdate, EventKind.TRADE_REPORT: Trade, EventKind.TIMER: TimerEvent}
        if self.kind in expected and not isinstance(self.payload, expected[self.kind]):
            raise TypeError("event kind does not match its normalized payload")
        if isinstance(self.payload, (OrderUpdate, Trade)) and (
            self.event_time != self.payload.event_time or self.available_at != self.payload.available_at
        ):
            raise ValueError("event envelope and normalized report timestamps must agree")
        object.__setattr__(self, "payload", freeze_payload(self.payload))


OrderEvent: TypeAlias = CanonicalEvent[OrderUpdate]
TradeEvent: TypeAlias = CanonicalEvent[Trade]


class EventQueue:
    """模拟事件按可见时刻/显式优先级/序号排序；实盘回放只按实际接收序号排序。"""

    def __init__(self, mode: ReplayOrder, priorities: Mapping[EventKind, int] | None = None):
        require_enum(mode, ReplayOrder)
        if mode == ReplayOrder.SIMULATED and priorities is None:
            raise ValueError("simulated ordering requires an explicit priority policy")
        if mode == ReplayOrder.RECORDED and priorities is not None:
            raise ValueError("recorded replay uses ingress sequence without a simulation priority policy")
        self._mode = mode
        values = dict(priorities or {})
        for kind, rank in values.items():
            require_enum(kind, EventKind)
            if isinstance(rank, bool) or not isinstance(rank, int):
                raise TypeError("event priorities must be integers")
        self._priorities = MappingProxyType(values)
        self._queue: list[tuple[tuple, CanonicalEvent]] = []
        self._seen_sequences: set[int] = set()
        self._last_key: tuple | None = None

    @property
    def mode(self) -> ReplayOrder:
        return self._mode

    @property
    def priorities(self) -> Mapping[EventKind, int]:
        return self._priorities

    def push(self, event: CanonicalEvent) -> None:
        if not isinstance(event, CanonicalEvent):
            raise TypeError("only normalized events may enter the queue")
        if event.sequence in self._seen_sequences:
            raise ValueError("duplicate ingress sequence")
        key: tuple[datetime, int, int] | tuple[int]
        if self.mode == ReplayOrder.SIMULATED:
            if event.kind not in self.priorities:
                raise ValueError("event kind is missing from the recorded priority policy")
            key = (utc_timestamp(event.available_at), self.priorities[event.kind], event.sequence)
        else:
            key = (event.sequence,)
        if self._last_key is not None and key <= self._last_key:
            raise ValueError("event would reorder an already consumed history")
        heapq.heappush(self._queue, (key, event))
        self._seen_sequences.add(event.sequence)

    def pop(self) -> CanonicalEvent | None:
        if not self._queue:
            return None
        self._last_key, event = heapq.heappop(self._queue)
        return event

    def __len__(self) -> int:
        return len(self._queue)


@dataclass(frozen=True, slots=True, kw_only=True)
class JournalTransaction:
    transaction_id: str
    events: tuple[CanonicalEvent, ...]
    cursor_before: int
    cursor_after: int
    state_updates: Mapping[str, object] = field(default_factory=dict)
    deduplication_keys: tuple[TradeKey, ...] = ()
    control_record: ControlRecord | None = None

    def __post_init__(self) -> None:
        require_text(self.transaction_id, "transaction_id")
        require_int(self.cursor_before, "cursor_before")
        require_int(self.cursor_after, "cursor_after", self.cursor_before)
        events = tuple(self.events)
        keys = tuple(self.deduplication_keys)
        if any(not isinstance(event, CanonicalEvent) for event in events):
            raise TypeError("journal events must already be normalized")
        if any(not isinstance(key, TradeKey) for key in keys):
            raise TypeError("journal deduplication uses scoped trade identities")
        if self.control_record is not None and not isinstance(self.control_record, ControlRecord):
            raise TypeError("journal control record must be normalized")
        if not isinstance(self.state_updates, Mapping):
            raise TypeError("journal state updates must be a named mapping")
        object.__setattr__(self, "events", events)
        object.__setattr__(self, "deduplication_keys", keys)
        object.__setattr__(self, "state_updates", freeze_payload(self.state_updates))


@dataclass(frozen=True, slots=True)
class JournalSnapshot:
    account_id: str
    journal_seq: int
    cursor: int
    state: Mapping[str, object]
    deduplication_keys: frozenset[TradeKey]
    control_record: ControlRecord | None

    def __post_init__(self) -> None:
        require_text(self.account_id, "account_id")
        require_int(self.journal_seq, "journal_seq")
        require_int(self.cursor, "cursor")
        if not isinstance(self.state, Mapping):
            raise TypeError("journal snapshot state must be a named mapping")
        keys = frozenset(self.deduplication_keys)
        if any(not isinstance(key, TradeKey) or key.account_id != self.account_id for key in keys):
            raise ValueError("snapshot trade identities must match the account")
        if self.control_record is not None and (
            not isinstance(self.control_record, ControlRecord) or self.control_record.journal_seq > self.journal_seq
        ):
            raise ValueError("snapshot control record cannot be newer than its journal sequence")
        object.__setattr__(self, "state", freeze_payload(self.state))
        object.__setattr__(self, "deduplication_keys", keys)
