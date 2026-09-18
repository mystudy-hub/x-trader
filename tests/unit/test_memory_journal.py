"""Unit tests for in-memory JournalPort adapter (S2-11, FR-REC-01)."""

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from qh_trader.core.constants import DuplicateFactError, EventKind, Exchange, JournalConflictError
from qh_trader.core.event import CanonicalEvent, JournalTransaction, TimerEvent
from qh_trader.core.objects import TradeKey
from qh_trader.infrastructure.memory_journal import MemoryJournal


def make_event(sequence: int = 1) -> CanonicalEvent:
    now = datetime.now(timezone.utc)
    timer = TimerEvent(timer_id=f"t-{sequence}", payload={"data": 123})
    return CanonicalEvent(
        event_id=f"event-{sequence}",
        kind=EventKind.TIMER,
        event_time=now,
        available_at=now,
        sequence=sequence,
        source_id="test-source",
        payload=timer,
    )


def test_memory_journal_atomic_append_and_idempotency():
    journal = MemoryJournal(account_id="acc-test")

    tx1 = JournalTransaction(
        transaction_id="tx-1",
        events=(make_event(),),
        cursor_before=0,
        cursor_after=1,
    )
    seq1 = journal.append(tx1)
    assert seq1 == 1

    # 幂等重试
    assert journal.append(tx1) == 1


def test_memory_journal_duplicate_trade_rejected():
    journal = MemoryJournal(account_id="acc-test")
    tk = TradeKey("acc-test", Exchange.SHFE, date(2024, 9, 9), "T1")

    tx1 = JournalTransaction(
        transaction_id="tx-1",
        events=(make_event(),),
        cursor_before=0,
        cursor_after=1,
        deduplication_keys=(tk,),
    )
    journal.append(tx1)
    assert journal.contains_trade(tk) is True

    # 新事务包含相同成交键 -> 拒绝
    tx2 = JournalTransaction(
        transaction_id="tx-2",
        events=(make_event(2),),
        cursor_before=1,
        cursor_after=2,
        deduplication_keys=(tk,),
    )
    with pytest.raises(DuplicateFactError):
        journal.append(tx2)


def test_memory_journal_cursor_conflict_rejected():
    journal = MemoryJournal(account_id="acc-test")

    tx1 = JournalTransaction(
        transaction_id="tx-1",
        events=(make_event(),),
        cursor_before=0,
        cursor_after=5,
    )
    journal.append(tx1)

    # 下一事务声明前置游标是 4 (不匹配当前游标 5)
    tx2 = JournalTransaction(
        transaction_id="tx-2",
        events=(make_event(2),),
        cursor_before=4,
        cursor_after=6,
    )
    with pytest.raises(JournalConflictError):
        journal.append(tx2)


def test_memory_journal_replay_and_checkpoint():
    journal = MemoryJournal(account_id="acc-test")

    tx1 = JournalTransaction(
        transaction_id="tx-1",
        events=(make_event(1), make_event(2)),
        cursor_before=0,
        cursor_after=2,
    )
    tx2 = JournalTransaction(
        transaction_id="tx-2",
        events=(make_event(3),),
        cursor_before=2,
        cursor_after=3,
    )
    journal.append(tx1)
    journal.append(tx2)

    # 从 seq=1 回放 (只包含 seq=2 的事件)
    events = list(journal.replay_from(1))
    assert len(events) == 1
    assert events[0].sequence == 3

    # 检查点
    cp = journal.load_checkpoint()
    assert cp.journal_seq == 2
    assert cp.cursor == 3

    # 回测结束导出规范事件: 与 replay_from(0) 一致且保持提交顺序
    exported = journal.export_events()
    assert exported == tuple(tx1.events) + tuple(tx2.events)
    assert list(journal.replay_from(0)) == list(exported)
    assert [event.sequence for event in exported] == [1, 2, 3]


def test_memory_journal_append_never_auto_snapshots_and_rejects_reused_id_with_new_contents():
    journal = MemoryJournal(account_id="acc-test")
    tx1 = JournalTransaction(transaction_id="tx-1", events=(make_event(1),), cursor_before=0, cursor_after=1)
    journal.append(tx1)
    assert journal.load_snapshot() is None
    with pytest.raises(JournalConflictError, match="different contents"):
        journal.append(replace(tx1, state_updates={"balance": 1}))
    with pytest.raises(JournalConflictError, match="ingress"):
        stale = JournalTransaction(transaction_id="tx-2", events=(make_event(1),), cursor_before=1, cursor_after=2)
        journal.append(stale)
