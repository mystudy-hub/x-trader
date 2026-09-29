"""[Infrastructure 层] 独立策略游标、确定性意图清单与耐久 outbox (S5-05, ADR-X1, A23).

交易事件仍唯一保存在交易 Journal。本库仅保存事件摘要和策略产生的命令；先落盘意图再 INSERT
交易命令表，跨库崩溃窗口用相同 command_id 重投闭合。每个策略状态库只允许一个进程使用。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from qh_trader.core.constants import JournalConflictError, JournalCorruptionError
from qh_trader.core.event import CanonicalEvent
from qh_trader.core.execution import ExecutionCommand, ExecutionOwnershipError
from qh_trader.core.objects import freeze_payload, require_int
from qh_trader.infrastructure import journal_codec


def _digest(value: object) -> str:
    return hashlib.sha256(journal_codec.dumps(value).encode("utf-8")).hexdigest()


class SQLiteStrategyRuntimeStore:
    """FULL + WAL 的策略私有库；metadata 创建后不可改，重启必须精确匹配."""

    def __init__(
        self,
        path: Path | str,
        *,
        metadata: Mapping[str, object],
        initialize: Callable[[], Mapping[str, object]] | None = None,
    ) -> None:
        path = Path(path).resolve()
        if str(path).startswith(("\\\\", "//")):
            raise ValueError("strategy runtime requires a local database")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = path.with_name(path.name + "-strategy.lock").open("a+b")
        self._connection: sqlite3.Connection | None = None
        try:
            if self._lock.seek(0, os.SEEK_END) == 0:
                self._lock.write(b"\0")
                self._lock.flush()
            self._lock.seek(0)
            try:
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(self._lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ExecutionOwnershipError("another strategy process owns this runtime database") from exc
            connection = sqlite3.connect(path, isolation_level=None)
            self._connection = connection
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS strategy_meta "
                "(singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload TEXT NOT NULL, "
                "sha256 TEXT NOT NULL, cursor INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS strategy_callbacks "
                "(event_id TEXT PRIMARY KEY, ingress_sequence INTEGER UNIQUE NOT NULL, "
                "event_sha256 TEXT NOT NULL, commands TEXT NOT NULL, commands_sha256 TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS strategy_outbox "
                "(command_id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES strategy_callbacks(event_id), "
                "ordinal INTEGER NOT NULL, payload TEXT NOT NULL, sha256 TEXT NOT NULL, "
                "delivered INTEGER NOT NULL DEFAULT 0 CHECK(delivered IN (0,1)))"
            )
            row = connection.execute("SELECT * FROM strategy_meta WHERE singleton=1").fetchone()
            if row is None:
                # 空文件或建表后崩溃都仍是首次初始化；保护在持锁后、写入 metadata 前执行。
                if initialize is not None:
                    metadata = initialize()
                require_int(metadata["start_cursor"], "strategy initial journal cursor")
                encoded = journal_codec.dumps(metadata)
                connection.execute(
                    "INSERT INTO strategy_meta VALUES (1, ?, ?, ?)",
                    (encoded, _digest(metadata), metadata["start_cursor"]),
                )
                self.metadata = freeze_payload(metadata)
            else:
                self.metadata = self._read(row["payload"], row["sha256"])
                # start_cursor 属于首次启动记录；重新启动时只检查配置和固定控制代次。
                expected = {key: value for key, value in metadata.items() if key != "start_cursor"}
                actual = {key: value for key, value in self.metadata.items() if key != "start_cursor"}
                if expected != actual:
                    raise JournalConflictError(
                        "strategy runtime binding/configuration changed; explicit migration required"
                    )
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> SQLiteStrategyRuntimeStore:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        self._lock.close()

    @staticmethod
    def _read(payload: str, digest: str) -> object:
        if hashlib.sha256(payload.encode("utf-8")).hexdigest() != digest:
            raise JournalCorruptionError("strategy runtime checksum mismatch")
        return journal_codec.loads(payload)

    @property
    def cursor(self) -> int:
        return self._connection.execute("SELECT cursor FROM strategy_meta WHERE singleton=1").fetchone()[0]

    @property
    def last_sequence(self) -> int:
        value = self._connection.execute("SELECT MAX(ingress_sequence) FROM strategy_callbacks").fetchone()[0]
        return 0 if value is None else value

    def recorded(self, event: CanonicalEvent) -> tuple[ExecutionCommand, ...] | None:
        row = self._connection.execute(
            "SELECT * FROM strategy_callbacks WHERE event_id=?", (event.event_id,)
        ).fetchone()
        if row is None:
            last = self._connection.execute("SELECT MAX(ingress_sequence) FROM strategy_callbacks").fetchone()[0]
            if last is not None and event.sequence <= last:
                raise JournalCorruptionError("strategy callback history has a gap or was reordered")
            return None
        if row["event_sha256"] != _digest(event) or row["ingress_sequence"] != event.sequence:
            raise JournalCorruptionError("strategy callback refers to changed journal history")
        commands = self._read(row["commands"], row["commands_sha256"])
        if not isinstance(commands, tuple) or any(not isinstance(item, ExecutionCommand) for item in commands):
            raise JournalCorruptionError("strategy callback contains invalid commands")
        return commands

    def record(self, event: CanonicalEvent, commands: Sequence[ExecutionCommand]) -> None:
        commands = tuple(commands)
        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            previous = self.recorded(event)
            if previous is not None:
                if previous != commands:
                    raise JournalConflictError("strategy replay changed previously recorded intents")
            else:
                connection.execute(
                    "INSERT INTO strategy_callbacks VALUES (?, ?, ?, ?, ?)",
                    (event.event_id, event.sequence, _digest(event), journal_codec.dumps(commands), _digest(commands)),
                )
                for ordinal, command in enumerate(commands):
                    connection.execute(
                        "INSERT INTO strategy_outbox (command_id,event_id,ordinal,payload,sha256) VALUES (?,?,?,?,?)",
                        (command.command_id, event.event_id, ordinal, journal_codec.dumps(command), _digest(command)),
                    )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def pending(self) -> tuple[ExecutionCommand, ...]:
        rows = self._connection.execute(
            "SELECT o.payload,o.sha256 FROM strategy_outbox o JOIN strategy_callbacks c USING(event_id) "
            "WHERE o.delivered=0 ORDER BY c.ingress_sequence,o.ordinal"
        ).fetchall()
        commands = tuple(self._read(row["payload"], row["sha256"]) for row in rows)
        if any(not isinstance(command, ExecutionCommand) for command in commands):
            raise JournalCorruptionError("strategy outbox contains invalid commands")
        return commands

    def delivered(self, command_id: str) -> None:
        changed = self._connection.execute(
            "UPDATE strategy_outbox SET delivered=1 WHERE command_id=?", (command_id,)
        ).rowcount
        if changed != 1:
            raise JournalConflictError("cannot acknowledge an unknown strategy command")

    def advance(self, cursor: int) -> None:
        require_int(cursor, "strategy journal cursor", self.cursor)
        if self.pending():
            raise JournalConflictError("strategy cursor cannot pass undelivered intents")
        self._connection.execute("UPDATE strategy_meta SET cursor=? WHERE singleton=1", (cursor,))
