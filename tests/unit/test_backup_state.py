"""[Tests 层] S5-09 WAL 一致性、完整性和离线隔离恢复 (A14/A18/A23)。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from qh_trader.core.constants import EventKind
from qh_trader.core.event import CanonicalEvent, JournalTransaction, TimerEvent
from qh_trader.core.execution import CommandKind, CommandStatus, ExecutionCommand
from qh_trader.core.objects import ControlEpoch, ControlRecord
from qh_trader.infrastructure.command_queue import SQLiteCommandClient, SQLiteExecutionStore
from qh_trader.infrastructure.journal import SQLiteJournal
from scripts import backup_state as backup
from scripts.live_assembly import AssemblyError, ExecutionSpec, assemble

ACCOUNT = "backup-test-account"
NOW = datetime(2024, 9, 10, 1, tzinfo=timezone.utc)
CONTROL = ControlEpoch("original-controller", 17)


@pytest.fixture
def active(tmp_path):
    path = tmp_path / "source" / "trading.db"
    with SQLiteJournal(path, account_id=ACCOUNT) as journal:
        journal.migrate()
        journal.connection.execute("PRAGMA wal_autocheckpoint=0")
        with SQLiteExecutionStore(journal) as store:
            store.migrate()
            event = CanonicalEvent(
                event_id="clock-1",
                kind=EventKind.TIMER,
                event_time=NOW,
                available_at=NOW,
                sequence=1,
                source_id="fixture",
                payload=TimerEvent("settlement"),
            )
            journal.append(
                JournalTransaction(
                    transaction_id="initial",
                    events=(event,),
                    cursor_before=0,
                    cursor_after=1,
                    control_record=ControlRecord(CONTROL, NOW, 1),
                    state_updates={
                        "balance": Decimal("10000.125"),
                        "reservations": {"open-order": Decimal("120")},
                        "account_checkpoint": {"fixture_version": 1, "through_fact": 50},
                        "account_fact/51": {"fixture": "retained-tail"},
                    },
                )
            )
            with SQLiteCommandClient(path, account_id=ACCOUNT) as client:
                for number in (1, 2):
                    client.submit(
                        ExecutionCommand(
                            command_id=f"command-{number}",
                            account_id=ACCOUNT,
                            producer_id="operator",
                            control=CONTROL,
                            kind=CommandKind.PAUSE,
                            submitted_at=NOW,
                            payload={"reason": "fixture"},
                        )
                    )
                store.commit(
                    JournalTransaction(transaction_id="command-done", events=(), cursor_before=1, cursor_after=1),
                    expected_control=CONTROL,
                    command=client.get("command-1"),
                    status=CommandStatus.COMPLETED,
                )
            journal.snapshot(journal.head_seq)
            yield path, journal


def test_wal_backup_and_restore_preserve_whole_database_while_writer_is_open(active, tmp_path):
    path, journal = active
    assert Path(str(path) + "-wal").stat().st_size > 0
    before = journal.load_checkpoint()
    snapshot_before = journal.load_snapshot()
    events_before = tuple(journal.replay_from(0))
    artifact = tmp_path / "backup"
    manifest = backup.backup_state(path, artifact)
    assert manifest["journal"]["head_seq"] == 2
    assert manifest["journal"]["cursor"] == 1
    assert manifest["journal"]["control"] == {
        "controller_id": CONTROL.controller_id,
        "epoch": 17,
        "journal_seq": 1,
    }
    assert manifest["tables"]["command_queue"] == 2
    assert backup.verify_backup(artifact) == manifest
    assert sorted(item.name for item in artifact.iterdir()) == ["manifest.json", "trading.db"]

    # 备份后的新事实不能污染已发布工件。
    journal.append(
        JournalTransaction(
            transaction_id="after-backup",
            events=(),
            cursor_before=1,
            cursor_after=1,
            state_updates={"balance": Decimal("999999")},
        )
    )
    restored = tmp_path / "restored"
    receipt = backup.restore_state(artifact, restored)
    assert receipt["offline_only"] is True and receipt["trading_authorized"] is False
    assert receipt["journal"]["control"]["epoch"] == 17
    with SQLiteJournal(restored / "trading.db", account_id=ACCOUNT) as reopened:
        assert reopened.load_checkpoint() == before
        assert reopened.load_snapshot() == snapshot_before
        assert tuple(reopened.replay_from(0)) == events_before
        assert reopened.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert reopened.connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        guard = reopened.connection.execute("SELECT * FROM qh_offline_restore_guard").fetchone()
        assert tuple(guard) == (
            manifest["database"]["sha256"],
            receipt["restored_utc"],
            "latest_control_epoch_unverified",
        )
    with SQLiteCommandClient(restored / "trading.db", account_id=ACCOUNT, read_only=True) as client:
        assert client.get("command-1").status == CommandStatus.COMPLETED
        assert client.get("command-1").processed_seq == 2
        assert client.get("command-2").status == CommandStatus.PENDING
        assert client.control().epoch == CONTROL
    assert backup.verify_backup(artifact) == manifest


def test_uncommitted_wal_changes_do_not_enter_snapshot(active, tmp_path):
    path, journal = active
    journal.connection.execute("BEGIN IMMEDIATE")
    journal.connection.execute("DELETE FROM command_queue")
    try:
        manifest = backup.backup_state(path, tmp_path / "snapshot")
        assert manifest["tables"]["command_queue"] == 2
    finally:
        journal.connection.rollback()


@pytest.mark.parametrize("mode", ["paper", "live"])
def test_restored_database_cannot_start_execution_or_create_writer_lock(active, tmp_path, mode):
    path, _ = active
    backup.backup_state(path, tmp_path / "backup")
    backup.restore_state(tmp_path / "backup", tmp_path / "restored")
    restored = tmp_path / "restored" / "trading.db"
    before = restored.read_bytes()
    spec = ExecutionSpec(
        mode=mode,
        account_id=ACCOUNT,
        journal_path=restored,
        catalog_path=tmp_path / "intentionally-unread-catalog.json",
        symbols=("SHFE.rb2410",),
        initial_capital=Decimal("10000"),
        trading_day=date(2024, 9, 10),
        controller_id="would-be-controller",
        heartbeat_path=tmp_path / "never-created.json",
    )
    with pytest.raises(AssemblyError, match="offline restore cannot execute"):
        assemble(spec)
    assert restored.read_bytes() == before
    assert not Path(str(restored) + "-execution.lock").exists()
    assert not spec.heartbeat_path.exists()


@pytest.mark.parametrize("change", ["bytes", "manifest-cursor", "version", "path", "journal-version", "payload"])
def test_corrupted_or_incompatible_backups_are_refused(active, tmp_path, change):
    path, _ = active
    directory = tmp_path / "backup"
    manifest = backup.backup_state(path, directory)
    database = directory / "trading.db"
    if change == "bytes":
        with database.open("r+b") as stream:
            stream.seek(40)
            stream.write(b"corrupt")
    elif change == "manifest-cursor":
        manifest["journal"]["cursor"] = 100
    elif change == "version":
        manifest["version"] = 2
    elif change == "path":
        manifest["database"]["file"] = "../source/trading.db"
    else:
        with closing(sqlite3.connect(database)) as connection:
            if change == "journal-version":
                connection.execute("UPDATE journal_schema SET version=2")
            else:
                connection.execute("UPDATE journal_state SET payload='corrupted'")
            connection.commit()
        manifest["database"]["bytes"] = database.stat().st_size
        manifest["database"]["sha256"] = hashlib.sha256(database.read_bytes()).hexdigest()
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(backup.BackupError):
        backup.verify_backup(directory)
    with pytest.raises(backup.BackupError):
        backup.restore_state(directory, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize(
    "statements, error",
    [
        (("UPDATE command_queue SET control_epoch=99 WHERE command_id='command-1'",), "command queue"),
        (("UPDATE command_queue SET controller_id='other' WHERE command_id='command-1'",), "command queue"),
        (("UPDATE command_queue SET command_id='wrong' WHERE command_id='command-1'",), "command queue"),
        (("UPDATE command_queue SET kind='RESUME' WHERE command_id='command-1'",), "command queue"),
        (("UPDATE journal_meta SET cursor=999",), "cursor"),
        (("UPDATE journal_meta SET head_seq=3",), "head"),
        (("UPDATE journal_meta SET last_ingress_seq=999",), "ingress"),
        (("UPDATE journal_transactions SET transaction_id='wrong' WHERE journal_seq=2",), "transaction index"),
        (("UPDATE journal_transactions SET cursor_before=0 WHERE journal_seq=2",), "transaction index"),
        (("UPDATE journal_events SET event_id='wrong'",), "event index"),
        (("UPDATE journal_events SET ingress_seq=9", "UPDATE journal_meta SET last_ingress_seq=9"), "event index"),
        (("UPDATE journal_control SET journal_seq=2",), "control record"),
    ],
)
def test_index_or_cursor_corruption_is_rejected_even_with_updated_file_checksum(active, tmp_path, statements, error):
    path, journal = active
    artifact = tmp_path / "backup"
    manifest = backup.backup_state(path, artifact)
    for statement in statements:
        journal.connection.execute(statement)
    with pytest.raises(backup.BackupError, match=error):
        backup.backup_state(path, tmp_path / "invalid-backup")
    assert not (tmp_path / "invalid-backup").exists()

    database = artifact / "trading.db"
    with closing(sqlite3.connect(database)) as connection:
        for statement in statements:
            connection.execute(statement)
        connection.commit()
    manifest["database"]["bytes"] = database.stat().st_size
    manifest["database"]["sha256"] = hashlib.sha256(database.read_bytes()).hexdigest()
    (artifact / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(backup.BackupError, match=error):
        backup.verify_backup(artifact)
    with pytest.raises(backup.BackupError, match=error):
        backup.restore_state(artifact, tmp_path / "refused-restore")
    assert not (tmp_path / "refused-restore").exists()


def test_existing_and_active_directories_are_never_overwritten(active, tmp_path):
    path, journal = active
    directory = tmp_path / "backup"
    backup.backup_state(path, directory)
    before = journal.load_checkpoint()
    for destination in (directory, path.parent, path, tmp_path):
        with pytest.raises(backup.BackupError, match="new directory"):
            backup.restore_state(directory, destination)
        with pytest.raises(backup.BackupError, match="new directory"):
            backup.backup_state(path, destination)
    assert journal.load_checkpoint() == before
    # 即使活动主文件暂时缺失，也不复用含孤立 WAL 的目录。
    orphan = tmp_path / "orphan"
    orphan.mkdir()
    (orphan / "trading.db-wal").write_bytes(b"unrecovered WAL")
    with pytest.raises(backup.BackupError, match="new directory"):
        backup.restore_state(directory, orphan)
    assert (orphan / "trading.db-wal").read_bytes() == b"unrecovered WAL"


def test_incomplete_publication_and_active_backup_sidecars_are_refused(active, tmp_path):
    path, _ = active
    directory = tmp_path / "backup"
    backup.backup_state(path, directory)
    sidecar = directory / "trading.db-wal"
    sidecar.write_bytes(b"unexpected WAL")
    with pytest.raises(backup.BackupError, match="self-contained"):
        backup.verify_backup(directory)
    sidecar.unlink()
    (directory / "manifest.json").rename(directory / "manifest.json.partial")
    with pytest.raises(backup.BackupError, match="manifest"):
        backup.verify_backup(directory)


def test_publication_failure_cleans_only_its_new_directory(active, tmp_path, monkeypatch):
    path, journal = active
    directory = tmp_path / "backup"
    before = journal.load_checkpoint()

    def disk_full(*args, **kwargs):
        raise OSError("injected disk full")

    monkeypatch.setattr(backup, "_write_json", disk_full)
    with pytest.raises(OSError, match="disk full"):
        backup.backup_state(path, directory)
    assert not directory.exists()
    assert journal.load_checkpoint() == before


def test_only_one_concurrent_publisher_can_claim_a_directory(active, tmp_path):
    path, _ = active
    destination = tmp_path / "contended"

    def publish():
        try:
            backup.backup_state(path, destination)
            return "published"
        except backup.BackupError:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: publish(), range(2)))
    assert sorted(outcomes) == ["published", "refused"]
    backup.verify_backup(destination)


def test_cli_backup_verify_restore_and_failure_exit_codes(active, tmp_path, capsys):
    path, _ = active
    artifact = tmp_path / "backup"
    assert backup.main(["backup", "--journal", str(path), "--out", str(artifact)]) == 0
    assert backup.main(["verify", "--backup", str(artifact)]) == 0
    assert backup.main(["restore", "--backup", str(artifact), "--out", str(tmp_path / "offline")]) == 0
    assert backup.main(["restore", "--backup", str(artifact), "--out", str(path.parent)]) == 2
    assert "new directory" in capsys.readouterr().err
    assert backup.main(["backup", "--journal", str(tmp_path / "missing.db"), "--out", str(tmp_path / "bad")]) == 2
    assert not (tmp_path / "missing.db").exists()


def test_non_trading_database_is_not_published(tmp_path):
    source = tmp_path / "unrelated.db"
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")
    with pytest.raises(backup.BackupError, match="journal and command queue"):
        backup.backup_state(source, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()
