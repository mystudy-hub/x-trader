"""[Infrastructure 层] 策略游标独立 SQLite 存储与进程独占锁 (S5-05, FR-REC-02).

只存规范 Mapping；Engine 数据类由装配层转换，避免基础设施反向依赖引擎。
使用独立数据库，不能写入执行服务的交易日志或命令表。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import threading
from collections.abc import Mapping
from pathlib import Path

from qh_trader.core.constants import JournalCorruptionError
from qh_trader.core.execution import ExecutionOwnershipError
from qh_trader.core.objects import freeze_payload, require_text
from qh_trader.infrastructure import journal_codec


class _StrategyLock:
    """操作系统锁随进程退出释放；锁文件不能删除或替换。"""

    def __init__(self, path: Path) -> None:
        self._file = path.with_name(path.name + "-strategy.lock").open("a+b")
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
            raise ExecutionOwnershipError("another producer owns this strategy checkpoint database") from exc

    def close(self) -> None:
        self._file.close()


class SQLiteStrategyCheckpointStore:
    """整个数据库在生产者存活期间独占，save 采用 SQLite FULL 原子事务。

    load(stream_id) / save(stream_id, payload) 交换仅含 Core 值的 Mapping。
    建议每个策略配置单独命名数据库；关闭或进程退出后方可重启接管。
    """

    def __init__(self, database: Path | str) -> None:
        path = Path(database).resolve()
        if str(path).startswith(("\\\\", "//")) or path.name.casefold() == "trading.db":
            raise ValueError("strategy checkpoints require a separate local database")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = _StrategyLock(path)
        self._thread = threading.get_ident()
        self._closed = False
        self._connection: sqlite3.Connection | None = None
        try:
            self._connection = sqlite3.connect(path, isolation_level=None)
            self._connection.row_factory = sqlite3.Row
            tables = {row[0] for row in self._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables - {"strategy_checkpoints"}:
                raise ValueError("strategy checkpoint database contains unrelated tables")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute(
                """CREATE TABLE IF NOT EXISTS strategy_checkpoints (
                    stream_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    sha256 TEXT NOT NULL)"""
            )
        except Exception:
            if self._connection is not None:
                self._connection.close()
            self._lock.close()
            self._closed = True
            raise

    def __enter__(self) -> SQLiteStrategyCheckpointStore:
        self._assert_owner()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _assert_owner(self) -> None:
        if self._closed or threading.get_ident() != self._thread:
            raise ExecutionOwnershipError("strategy checkpoint store requires its owning thread")

    def close(self) -> None:
        if self._closed:
            return
        self._assert_owner()
        try:
            self._connection.close()
        finally:
            self._lock.close()
            self._closed = True

    def load(self, stream_id: str) -> Mapping[str, object] | None:
        self._assert_owner()
        require_text(stream_id, "stream_id")
        row = self._connection.execute(
            "SELECT payload, sha256 FROM strategy_checkpoints WHERE stream_id=?", (stream_id,)
        ).fetchone()
        if row is None:
            return None
        if hashlib.sha256(row["payload"].encode()).hexdigest() != row["sha256"]:
            raise JournalCorruptionError("strategy checkpoint checksum mismatch")
        try:
            value = journal_codec.loads(row["payload"])
        except (TypeError, ValueError, KeyError) as exc:
            raise JournalCorruptionError("invalid strategy checkpoint payload") from exc
        if not isinstance(value, Mapping) or value.get("stream_id") != stream_id:
            raise JournalCorruptionError("strategy checkpoint stream differs from its payload")
        return freeze_payload(value)

    def save(self, stream_id: str, payload: Mapping[str, object]) -> None:
        self._assert_owner()
        require_text(stream_id, "stream_id")
        if not isinstance(payload, Mapping) or payload.get("stream_id") != stream_id:
            raise ValueError("checkpoint payload must identify its strategy stream")
        encoded = journal_codec.dumps(freeze_payload(payload))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(
                """INSERT INTO strategy_checkpoints(stream_id, payload, sha256) VALUES (?, ?, ?)
                   ON CONFLICT(stream_id) DO UPDATE SET payload=excluded.payload, sha256=excluded.sha256""",
                (stream_id, encoded, digest),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
