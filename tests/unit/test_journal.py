"""Real SQLite rollback/restart tests for atomic journal state and immutable replay."""

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from qh_trader.core.constants import (
    DuplicateFactError,
    EventKind,
    Exchange,
    JournalConflictError,
    JournalCorruptionError,
    Offset,
    Side,
)
from qh_trader.core.event import CanonicalEvent, JournalTransaction, TimerEvent
from qh_trader.core.objects import ControlEpoch, ControlRecord, Trade, TradeKey
from qh_trader.core.ports import JournalPort
from qh_trader.infrastructure import journal_codec
from qh_trader.infrastructure.journal import SQLiteJournal
from qh_trader.infrastructure.rule_store import RuleStore

ACCOUNT = "journal-test-account"
NOW = datetime(2024, 9, 9, 1, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]


def trade_event(sequence, instrument, *, trade_id=None, trading_day=None, account=ACCOUNT, event_time=None):
    day = trading_day or date(2024, 9, 9)
    key = TradeKey(account, Exchange.SHFE, day, trade_id or f"trade-{sequence}")
    timestamp = event_time or NOW + timedelta(seconds=sequence)
    trade = Trade(
        account_id=account,
        instrument=instrument,
        trading_day=day,
        trade_id=key.trade_id,
        side=Side.BUY,
        offset=Offset.OPEN,
        quantity=1,
        price=Decimal("100.125"),
        event_time=timestamp,
        available_at=timestamp,
        deduplication_key=key,
    )
    return CanonicalEvent(
        event_id=f"event-{sequence}",
        kind=EventKind.TRADE_REPORT,
        event_time=timestamp,
        available_at=timestamp,
        sequence=sequence,
        source_id="synthetic-gateway",
        payload=trade,
    )


def transaction(event, before=0, after=1, **changes):
    values = dict(
        transaction_id=f"tx-{event.sequence}",
        events=(event,),
        cursor_before=before,
        cursor_after=after,
        state_updates={"balance": Decimal("100.123456"), "reservations": {"order-1": 2}},
        deduplication_keys=(event.payload.deduplication_key,),
    )
    return JournalTransaction(**(values | changes))


@pytest.fixture
def journal(tmp_path):
    with SQLiteJournal(tmp_path / "trading.db", account_id=ACCOUNT) as instance:
        instance.migrate()
        yield instance


def test_journal_protocol_and_required_file_pragmas(journal):
    assert isinstance(journal, JournalPort)
    assert journal.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert journal.connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert journal.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert journal.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert journal.head_seq == journal.cursor == 0


def test_codec_preserves_normalized_core_values_and_canonical_mapping_order(sample_instrument, sample_bars):
    value = {
        "trade": trade_event(1, sample_instrument),
        "bar": sample_bars[0],
        "money": Decimal("1.2500"),
        "timer": TimerEvent("timeout", {"at": NOW}),
        "float": -0.0,
        "day": date(2024, 9, 9),
        "keys": frozenset({TradeKey(ACCOUNT, Exchange.SHFE, date(2024, 9, 9), "one")}),
    }
    assert journal_codec.loads(journal_codec.dumps(value)) == value
    assert journal_codec.dumps({"b": 2, "a": 1}) == journal_codec.dumps({"a": 1, "b": 2})
    encoded = json.loads(journal_codec.dumps(TimerEvent("a")))
    encoded["value"]["name"] = "untrusted.module.Class"
    with pytest.raises(ValueError, match="unknown journal contract"):
        journal_codec.loads(json.dumps(encoded))


@pytest.mark.parametrize(
    "value",
    [
        {"nested": {"AuthCode": "do-not-store"}},
        {"password": "do-not-store"},
        {"terminal_payload": b"do-not-store"},
        "AuthCode=do-not-store",
        b"binary",
    ],
)
def test_credentials_and_raw_terminal_values_are_rejected(value):
    with pytest.raises((TypeError, ValueError)) as failure:
        journal_codec.dumps(value)
    assert "do-not-store" not in str(failure.value)


def test_transaction_is_atomic_idempotent_and_immutable(journal, sample_instrument):
    first = transaction(trade_event(1, sample_instrument))
    assert journal.append(first) == 1
    second = transaction(trade_event(2, sample_instrument), 1, 2, state_updates={"balance": Decimal(200)})
    assert journal.append(second) == 2
    assert journal.append(first) == 1
    assert journal.cursor == 2 and journal.load_state()["balance"] == Decimal(200)
    with pytest.raises(JournalConflictError, match="different contents"):
        journal.append(replace(first, state_updates={"balance": Decimal(999)}))
    with pytest.raises(TypeError):
        journal.load_state()["balance"] = 0


def test_scoped_trade_duplicates_reject_the_whole_new_transaction(journal, sample_instrument):
    first = transaction(trade_event(1, sample_instrument, trade_id="same"))
    journal.append(first)
    duplicate = transaction(
        trade_event(2, sample_instrument, trade_id="same"), 1, 2, state_updates={"balance": Decimal(999)}
    )
    with pytest.raises(DuplicateFactError):
        journal.append(duplicate)
    assert journal.head_seq == journal.cursor == 1
    assert journal.load_state()["balance"] == Decimal("100.123456")
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
    assert journal.head_seq == 0


def test_write_failure_rolls_back_events_state_keys_and_cursor(journal, sample_instrument):
    journal.connection.execute("""CREATE TRIGGER fail_cursor BEFORE UPDATE ON journal_meta
                                  BEGIN SELECT RAISE(ABORT, 'injected write failure'); END""")
    tx = transaction(trade_event(1, sample_instrument))
    with pytest.raises(sqlite3.DatabaseError, match="injected"):
        journal.append(tx)
    assert journal.head_seq == journal.cursor == 0
    assert journal.load_state() == {}
    assert not journal.contains_trade(tx.deduplication_keys[0])
    assert list(journal.replay_from(0)) == []
    for table in ("journal_transactions", "journal_events", "journal_state", "journal_trade_keys"):
        assert journal.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    journal.connection.execute("DROP TRIGGER fail_cursor")
    assert journal.append(tx) == 1


def test_sqlite_full_is_not_reported_as_success(journal, sample_instrument):
    pages = journal.connection.execute("PRAGMA page_count").fetchone()[0]
    journal.connection.execute(f"PRAGMA max_page_count={pages}")
    tx = transaction(trade_event(1, sample_instrument), state_updates={"large_state": "x" * 500_000})
    with pytest.raises(sqlite3.DatabaseError, match="full"):
        journal.append(tx)
    assert journal.cursor == journal.head_seq == 0
    assert journal.load_state() == {}
    assert not journal.contains_trade(tx.deduplication_keys[0])


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
    assert snapshot.cursor == 0 and snapshot.state["balance"] == Decimal(100)
    assert snapshot.control_record == first_control
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
    # Facts from an old producer remain valid; ownership checks only constrain control changes.
    second = replace(trade_event(2, sample_instrument, event_time=NOW), source_id="old-controller-a")
    journal.append(transaction(second, 1, 2))
    assert journal.load_control_record() == second_control
    assert list(journal.replay_from(1)) == [first, second]


def test_replay_iterator_does_not_include_later_commits(journal, sample_instrument):
    first = trade_event(1, sample_instrument)
    journal.append(transaction(first))
    replay = journal.replay_from(0)
    journal.append(transaction(trade_event(2, sample_instrument), 1, 2))
    assert list(replay) == [first]


def test_checkpoint_is_consistent_when_another_connection_commits(journal, sample_instrument, monkeypatch):
    first = transaction(trade_event(1, sample_instrument))
    journal.append(first)
    path = journal.connection.execute("PRAGMA database_list").fetchone()[2]
    original_load = journal.load_state
    with SQLiteJournal(path, account_id=ACCOUNT) as other:

        def publish_between_reads():
            other.append(transaction(trade_event(2, sample_instrument), 1, 2, state_updates={"balance": Decimal(999)}))
            return original_load()

        monkeypatch.setattr(journal, "load_state", publish_between_reads)
        checkpoint = journal.load_checkpoint()
    assert checkpoint.journal_seq == checkpoint.cursor == 1
    assert checkpoint.state["balance"] == Decimal("100.123456")
    assert checkpoint.deduplication_keys == frozenset(first.deduplication_keys)
    assert journal.head_seq == 2
    assert original_load()["balance"] == Decimal(999)
    assert journal_codec.loads(journal_codec.dumps(checkpoint)) == checkpoint


def test_reopen_and_checksum_detection(tmp_path, sample_instrument):
    path = tmp_path / "trading.db"
    with SQLiteJournal(path, account_id=ACCOUNT) as journal:
        journal.migrate()
        tx = transaction(trade_event(1, sample_instrument))
        journal.append(tx)
        journal.snapshot(1)
    with SQLiteJournal(path, account_id=ACCOUNT) as reopened:
        assert reopened.cursor == 1 and reopened.load_snapshot().deduplication_keys == frozenset(tx.deduplication_keys)
        assert list(reopened.replay_from(0)) == list(tx.events)
        reopened.connection.execute("UPDATE journal_state SET payload='corrupted'")
        with pytest.raises(JournalCorruptionError, match="checksum"):
            reopened.load_state()
    with SQLiteJournal(path, account_id="other-account") as wrong:
        with pytest.raises(JournalConflictError, match="different account"):
            wrong.migrate()


def test_process_exit_before_commit_keeps_last_committed_state(tmp_path, sample_instrument):
    path = tmp_path / "trading.db"
    with SQLiteJournal(path, account_id=ACCOUNT) as journal:
        journal.migrate()
        journal.append(transaction(trade_event(1, sample_instrument)))
    pending = journal_codec.dumps(
        transaction(trade_event(2, sample_instrument), 1, 2, state_updates={"balance": Decimal(999)})
    )
    script = """import os,sys
from qh_trader.infrastructure.journal import SQLiteJournal
from qh_trader.infrastructure.journal_codec import loads
journal=SQLiteJournal(sys.argv[1], account_id=sys.argv[2])
journal._commit=lambda: os._exit(23)
journal.append(loads(sys.argv[3]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path), ACCOUNT, pending],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        check=False,
    )
    assert result.returncode == 23, result.stderr
    with SQLiteJournal(path, account_id=ACCOUNT) as reopened:
        assert reopened.head_seq == reopened.cursor == 1
        assert reopened.load_state()["balance"] == Decimal("100.123456")


def test_database_roles_cannot_be_mixed(tmp_path):
    metadata = tmp_path / "metadata.db"
    with RuleStore(metadata) as store:
        store.migrate()
    with SQLiteJournal(metadata, account_id=ACCOUNT) as journal:
        with pytest.raises(ValueError, match="separate databases"):
            journal.migrate()
    trading = tmp_path / "trading.db"
    with SQLiteJournal(trading, account_id=ACCOUNT) as journal:
        journal.migrate()
    with RuleStore(trading) as store:
        with pytest.raises(ValueError, match="separate databases"):
            store.migrate()
