"""[Tests 层] S5-10 离线事实重放、游标与投影差异证据 (A04/A14/A18)。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from qh_trader.core.constants import EventKind, Exchange, Offset, Side
from qh_trader.core.event import CanonicalEvent, JournalTransaction, TimerEvent
from qh_trader.core.objects import ControlEpoch, ControlRecord, Trade, TradeKey
from qh_trader.infrastructure import journal_codec
from qh_trader.infrastructure.command_queue import SQLiteExecutionStore
from qh_trader.infrastructure.journal import SQLiteJournal
from scripts import backup_state, replay_events

ACCOUNT = "offline-replay-account"
NOW = datetime(2024, 9, 10, 1, tzinfo=timezone.utc)


@pytest.fixture
def journal(tmp_path, sample_instrument):
    path = tmp_path / "source.db"
    with SQLiteJournal(path, account_id=ACCOUNT) as journal:
        journal.migrate()
        with SQLiteExecutionStore(journal) as store:
            store.migrate()
        journal.snapshot(0)
        event = CanonicalEvent(
            event_id="event-1",
            kind=EventKind.TIMER,
            event_time=NOW,
            available_at=NOW,
            sequence=1,
            source_id="fixture",
            payload=TimerEvent("initialize"),
        )
        journal.append(
            JournalTransaction(
                transaction_id="initial",
                events=(event,),
                cursor_before=0,
                cursor_after=1,
                state_updates={"balance": Decimal("1000"), "reservation": Decimal("100")},
                control_record=ControlRecord(ControlEpoch("first", 17), NOW, 1),
            )
        )
        journal.snapshot(1)
        key = TradeKey(ACCOUNT, Exchange.SHFE, date(2024, 9, 10), "trade-1")
        trade = Trade(
            account_id=ACCOUNT,
            instrument=sample_instrument,
            trading_day=key.trading_day,
            trade_id=key.trade_id,
            side=Side.BUY,
            offset=Offset.OPEN,
            quantity=1,
            price=Decimal("3500"),
            event_time=NOW,
            available_at=NOW,
            deduplication_key=key,
        )
        journal.append(
            JournalTransaction(
                transaction_id="fill",
                events=(
                    CanonicalEvent(
                        event_id="event-2",
                        kind=EventKind.TRADE_REPORT,
                        event_time=NOW,
                        available_at=NOW,
                        sequence=2,
                        source_id="fixture",
                        payload=trade,
                    ),
                ),
                cursor_before=1,
                cursor_after=2,
                deduplication_keys=(key,),
                state_updates={"balance": Decimal("995"), "reservation": None, "position": 1},
            )
        )
        journal.append(
            JournalTransaction(
                transaction_id="takeover",
                events=(),
                cursor_before=2,
                cursor_after=2,
                control_record=ControlRecord(ControlEpoch("second", 18), NOW, 3),
            )
        )
        yield path, journal


@pytest.mark.parametrize("start, expected_replayed", [(None, 2), (0, 3), (1, 2)])
def test_replay_validates_snapshots_trades_deletions_control_and_published_state(journal, start, expected_replayed):
    path, instance = journal
    before = instance.load_checkpoint()
    file_before = path.read_bytes()
    wal_before = Path(str(path) + "-wal").read_bytes()
    report = replay_events.replay_events(path, ACCOUNT, snapshot_seq=start)
    assert report["consistent"] and report["differences"] == []
    assert report["evidence_type"] == "historical_replay"
    assert report["validation_scope"] == "journal_facts_and_projections"
    assert report["validated_transactions"] == report["head_seq"] == 3
    assert report["validated_events"] == report["cursor"] == 2
    assert report["validated_snapshots"] == 2
    assert report["replayed_transactions"] == expected_replayed
    assert report["trading_authorized"] is False
    assert instance.load_checkpoint() == before
    assert path.read_bytes() == file_before
    assert Path(str(path) + "-wal").read_bytes() == wal_before


def _rewrite(connection, table, payload, where):
    encoded = journal_codec.dumps(payload)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    connection.execute(f"UPDATE {table} SET payload=?, sha256=? WHERE {where}", (encoded, digest))


@pytest.mark.parametrize("field", ["state", "cursor", "control", "dedup", "ingress", "snapshot"])
def test_validly_encoded_projection_drift_produces_a_difference_report(journal, field):
    path, instance = journal
    connection = instance.connection
    if field == "state":
        _rewrite(connection, "journal_state", Decimal("999999"), "state_key='balance'")
        expected_field = "projection.state.balance"
    elif field == "cursor":
        connection.execute("UPDATE journal_meta SET cursor=1")
        expected_field = "projection.cursor"
    elif field == "control":
        _rewrite(connection, "journal_control", ControlRecord(ControlEpoch("wrong", 16), NOW, 3), "singleton=1")
        expected_field = "projection.control_record"
    elif field == "dedup":
        connection.execute("DELETE FROM journal_trade_keys")
        expected_field = "projection.deduplication_keys"
    elif field == "ingress":
        connection.execute("UPDATE journal_meta SET last_ingress_seq=999")
        expected_field = "last_ingress_seq"
    else:
        row = connection.execute("SELECT payload FROM journal_snapshots WHERE journal_seq=1").fetchone()
        snapshot = dict(journal_codec.loads(row[0]))
        snapshot["state"] = {"balance": Decimal("123"), "reservation": Decimal("100")}
        _rewrite(connection, "journal_snapshots", snapshot, "journal_seq=1")
        expected_field = "snapshot[1].state.balance"
    report = replay_events.replay_events(path, ACCOUNT)
    assert report["consistent"] is False
    assert expected_field in {item["field"] for item in report["differences"]}


@pytest.mark.parametrize("fault", ["checksum", "event-index", "tx-index", "event-order", "head", "snapshot-digest"])
def test_corrupt_history_is_refused_instead_of_replayed(journal, fault):
    path, instance = journal
    connection = instance.connection
    if fault == "checksum":
        connection.execute("UPDATE journal_transactions SET sha256='wrong' WHERE journal_seq=2")
    elif fault == "event-index":
        connection.execute("UPDATE journal_events SET event_id='wrong' WHERE ingress_seq=2")
    elif fault == "tx-index":
        connection.execute("UPDATE journal_transactions SET cursor_before=0 WHERE journal_seq=2")
    elif fault == "event-order":
        row = connection.execute("SELECT payload FROM journal_transactions WHERE journal_seq=2").fetchone()
        transaction = journal_codec.loads(row[0])
        changed = replace(transaction, events=(replace(transaction.events[0], sequence=0),))
        _rewrite(connection, "journal_transactions", changed, "journal_seq=2")
    elif fault == "head":
        connection.execute("UPDATE journal_meta SET head_seq=4")
    else:
        connection.execute("UPDATE journal_snapshots SET sha256='wrong' WHERE journal_seq=1")
    with pytest.raises(replay_events.ReplayError):
        replay_events.replay_events(path, ACCOUNT)


def test_wrong_account_missing_snapshot_and_missing_database_are_refused(journal, tmp_path):
    path, _ = journal
    for account, sequence in (("other-account", None), (ACCOUNT, 2), (ACCOUNT, -1), (ACCOUNT, 4)):
        with pytest.raises(replay_events.ReplayError):
            replay_events.replay_events(path, account, snapshot_seq=sequence)
    with pytest.raises(replay_events.ReplayError, match="does not exist"):
        replay_events.replay_events(tmp_path / "missing.db", ACCOUNT)
    assert not (tmp_path / "missing.db").exists()


def test_restore_guard_allows_offline_replay(journal, tmp_path):
    path, _ = journal
    backup_state.backup_state(path, tmp_path / "backup")
    backup_state.restore_state(tmp_path / "backup", tmp_path / "restored")
    report = replay_events.replay_events(tmp_path / "restored" / "trading.db", ACCOUNT)
    assert report["consistent"] and report["offline_restore"]
    assert report["trading_authorized"] is False


def test_cli_writes_versioned_report_returns_drift_and_never_overwrites(journal, tmp_path, capsys):
    path, instance = journal
    report_path = tmp_path / "report.json"
    args = ["--journal", str(path), "--account", ACCOUNT, "--out", str(report_path)]
    assert replay_events.main(args) == 0
    evidence = report_path.read_bytes()
    assert json.loads(evidence)["consistent"]
    assert replay_events.main(args) == 2
    assert report_path.read_bytes() == evidence
    instance.connection.execute("UPDATE journal_meta SET cursor=0")
    assert replay_events.main(args[:-1] + [str(tmp_path / "drift.json")]) == 1
    assert not json.loads((tmp_path / "drift.json").read_text(encoding="utf-8"))["consistent"]
    assert capsys.readouterr().err
