"""SQLite JournalPort: atomically persist events, projections, trade identities, control and cursor."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any

from qh_trader.core.constants import DuplicateFactError, JournalConflictError, JournalCorruptionError
from qh_trader.core.event import CanonicalEvent, JournalSnapshot, JournalTransaction
from qh_trader.core.objects import ControlRecord, Trade, TradeKey, freeze_payload, require_int, require_text
from qh_trader.infrastructure import journal_codec as codec


def _hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SQLiteJournal:
    """One account per database. Migration is explicit; failed appends never publish partial state."""

    def __init__(self, database: Path | str, *, account_id: str) -> None:
        require_text(account_id, "account_id")
        self.account_id = account_id
        in_memory = str(database) == ":memory:"
        if not in_memory:
            path = Path(database).resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            database = path
        self.connection = sqlite3.connect(database, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        mode = self.connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        if not in_memory and mode != "wal":
            self.close()
            raise RuntimeError("trading journal requires a local SQLite WAL database")

    def __enter__(self) -> SQLiteJournal:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _commit(self) -> None:
        self.connection.commit()

    def migrate(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            metadata_db = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rule_store_schema'"
            ).fetchone()
            if metadata_db is not None:
                raise ValueError("rule metadata and the trading journal must use separate databases")
            self.connection.execute("CREATE TABLE IF NOT EXISTS journal_schema (version INTEGER NOT NULL)")
            versions = self.connection.execute("SELECT version FROM journal_schema").fetchall()
            if versions and (len(versions) != 1 or versions[0][0] != 1):
                raise ValueError("unsupported journal schema")
            if not versions:
                self.connection.execute("INSERT INTO journal_schema VALUES (1)")
            definitions = (
                """CREATE TABLE IF NOT EXISTS journal_meta (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    account_id TEXT NOT NULL, head_seq INTEGER NOT NULL CHECK(head_seq>=0),
                    cursor INTEGER NOT NULL CHECK(cursor>=0),
                    last_ingress_seq INTEGER NOT NULL CHECK(last_ingress_seq>=-1))""",
                """CREATE TABLE IF NOT EXISTS journal_transactions (
                    journal_seq INTEGER PRIMARY KEY CHECK(journal_seq>0),
                    transaction_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, sha256 TEXT NOT NULL,
                    cursor_before INTEGER NOT NULL, cursor_after INTEGER NOT NULL,
                    CHECK(cursor_after>=cursor_before AND cursor_before>=0))""",
                """CREATE TABLE IF NOT EXISTS journal_events (
                    journal_seq INTEGER NOT NULL REFERENCES journal_transactions(journal_seq),
                    ordinal INTEGER NOT NULL, event_id TEXT UNIQUE NOT NULL,
                    ingress_seq INTEGER UNIQUE NOT NULL CHECK(ingress_seq>=0),
                    payload TEXT NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY(journal_seq, ordinal))""",
                """CREATE TABLE IF NOT EXISTS journal_state (
                    state_key TEXT PRIMARY KEY, payload TEXT NOT NULL, sha256 TEXT NOT NULL,
                    journal_seq INTEGER NOT NULL REFERENCES journal_transactions(journal_seq))""",
                """CREATE TABLE IF NOT EXISTS journal_trade_keys (
                    key_hash TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    journal_seq INTEGER NOT NULL REFERENCES journal_transactions(journal_seq))""",
                """CREATE TABLE IF NOT EXISTS journal_control (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload TEXT NOT NULL, sha256 TEXT NOT NULL,
                    journal_seq INTEGER NOT NULL REFERENCES journal_transactions(journal_seq))""",
                """CREATE TABLE IF NOT EXISTS journal_snapshots (
                    journal_seq INTEGER PRIMARY KEY CHECK(journal_seq>=0),
                    payload TEXT NOT NULL, sha256 TEXT NOT NULL)""",
            )
            for statement in definitions:
                self.connection.execute(statement)
            self.connection.execute("INSERT OR IGNORE INTO journal_meta VALUES (1, ?, 0, 0, -1)", (self.account_id,))
            self._head()
            self._commit()
        except Exception:
            self.connection.rollback()
            raise

    def _head(self) -> sqlite3.Row:
        schema = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='journal_schema'"
        ).fetchone()
        if schema is None:
            raise RuntimeError("call journal.migrate() from the assembly entry point first")
        versions = self.connection.execute("SELECT version FROM journal_schema").fetchall()
        if len(versions) != 1 or versions[0][0] != 1:
            raise JournalCorruptionError("invalid journal schema version")
        row = self.connection.execute("SELECT * FROM journal_meta WHERE singleton=1").fetchone()
        if row is None or row["account_id"] != self.account_id:
            raise JournalConflictError("journal database is bound to a different account")
        return row

    @property
    def head_seq(self) -> int:
        return self._head()["head_seq"]

    @property
    def cursor(self) -> int:
        return self._head()["cursor"]

    def _check_account(self, value: Any) -> None:
        if is_dataclass(value) and not isinstance(value, type):
            if hasattr(value, "account_id") and value.account_id != self.account_id:
                raise JournalConflictError("cross-account values cannot enter this journal")
            for member in fields(value):
                self._check_account(getattr(value, member.name))
        elif isinstance(value, Mapping):
            if "account_id" in value and value["account_id"] != self.account_id:
                raise JournalConflictError("cross-account state cannot enter this journal")
            for item in value.values():
                self._check_account(item)
        elif isinstance(value, (tuple, list, set, frozenset)):
            for item in value:
                self._check_account(item)

    def _read(self, payload: str, digest: str) -> Any:
        if _hash(payload) != digest:
            raise JournalCorruptionError("journal payload checksum mismatch")
        try:
            result = codec.loads(payload)
            self._check_account(result)
            return result
        except (TypeError, ValueError, KeyError, JournalConflictError) as exc:
            raise JournalCorruptionError("journal payload violates its normalized contract") from exc

    def contains_trade(self, key: TradeKey) -> bool:
        if not isinstance(key, TradeKey):
            raise TypeError("trade lookup requires a scoped TradeKey")
        self._head()
        self._check_account(key)
        payload = codec.dumps(key)
        row = self.connection.execute(
            "SELECT payload FROM journal_trade_keys WHERE key_hash=?", (_hash(payload),)
        ).fetchone()
        if row is not None and row["payload"] != payload:
            raise JournalCorruptionError("scoped trade identity checksum mismatch")
        return row is not None

    def append(self, transaction: JournalTransaction) -> int:
        return self._append_atomic(transaction)

    def _append_atomic(
        self,
        transaction: JournalTransaction,
        *,
        precondition: Callable[[], None] | None = None,
        before_commit: Callable[[int], None] | None = None,
    ) -> int:
        """Infrastructure extension for an atomic command acknowledgement.

        Hooks run inside the same short write transaction; they must only access
        local SQLite state. Public JournalPort callers continue to use append().
        """
        if not isinstance(transaction, JournalTransaction):
            raise TypeError("append requires a normalized JournalTransaction")
        self._check_account(transaction)
        payload = codec.dumps(transaction)
        digest = _hash(payload)
        keys = transaction.deduplication_keys
        if set(transaction.state_updates) & {
            "journal_seq",
            "head_seq",
            "cursor",
            "control_record",
            "control_epoch",
            "account_id",
        }:
            raise JournalConflictError("reserved journal metadata must use transaction fields, not state projections")
        if len(set(keys)) != len(keys):
            raise JournalConflictError("transaction repeats a scoped trade identity")
        required = {event.payload.deduplication_key for event in transaction.events if isinstance(event.payload, Trade)}
        if not required <= set(keys):
            raise JournalConflictError("trade reports must declare their atomic deduplication keys")
        if transaction.cursor_after != transaction.cursor_before and not transaction.events:
            raise JournalConflictError("cursor advancement requires persisted event evidence")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if precondition is not None:
                precondition()
            head = self._head()
            existing = self.connection.execute(
                "SELECT journal_seq, sha256, payload FROM journal_transactions WHERE transaction_id=?",
                (transaction.transaction_id,),
            ).fetchone()
            if existing is not None:
                if existing["sha256"] != digest or existing["payload"] != payload:
                    raise JournalConflictError("transaction ID was reused with different contents")
                if before_commit is not None:
                    before_commit(existing["journal_seq"])
                self._commit()
                return existing["journal_seq"]
            if transaction.cursor_before != head["cursor"]:
                raise JournalConflictError("stale processing cursor; reload committed state before retrying")
            sequence = head["head_seq"] + 1
            ingress = head["last_ingress_seq"]
            identities = set()
            for event in transaction.events:
                if event.sequence <= ingress or event.event_id in identities:
                    raise JournalConflictError("events must preserve unique, increasing ingress order")
                if self.connection.execute(
                    "SELECT 1 FROM journal_events WHERE event_id=?", (event.event_id,)
                ).fetchone():
                    raise JournalConflictError("event identity has already been committed")
                identities.add(event.event_id)
                ingress = event.sequence
            for key in keys:
                if self.contains_trade(key):
                    raise DuplicateFactError("scoped trade fact has already been committed")
            control = transaction.control_record
            if control is not None:
                previous = self.load_control_record()
                if control.journal_seq != sequence:
                    raise JournalConflictError("control record must bind the new journal sequence")
                if previous is not None and control.epoch.epoch <= previous.epoch.epoch:
                    raise JournalConflictError("control epoch must advance monotonically")
            self.connection.execute(
                "INSERT INTO journal_transactions VALUES (?, ?, ?, ?, ?, ?)",
                (
                    sequence,
                    transaction.transaction_id,
                    payload,
                    digest,
                    transaction.cursor_before,
                    transaction.cursor_after,
                ),
            )
            for ordinal, event in enumerate(transaction.events):
                encoded = codec.dumps(event)
                self.connection.execute(
                    "INSERT INTO journal_events VALUES (?, ?, ?, ?, ?, ?)",
                    (sequence, ordinal, event.event_id, event.sequence, encoded, _hash(encoded)),
                )
            for name, value in transaction.state_updates.items():
                encoded = codec.dumps(value)
                self.connection.execute(
                    """INSERT INTO journal_state VALUES (?, ?, ?, ?)
                       ON CONFLICT(state_key) DO UPDATE SET payload=excluded.payload, sha256=excluded.sha256,
                       journal_seq=excluded.journal_seq""",
                    (name, encoded, _hash(encoded), sequence),
                )
            for key in keys:
                encoded = codec.dumps(key)
                self.connection.execute(
                    "INSERT INTO journal_trade_keys VALUES (?, ?, ?)", (_hash(encoded), encoded, sequence)
                )
            if control is not None:
                encoded = codec.dumps(control)
                self.connection.execute(
                    """INSERT INTO journal_control VALUES (1, ?, ?, ?)
                       ON CONFLICT(singleton) DO UPDATE SET payload=excluded.payload, sha256=excluded.sha256,
                       journal_seq=excluded.journal_seq""",
                    (encoded, _hash(encoded), sequence),
                )
            self.connection.execute(
                "UPDATE journal_meta SET head_seq=?, cursor=?, last_ingress_seq=? WHERE singleton=1",
                (sequence, transaction.cursor_after, ingress),
            )
            if before_commit is not None:
                before_commit(sequence)
            self._commit()
            return sequence
        except Exception:
            self.connection.rollback()
            raise

    def load_state(self) -> Mapping[str, object]:
        self._head()
        rows = self.connection.execute(
            "SELECT state_key, payload, sha256 FROM journal_state ORDER BY state_key"
        ).fetchall()
        return freeze_payload({row["state_key"]: self._read(row["payload"], row["sha256"]) for row in rows})

    def load_control_record(self) -> ControlRecord | None:
        head = self._head()
        row = self.connection.execute("SELECT * FROM journal_control WHERE singleton=1").fetchone()
        if row is None:
            return None
        record = self._read(row["payload"], row["sha256"])
        if (
            not isinstance(record, ControlRecord)
            or record.journal_seq != row["journal_seq"]
            or record.journal_seq > head["head_seq"]
        ):
            raise JournalCorruptionError("invalid persisted control record")
        return record

    def replay_from(self, seq: int) -> Iterator[CanonicalEvent]:
        require_int(seq, "journal sequence")
        head = self._head()["head_seq"]
        if seq > head:
            raise JournalConflictError("replay cursor is beyond committed journal history")
        rows = self.connection.execute(
            "SELECT payload, sha256 FROM journal_events WHERE journal_seq>? AND journal_seq<=? "
            "ORDER BY journal_seq, ordinal",
            (seq, head),
        ).fetchall()
        events = []
        for row in rows:
            event = self._read(row["payload"], row["sha256"])
            if not isinstance(event, CanonicalEvent):
                raise JournalCorruptionError("replay entry is not a canonical event")
            events.append(event)
        return iter(events)

    def snapshot(self, seq: int) -> None:
        require_int(seq, "snapshot sequence")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if seq > self._head()["head_seq"]:
                raise JournalConflictError("snapshot cursor is beyond committed history")
            state: dict[str, object] = {}
            keys: set[TradeKey] = set()
            control = None
            cursor = 0
            rows = self.connection.execute(
                "SELECT payload, sha256 FROM journal_transactions WHERE journal_seq<=? ORDER BY journal_seq", (seq,)
            ).fetchall()
            for row in rows:
                transaction = self._read(row["payload"], row["sha256"])
                if not isinstance(transaction, JournalTransaction) or transaction.cursor_before != cursor:
                    raise JournalCorruptionError("transaction history does not form a contiguous processing cursor")
                state.update(transaction.state_updates)
                keys.update(transaction.deduplication_keys)
                if transaction.control_record is not None:
                    control = transaction.control_record
                cursor = transaction.cursor_after
            payload = codec.dumps(
                {
                    "account_id": self.account_id,
                    "journal_seq": seq,
                    "cursor": cursor,
                    "state": state,
                    "deduplication_keys": frozenset(keys),
                    "control_record": control,
                }
            )
            previous = self.connection.execute(
                "SELECT payload, sha256 FROM journal_snapshots WHERE journal_seq=?", (seq,)
            ).fetchone()
            if previous is not None:
                if previous["payload"] != payload or previous["sha256"] != _hash(payload):
                    raise JournalCorruptionError("existing snapshot does not match committed history")
            else:
                self.connection.execute(
                    "INSERT INTO journal_snapshots VALUES (?, ?, ?)", (seq, payload, _hash(payload))
                )
            self._commit()
        except Exception:
            self.connection.rollback()
            raise

    def load_snapshot(self, seq: int | None = None) -> JournalSnapshot | None:
        head = self._head()["head_seq"]
        if seq is None:
            seq = head
        require_int(seq, "snapshot sequence")
        if seq > head:
            raise JournalConflictError("snapshot cursor is beyond committed history")
        row = self.connection.execute(
            "SELECT * FROM journal_snapshots WHERE journal_seq<=? ORDER BY journal_seq DESC LIMIT 1", (seq,)
        ).fetchone()
        if row is None:
            return None
        snapshot = JournalSnapshot(**self._read(row["payload"], row["sha256"]))
        if snapshot.journal_seq != row["journal_seq"] or snapshot.account_id != self.account_id:
            raise JournalCorruptionError("snapshot identity is inconsistent")
        # This is a historical view only; it never rewinds live state or the current control epoch.
        return snapshot

    def load_checkpoint(self) -> JournalSnapshot:
        """Read projections, cursor, deduplication and current control from one SQLite read snapshot."""
        self.connection.execute("BEGIN")
        try:
            head = self._head()
            state = self.load_state()
            rows = self.connection.execute("SELECT payload, key_hash FROM journal_trade_keys").fetchall()
            keys = frozenset(self._read(row["payload"], row["key_hash"]) for row in rows)
            checkpoint = JournalSnapshot(
                self.account_id, head["head_seq"], head["cursor"], state, keys, self.load_control_record()
            )
            self._commit()
            return checkpoint
        except Exception:
            self.connection.rollback()
            raise
