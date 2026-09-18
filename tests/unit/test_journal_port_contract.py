"""Shared JournalPort contract: SQLiteJournal and MemoryJournal must behave identically (S2-11, FR-REC-01)."""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from qh_trader.core.constants import DuplicateFactError, JournalConflictError
from qh_trader.core.event import JournalTransaction
from qh_trader.core.objects import ControlEpoch, ControlRecord
from qh_trader.core.ports import JournalPort
from qh_trader.infrastructure.journal import SQLiteJournal
from qh_trader.infrastructure.memory_journal import MemoryJournal
from tests.unit.test_journal import ACCOUNT, NOW, trade_event, transaction


@pytest.fixture(params=["sqlite", "memory"])
def journal(request, tmp_path):
    if request.param == "memory":
        yield MemoryJournal(account_id=ACCOUNT)
        return
    with SQLiteJournal(tmp_path / "trading.db", account_id=ACCOUNT) as instance:
        instance.migrate()
        yield instance


def test_adapter_satisfies_port_protocol(journal):
    assert isinstance(journal, JournalPort)
    assert journal.head_seq == journal.cursor == 0
    assert journal.load_control_record() is None
    assert journal.load_snapshot() is None
    checkpoint = journal.load_checkpoint()
    assert checkpoint.journal_seq == checkpoint.cursor == 0 and checkpoint.state == {}


def test_append_is_atomic_and_idempotent_only_for_identical_contents(journal, sample_instrument):
    first = transaction(trade_event(1, sample_instrument))
    assert journal.append(first) == 1
    second = transaction(trade_event(2, sample_instrument), 1, 2, state_updates={"balance": Decimal(200)})
    assert journal.append(second) == 2
    assert journal.append(first) == 1
    assert journal.cursor == 2 and journal.load_state()["balance"] == Decimal(200)
    with pytest.raises(JournalConflictError, match="different contents"):
        journal.append(replace(first, state_updates={"balance": Decimal(999)}))
    assert journal.head_seq == 2
    with pytest.raises(TypeError):
        journal.load_state()["balance"] = 0


def test_scoped_trade_duplicates_reject_the_whole_new_transaction(journal, sample_instrument):
    journal.append(transaction(trade_event(1, sample_instrument, trade_id="same")))
    duplicate = transaction(
        trade_event(2, sample_instrument, trade_id="same"), 1, 2, state_updates={"balance": Decimal(999)}
    )
    with pytest.raises(DuplicateFactError):
        journal.append(duplicate)
    assert journal.head_seq == journal.cursor == 1
    assert journal.load_state()["balance"] == Decimal("100.123456")
    assert journal.contains_trade(duplicate.deduplication_keys[0])
    assert len(list(journal.replay_from(0))) == 1
    new_day = transaction(trade_event(2, sample_instrument, trade_id="same", trading_day=date(2024, 9, 10)), 1, 2)
    assert journal.append(new_day) == 2


def test_cursor_account_order_and_dedup_guards(journal, sample_instrument):
    event = trade_event(1, sample_instrument)
    with pytest.raises(JournalConflictError, match="cursor"):
        journal.append(transaction(event, before=1, after=2))
    with pytest.raises(JournalConflictError, match="cross-account"):
        journal.append(transaction(trade_event(1, sample_instrument, account="other-account")))
    with pytest.raises(JournalConflictError, match="deduplication"):
        journal.append(transaction(event, deduplication_keys=()))
    with pytest.raises(JournalConflictError, match="reserved"):
        journal.append(transaction(event, state_updates={"cursor": 99}))
    with pytest.raises(JournalConflictError, match="repeats"):
        journal.append(transaction(event, deduplication_keys=(event.payload.deduplication_key,) * 2))
    with pytest.raises(JournalConflictError, match="cursor advancement"):
        journal.append(JournalTransaction(transaction_id="empty", events=(), cursor_before=0, cursor_after=1))
    with pytest.raises(TypeError):
        journal.append("not a transaction")
    assert journal.head_seq == 0
    journal.append(transaction(event))
    with pytest.raises(JournalConflictError, match="ingress"):
        journal.append(transaction(trade_event(1, sample_instrument, trade_id="again"), 1, 2, transaction_id="dup"))
    stale_id = replace(trade_event(5, sample_instrument), event_id=event.event_id)
    with pytest.raises(JournalConflictError, match="identity"):
        journal.append(transaction(stale_id, 1, 2, transaction_id="dup-id"))
    assert journal.head_seq == 1


def test_replay_iterator_does_not_include_later_commits(journal, sample_instrument):
    first = trade_event(1, sample_instrument)
    journal.append(transaction(first))
    replay = journal.replay_from(0)
    journal.append(transaction(trade_event(2, sample_instrument), 1, 2))
    assert list(replay) == [first]
    with pytest.raises(JournalConflictError, match="beyond"):
        journal.replay_from(3)


def test_snapshots_and_replay_preserve_history_without_rewinding_control(journal, sample_instrument):
    first_control = ControlRecord(ControlEpoch("controller-a", 1), NOW, 1)
    journal.append(
        JournalTransaction(
            transaction_id="control-1",
            events=(),
            cursor_before=0,
            cursor_after=0,
            state_updates={"balance": Decimal(100)},
            control_record=first_control,
        )
    )
    first = trade_event(1, sample_instrument)
    journal.append(transaction(first, state_updates={"balance": Decimal(200)}))
    journal.snapshot(1)
    second_control = ControlRecord(ControlEpoch("controller-b", 2), NOW, 3)
    journal.append(
        JournalTransaction(
            transaction_id="control-2", events=(), cursor_before=1, cursor_after=1, control_record=second_control
        )
    )
    snapshot = journal.load_snapshot(1)
    assert snapshot.journal_seq == 1 and snapshot.cursor == 0 and snapshot.state["balance"] == Decimal(100)
    assert snapshot.control_record == first_control
    with pytest.raises(TypeError):
        snapshot.state["balance"] = 0
    # load_snapshot returns the latest snapshot at or before the requested sequence.
    assert journal.load_snapshot(2) == snapshot and journal.load_snapshot() == snapshot
    assert journal.load_snapshot(0) is None
    with pytest.raises(JournalConflictError, match="beyond"):
        journal.load_snapshot(99)
    with pytest.raises(JournalConflictError, match="beyond"):
        journal.snapshot(99)
    journal.snapshot(1)  # identical re-snapshot is accepted
    journal.snapshot(2)
    assert journal.load_snapshot(2).state["balance"] == Decimal(200)
    assert journal.load_control_record() == second_control
    assert list(journal.replay_from(snapshot.journal_seq)) == [first]
    assert journal.load_state()["balance"] == Decimal(200)
    with pytest.raises(JournalConflictError, match="epoch"):
        journal.append(
            JournalTransaction(
                transaction_id="stale-control",
                events=(),
                cursor_before=1,
                cursor_after=1,
                control_record=replace(first_control, journal_seq=4),
            )
        )
    with pytest.raises(JournalConflictError, match="bind the new journal sequence"):
        journal.append(
            JournalTransaction(
                transaction_id="unbound-control",
                events=(),
                cursor_before=1,
                cursor_after=1,
                control_record=ControlRecord(ControlEpoch("controller-c", 3), NOW, 2),
            )
        )
    second = replace(trade_event(2, sample_instrument, event_time=NOW), source_id="old-controller-a")
    journal.append(transaction(second, 1, 2))
    assert journal.load_control_record() == second_control
    assert list(journal.replay_from(1)) == [first, second]
    checkpoint = journal.load_checkpoint()
    assert checkpoint.journal_seq == 4 and checkpoint.cursor == 2
    expected_keys = {first.payload.deduplication_key, second.payload.deduplication_key}
    assert checkpoint.deduplication_keys == frozenset(expected_keys)
    assert checkpoint.control_record == second_control


def test_export_yields_every_canonical_event_in_commit_order(journal, sample_instrument):
    events = [trade_event(index, sample_instrument) for index in range(1, 5)]
    journal.append(transaction(events[0], 0, 1, transaction_id="a"))
    journal.append(
        transaction(
            events[1],
            1,
            3,
            transaction_id="b",
            events=(events[1], events[2]),
            deduplication_keys=(events[1].payload.deduplication_key, events[2].payload.deduplication_key),
        )
    )
    journal.append(transaction(events[3], 3, 4, transaction_id="c"))
    assert list(journal.replay_from(0)) == events
    if isinstance(journal, MemoryJournal):
        assert journal.export_events() == tuple(events)
