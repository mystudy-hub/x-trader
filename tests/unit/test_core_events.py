"""Deterministic ordering and immutable transactions, independent of market ticks."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from itertools import permutations

import pytest

from qh_trader.core.clock import VirtualClock, utc_timestamp
from qh_trader.core.constants import EventKind, Exchange, ReplayOrder
from qh_trader.core.event import CanonicalEvent, EventQueue, JournalTransaction, TimerEvent
from qh_trader.core.objects import TradeKey

NOW = datetime(2024, 9, 10, 1, tzinfo=timezone.utc)


def event(sequence, kind=EventKind.MARKET_DATA, **changes):
    values = dict(
        event_id=f"event-{sequence}",
        kind=kind,
        event_time=NOW,
        available_at=NOW,
        sequence=sequence,
        source_id="test-only",
        payload={"value": sequence},
    )
    return CanonicalEvent(**(values | changes))


def test_virtual_clock_fires_timers_without_market_ticks_and_keeps_fifo():
    clock = VirtualClock[TimerEvent](NOW)
    first = TimerEvent("one", {"nested": [1]})
    second = TimerEvent("two", {"nested": [2]})
    clock.schedule(NOW + timedelta(seconds=1), first)
    clock.schedule(NOW + timedelta(seconds=1), second)
    assert clock.now() == NOW
    assert clock.next_time() == NOW + timedelta(seconds=1)
    assert clock.next_event() == first
    assert clock.now() == NOW + timedelta(seconds=1)
    assert clock.next_event() == second
    assert clock.next_event() is None
    assert clock.next_time() is None


def test_virtual_clock_rejects_backward_time_and_skipped_timers():
    clock = VirtualClock(NOW)
    with pytest.raises(ValueError, match="past"):
        clock.schedule(NOW - timedelta(seconds=1), TimerEvent("past"))
    with pytest.raises(ValueError, match="backwards"):
        clock.advance_to(NOW - timedelta(seconds=1))
    clock.schedule(NOW + timedelta(seconds=1), TimerEvent("due"))
    with pytest.raises(ValueError, match="consume scheduled"):
        clock.advance_to(NOW + timedelta(seconds=2))
    clock.advance_to(NOW + timedelta(seconds=1))
    assert clock.next_event().timer_id == "due"
    clock.advance_to(NOW + timedelta(seconds=2))
    with pytest.raises(TypeError):
        clock.schedule(clock.now(), None)


def test_clock_normalizes_offset_but_rejects_naive_timestamp():
    offset = timezone(timedelta(hours=8))
    assert utc_timestamp(NOW.astimezone(offset)) == NOW
    with pytest.raises(ValueError, match="timezone-aware"):
        VirtualClock(NOW.replace(tzinfo=None))


def test_simulation_order_uses_visibility_then_declared_priority_then_sequence():
    priorities = {EventKind.MARKET_DATA: 0, EventKind.ORDER_ARRIVAL: 1, EventKind.CANCEL_ARRIVAL: 2}
    events = [
        event(9, EventKind.CANCEL_ARRIVAL, event_time=NOW - timedelta(minutes=1)),
        event(8, EventKind.ORDER_ARRIVAL),
        event(7, EventKind.ORDER_ARRIVAL),
    ]
    for insertion_order in permutations(events):
        queue = EventQueue(ReplayOrder.SIMULATED, priorities)
        for record in insertion_order:
            queue.push(record)
        queue.push(event(1, available_at=NOW + timedelta(seconds=1)))
        assert [queue.pop().sequence for _ in range(4)] == [7, 8, 9, 1]
        assert queue.pop() is None


def test_simulation_policy_is_required_and_cannot_change_after_queue_construction():
    with pytest.raises(ValueError, match="explicit priority"):
        EventQueue(ReplayOrder.SIMULATED)
    priorities = {EventKind.MARKET_DATA: 0}
    queue = EventQueue(ReplayOrder.SIMULATED, priorities)
    priorities[EventKind.MARKET_DATA] = 99
    assert queue.priorities[EventKind.MARKET_DATA] == 0
    with pytest.raises(TypeError):
        queue.priorities[EventKind.MARKET_DATA] = 99
    with pytest.raises(AttributeError):
        queue.mode = ReplayOrder.RECORDED
    with pytest.raises(ValueError, match="missing"):
        queue.push(event(1, EventKind.CANCEL_ARRIVAL))


def test_recorded_replay_preserves_ingress_sequence_despite_timestamp_disorder():
    queue = EventQueue(ReplayOrder.RECORDED)
    queue.push(event(6, event_time=NOW - timedelta(hours=1), available_at=NOW - timedelta(seconds=1)))
    queue.push(event(5))
    assert [queue.pop().sequence, queue.pop().sequence] == [5, 6]
    with pytest.raises(ValueError, match="already consumed"):
        queue.push(event(4))
    with pytest.raises(ValueError, match="without a simulation"):
        EventQueue(ReplayOrder.RECORDED, {EventKind.MARKET_DATA: 0})


def test_simulation_rejects_reused_ingress_sequences_and_history_reordering():
    queue = EventQueue(ReplayOrder.SIMULATED, {EventKind.MARKET_DATA: 0})
    record = event(10)
    queue.push(record)
    with pytest.raises(ValueError, match="duplicate"):
        queue.push(replace(record, event_id="another"))
    assert len(queue) == 1
    queue.pop()
    with pytest.raises(ValueError, match="duplicate"):
        queue.push(replace(record, available_at=NOW + timedelta(seconds=1)))
    with pytest.raises(ValueError, match="already consumed"):
        queue.push(event(11, available_at=NOW - timedelta(seconds=1)))


def test_event_payload_cannot_mutate_after_enqueue():
    raw = {"prices": [1, 2]}
    record = event(1, payload=raw)
    raw["prices"].append(3)
    assert record.payload["prices"] == (1, 2)
    with pytest.raises(TypeError):
        event(2, EventKind.TRADE_REPORT, payload=raw)
    with pytest.raises(TypeError):
        event(3, EventKind.TIMER, payload=raw)


def test_journal_transaction_snapshots_state_and_requires_scoped_dedup_keys():
    source = {"reservations": {"local-1": [2, 1]}}
    key = TradeKey("account-1", Exchange.SHFE, date(2024, 9, 10), "123")
    tx = JournalTransaction(
        transaction_id="tx-1",
        events=(event(1),),
        cursor_before=0,
        cursor_after=1,
        state_updates=source,
        deduplication_keys=(key,),
    )
    source["reservations"]["local-1"][0] = 99
    assert tx.state_updates["reservations"]["local-1"] == (2, 1)
    with pytest.raises(TypeError):
        tx.state_updates["cursor"] = 42
    with pytest.raises(TypeError, match="scoped trade"):
        replace(tx, deduplication_keys=("123",))
    with pytest.raises(ValueError):
        replace(tx, cursor_after=0, cursor_before=1)
