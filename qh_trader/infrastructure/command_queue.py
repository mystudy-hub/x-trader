"""[Infrastructure 层] Local SQLite command IPC and one account writer (ADR-X1)."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from qh_trader.core.constants import JournalConflictError, JournalCorruptionError
from qh_trader.core.event import CanonicalEvent, JournalSnapshot, JournalTransaction
from qh_trader.core.execution import (
    CommandKind,
    CommandStatus,
    ExecutionCommand,
    ExecutionOwnershipError,
    QueuedCommand,
)
from qh_trader.core.objects import ControlEpoch, ControlRecord, TradeKey, require_int, require_text
from qh_trader.infrastructure import journal_codec
from qh_trader.infrastructure.journal import SQLiteJournal


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _decode(payload: str, digest: str) -> object:
    if _digest(payload) != digest:
        raise JournalCorruptionError("command IPC checksum mismatch")
    try:
        return journal_codec.loads(payload)
    except (TypeError, ValueError, KeyError) as exc:
        raise JournalCorruptionError("command IPC payload violates its normalized contract") from exc


def _command(row: sqlite3.Row, account_id: str) -> QueuedCommand:
    value = _decode(row["payload"], row["sha256"])
    if not isinstance(value, ExecutionCommand):
        raise JournalCorruptionError("command queue contains an invalid command")
    if (
        value.account_id != account_id
        or value.account_id != row["account_id"]
        or value.command_id != row["command_id"]
        or value.control.controller_id != row["controller_id"]
        or value.control.epoch != row["control_epoch"]
        or value.kind.value != row["kind"]
    ):
        raise JournalCorruptionError("command queue identity differs from its payload")
    try:
        return QueuedCommand(row["sequence"], value, CommandStatus(row["status"]), row["processed_seq"])
    except (ValueError, TypeError) as exc:
        raise JournalCorruptionError("invalid command processing state") from exc


class _LocalExecutionLock:
    """OS-owned lock, released on process death; never delete the lock file."""

    def __init__(self, database: Path) -> None:
        self._file = database.with_name(database.name + "-execution.lock").open("a+b")
        try:
            if self._file.seek(0, os.SEEK_END) == 0:
                self._file.write(b"\0")
                self._file.flush()
            self._file.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._file.close()
            raise ExecutionOwnershipError("another execution service owns this account database") from exc

    def close(self) -> None:
        self._file.close()


class SQLiteExecutionStore:
    """Executor-only facade; no concrete storage type leaks into the engine.

    The lock covers this database's local writer lifetime. Broker connection
    isolation is a separate, mandatory takeover check, not a consequence of
    acquiring this lock. The caller owns and closes the injected Journal.
    """

    def __init__(self, journal: SQLiteJournal) -> None:
        journal._head()
        filename = journal.connection.execute("PRAGMA database_list").fetchone()[2]
        if not filename or filename.startswith(("\\\\", "//")):
            raise ValueError("execution IPC requires a local, file-backed trading database")
        self.journal = journal
        self.account_id = journal.account_id
        self._lock = _LocalExecutionLock(Path(filename).resolve())
        self._thread = threading.get_ident()
        self._closed = False

    def __enter__(self) -> SQLiteExecutionStore:
        self.assert_owner()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def assert_owner(self) -> None:
        if self._closed or threading.get_ident() != self._thread:
            raise ExecutionOwnershipError("execution store must run on its owning account thread")

    def close(self) -> None:
        if self._closed:
            return
        self.assert_owner()
        self._lock.close()
        self._closed = True

    def migrate(self) -> None:
        """Assembly calls this once before opening producer connections."""
        self.assert_owner()
        connection = self.journal.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS command_queue (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    command_id TEXT UNIQUE NOT NULL,
                    account_id TEXT NOT NULL,
                    controller_id TEXT NOT NULL,
                    control_epoch INTEGER NOT NULL CHECK(control_epoch>=0),
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING'
                        CHECK(status IN ('PENDING', 'DISPATCHING', 'COMPLETED', 'NOT_SENT',
                                         'SENT_UNKNOWN', 'REJECTED', 'REJECTED_STALE')),
                    processed_seq INTEGER REFERENCES journal_transactions(journal_seq),
                    CHECK((status='PENDING' AND processed_seq IS NULL)
                       OR (status<>'PENDING' AND processed_seq IS NOT NULL)))"""
            )
            columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(command_queue)"))
            if columns != (
                "sequence", "command_id", "account_id", "controller_id", "control_epoch",
                "kind", "payload", "sha256", "status", "processed_seq",
            ):
                raise JournalCorruptionError("unsupported execution command schema")
            connection.execute("CREATE INDEX IF NOT EXISTS command_pending ON command_queue(status, sequence)")
            self.journal._commit()
        except Exception:
            connection.rollback()
            raise

    def checkpoint(self) -> JournalSnapshot:
        self.assert_owner()
        return self.journal.load_checkpoint()

    def control(self) -> ControlRecord | None:
        self.assert_owner()
        return self.journal.load_control_record()

    def next_ingress_sequence(self) -> int:
        self.assert_owner()
        return self.journal._head()["last_ingress_seq"] + 1

    def get(self, command_id: str) -> QueuedCommand | None:
        self.assert_owner()
        require_text(command_id, "command_id")
        row = self.journal.connection.execute(
            "SELECT * FROM command_queue WHERE command_id=?", (command_id,)
        ).fetchone()
        return None if row is None else _command(row, self.account_id)

    def next_pending(self) -> QueuedCommand | None:
        self.assert_owner()
        # A watchdog application must not self-authorize or block ordinary commands.
        row = self.journal.connection.execute(
            "SELECT * FROM command_queue WHERE status='PENDING' AND kind<>? ORDER BY sequence LIMIT 1",
            (CommandKind.TAKEOVER_REQUEST.value,),
        ).fetchone()
        return None if row is None else _command(row, self.account_id)

    def interrupted(self) -> Sequence[QueuedCommand]:
        self.assert_owner()
        rows = self.journal.connection.execute(
            "SELECT * FROM command_queue WHERE status='DISPATCHING' ORDER BY sequence"
        ).fetchall()
        return tuple(_command(row, self.account_id) for row in rows)

    def contains_trade(self, key: TradeKey) -> bool:
        self.assert_owner()
        return self.journal.contains_trade(key)

    def event(self, event_id: str) -> CanonicalEvent | None:
        self.assert_owner()
        row = self.journal.connection.execute(
            "SELECT payload, sha256 FROM journal_events WHERE event_id=?", (event_id,)
        ).fetchone()
        if row is None:
            return None
        value = self.journal._read(row["payload"], row["sha256"])
        if not isinstance(value, CanonicalEvent):
            raise JournalCorruptionError("journal entry is not a canonical event")
        return value

    def commit(
        self,
        transaction: JournalTransaction,
        *,
        expected_control: ControlEpoch | None,
        command: QueuedCommand | None = None,
        status: CommandStatus | None = None,
    ) -> int:
        self.assert_owner()
        if (command is None) != (status is None):
            raise ValueError("command acknowledgements need both a command and its next status")
        if status is not None and not isinstance(status, CommandStatus):
            raise TypeError("command status must be normalized")
        if command is not None and status is not None:
            allowed = {
                CommandStatus.PENDING: {
                    CommandStatus.DISPATCHING, CommandStatus.COMPLETED,
                    CommandStatus.REJECTED, CommandStatus.REJECTED_STALE,
                },
                CommandStatus.DISPATCHING: {
                    CommandStatus.NOT_SENT, CommandStatus.SENT_UNKNOWN,
                },
            }
            if status not in allowed.get(command.status, set()):
                raise JournalConflictError("command transition could repeat an already processed request")

        def precondition() -> None:
            self.assert_owner()
            current = self.control()
            if (None if current is None else current.epoch) != expected_control:
                raise JournalConflictError("execution control changed before the atomic commit")

        def acknowledge(sequence: int) -> None:
            if command is None or status is None:
                return
            current = self.get(command.command.command_id)
            if current is None or current.command != command.command or current.sequence != command.sequence:
                raise JournalConflictError("queued command changed before commit")
            if current.status == status and current.processed_seq == sequence:
                return  # Exact replay of the same transaction is idempotent.
            if current != command:
                raise JournalConflictError("command has already been processed by another transaction")
            self.journal.connection.execute(
                "UPDATE command_queue SET status=?, processed_seq=? WHERE command_id=?",
                (status.value, sequence, command.command.command_id),
            )

        return self.journal._append_atomic(transaction, precondition=precondition, before_commit=acknowledge)


class SQLiteCommandClient:
    """Producer inserts only; subscriptions read the existing Journal by cursor.

    A timed-out INSERT raises; callers must retain the same command_id when they
    retry. This facade never silently drops a command or opens a missing database.
    It is a trusted local-process API, not an authentication boundary for arbitrary
    programs with filesystem access to the trading database.
    """

    def __init__(self, database: Path | str, *, account_id: str, read_only: bool = False) -> None:
        require_text(account_id, "account_id")
        self.account_id = account_id
        self.submission_failures = 0
        mode = "ro" if read_only else "rw"
        path = Path(database).resolve(strict=True)
        if str(path).startswith(("\\\\", "//")):
            raise ValueError("command IPC cannot use a network share")
        self._connection = sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA synchronous=FULL")
            if self._connection.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                raise ValueError("execution IPC requires an initialized WAL journal")
            head = self._connection.execute("SELECT account_id FROM journal_meta WHERE singleton=1").fetchone()
            if head is None or head["account_id"] != account_id:
                raise JournalConflictError("command database is bound to another account")
            self._connection.execute("SELECT sequence FROM command_queue LIMIT 0")
            self._connection.set_authorizer(self._authorize)
        except Exception:
            self._connection.close()
            raise

    @staticmethod
    def _authorize(action: int, table: str | None, column, database, trigger) -> int:
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_TRANSACTION):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_INSERT and table == "command_queue":
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    def __enter__(self) -> SQLiteCommandClient:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def get(self, command_id: str) -> QueuedCommand | None:
        row = self._connection.execute("SELECT * FROM command_queue WHERE command_id=?", (command_id,)).fetchone()
        return None if row is None else _command(row, self.account_id)

    def submit(self, command: ExecutionCommand) -> QueuedCommand:
        if not isinstance(command, ExecutionCommand):
            raise TypeError("only normalized execution commands may be submitted")
        if command.account_id != self.account_id:
            raise JournalConflictError("cannot submit a command for another account")
        payload = journal_codec.dumps(command)
        try:
            self._connection.execute(
                """INSERT OR IGNORE INTO command_queue
                   (command_id, account_id, controller_id, control_epoch, kind, payload, sha256)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    command.command_id, command.account_id, command.control.controller_id,
                    command.control.epoch, command.kind.value, payload, _digest(payload),
                ),
            )
            current = self.get(command.command_id)
            if current is None or current.command != command:
                raise JournalConflictError("command_id was reused with different contents")
            return current
        except (sqlite3.Error, JournalConflictError):
            self.submission_failures += 1
            raise

    def read_events(self, after_seq: int) -> tuple[int, tuple[CanonicalEvent, ...]]:
        """Return one consistent read snapshot and the cursor to retain afterwards."""
        require_int(after_seq, "journal subscription cursor")
        self._connection.execute("BEGIN")
        try:
            head = self._connection.execute("SELECT head_seq FROM journal_meta WHERE singleton=1").fetchone()[0]
            if after_seq > head:
                raise JournalConflictError("subscription cursor is beyond committed history")
            rows = self._connection.execute(
                """SELECT payload, sha256 FROM journal_events
                   WHERE journal_seq>? AND journal_seq<=? ORDER BY journal_seq, ordinal""",
                (after_seq, head),
            ).fetchall()
            events: list[CanonicalEvent] = []
            for row in rows:
                value = _decode(row["payload"], row["sha256"])
                if not isinstance(value, CanonicalEvent):
                    raise JournalCorruptionError("subscription contains an invalid journal event")
                events.append(value)
            self._connection.commit()
            return head, tuple(events)
        except Exception:
            self._connection.rollback()
            raise
