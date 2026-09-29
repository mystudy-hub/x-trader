#!/usr/bin/env python
"""[Scripts 层] SQLite 整库一致性备份、校验与隔离恢复 (S5-09, NFR-04, A14/A18/A23).

backup --journal PATH --out NEW_DIR
verify --backup DIR
restore --backup DIR --out NEW_DIR

命令和 Journal 共用同一交易库。备份读取已提交的 WAL 快照，不复制活动主文件。
恢复只发布带数据库内隔离标记的离线副本，不证明最新控制代次，也不启动交易服务。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.core.constants import JournalCorruptionError  # noqa: E402
from qh_trader.core.event import CanonicalEvent, JournalTransaction  # noqa: E402
from qh_trader.core.objects import ControlRecord  # noqa: E402
from qh_trader.infrastructure import journal_codec  # noqa: E402
from qh_trader.infrastructure.command_queue import _command  # noqa: E402

FORMAT = "qh-trader-state-backup"
VERSION = 1
DATABASE = "trading.db"
MANIFEST = "manifest.json"
RESTORE_GUARD_TABLE = "qh_offline_restore_guard"
REQUIRED_TABLES = {
    "journal_schema",
    "journal_meta",
    "journal_transactions",
    "journal_events",
    "journal_state",
    "journal_trade_keys",
    "journal_control",
    "journal_snapshots",
    "command_queue",
}
HASH_TABLES = {
    "journal_transactions": "sha256",
    "journal_events": "sha256",
    "journal_state": "sha256",
    "journal_trade_keys": "key_hash",
    "journal_control": "sha256",
    "journal_snapshots": "sha256",
}


class BackupError(ValueError):
    """备份、发布或校验条件不成立，不能作为成功的恢复证据。"""


def _local_path(value: Path | str) -> Path:
    path = Path(value).expanduser().resolve()
    if str(path).startswith(("\\\\", "//")):
        raise BackupError("backup and restore require a local filesystem")
    return path


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _sync(path: Path) -> None:
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _sync_directory(path: Path) -> None:
    # Windows 的文件 fsync 可用；目录 flush 没有 Python 标准库接口。
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@contextmanager
def _new_directory(path: Path) -> Iterator[Path]:
    """mkdir 是排他发布权；manifest 最后落盘，残缺目录不构成有效备份。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir()
    except FileExistsError as exc:
        raise BackupError("destination must be a new directory; existing state is never overwritten") from exc
    try:
        yield path
        _sync_directory(path)
        _sync_directory(path.parent)
    except BaseException:
        # 只清理本次独占创建的目录；异常中断若留下残件，verify 会拒绝无清单工件。
        for name in (DATABASE, DATABASE + ".partial", MANIFEST, MANIFEST + ".partial", "restore.json"):
            for suffix in ("", "-wal", "-shm", "-journal"):
                candidate = path / (name + suffix)
                if candidate.is_file() and not candidate.is_symlink():
                    candidate.unlink()
        try:
            path.rmdir()
        except OSError:
            pass
        raise


def _write_json(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _inspect_database(path: Path) -> dict:
    """校验静止单文件工件；immutable 避免校验器创建 WAL 或 SHM。"""
    if not path.is_file() or path.is_symlink():
        raise BackupError("backup database must be a regular file")
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise BackupError("backup database must be self-contained, without SQLite sidecars")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
            raise BackupError("SQLite integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError("SQLite foreign key check failed")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not REQUIRED_TABLES <= tables or "rule_store_schema" in tables:
            raise BackupError("backup requires journal and command queue in one trading database")
        versions = [row[0] for row in connection.execute("SELECT version FROM journal_schema")]
        if versions != [1]:
            raise BackupError("unsupported journal schema version")
        meta_rows = connection.execute("SELECT * FROM journal_meta").fetchall()
        if len(meta_rows) != 1 or meta_rows[0]["singleton"] != 1:
            raise BackupError("invalid journal metadata")
        meta = meta_rows[0]
        counts = {}
        for table in sorted(tables):
            quoted = '"' + table.replace('"', '""') + '"'
            counts[table] = connection.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0]
        if counts["journal_transactions"] != meta["head_seq"]:
            raise BackupError("journal head differs from transaction history")
        bounds = connection.execute("SELECT min(journal_seq), max(journal_seq) FROM journal_transactions").fetchone()
        if meta["head_seq"] and (bounds[0] != 1 or bounds[1] != meta["head_seq"]):
            raise BackupError("journal transaction indexes are not contiguous")
        last = connection.execute(
            "SELECT cursor_after FROM journal_transactions ORDER BY journal_seq DESC LIMIT 1"
        ).fetchone()
        if meta["cursor"] != (last[0] if last else 0):
            raise BackupError("journal cursor differs from the latest transaction")
        last_ingress = connection.execute("SELECT max(ingress_seq) FROM journal_events").fetchone()[0]
        if meta["last_ingress_seq"] != (-1 if last_ingress is None else last_ingress):
            raise BackupError("journal ingress metadata differs from the event index")
        for table, column in HASH_TABLES.items():
            for row in connection.execute(f"SELECT * FROM {table}"):
                if hashlib.sha256(row["payload"].encode("utf-8")).hexdigest() != row[column]:
                    raise BackupError(f"payload checksum mismatch in {table}")
                value = journal_codec.loads(row["payload"])
                if table == "journal_transactions" and (
                    not isinstance(value, JournalTransaction)
                    or value.transaction_id != row["transaction_id"]
                    or value.cursor_before != row["cursor_before"]
                    or value.cursor_after != row["cursor_after"]
                ):
                    raise BackupError("transaction index differs from its payload")
                if table == "journal_events" and (
                    not isinstance(value, CanonicalEvent)
                    or value.event_id != row["event_id"]
                    or value.sequence != row["ingress_seq"]
                ):
                    raise BackupError("event index differs from its payload")
        for row in connection.execute("SELECT * FROM command_queue"):
            try:
                _command(row, meta["account_id"])
            except (JournalCorruptionError, IndexError) as exc:
                raise BackupError("invalid command queue index or payload") from exc
        controls = connection.execute("SELECT * FROM journal_control").fetchall()
        control = None
        if controls:
            value = journal_codec.loads(controls[0]["payload"])
            if (
                len(controls) != 1
                or controls[0]["singleton"] != 1
                or not isinstance(value, ControlRecord)
                or value.journal_seq != controls[0]["journal_seq"]
                or value.journal_seq > meta["head_seq"]
            ):
                raise BackupError("invalid journal control record")
            control_transaction = connection.execute(
                "SELECT payload FROM journal_transactions WHERE journal_seq=?", (value.journal_seq,)
            ).fetchone()
            if control_transaction is None or journal_codec.loads(control_transaction[0]).control_record != value:
                raise BackupError("control record differs from its transaction")
            control = {
                "controller_id": value.epoch.controller_id,
                "epoch": value.epoch.epoch,
                "journal_seq": value.journal_seq,
            }
        return {
            "journal": {
                "schema_version": 1,
                "account_id": meta["account_id"],
                "head_seq": meta["head_seq"],
                "cursor": meta["cursor"],
                "last_ingress_seq": meta["last_ingress_seq"],
                "control": control,
            },
            "tables": counts,
        }


def _copy_sqlite(source: Path, destination: Path, *, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout

    def progress(status: int, remaining: int, total: int) -> None:
        if time.monotonic() > deadline:
            raise BackupError("SQLite online backup exceeded its deadline")

    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(destination)) as snapshot:
            original.backup(snapshot, pages=256, progress=progress, sleep=0.05)
            # 将 WAL 快照封闭为一个独立文件；恢复后的 Journal 自行启用 WAL + FULL。
            if snapshot.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise BackupError("could not seal the SQLite snapshot")
    _sync(destination)


def backup_state(journal: Path | str, output: Path | str) -> dict:
    source, destination = _local_path(journal), _local_path(output)
    if not source.is_file():
        raise BackupError("source trading database does not exist")
    with _new_directory(destination):
        partial = destination / (DATABASE + ".partial")
        _copy_sqlite(source, partial)
        details = _inspect_database(partial)
        manifest = {
            "format": FORMAT,
            "version": VERSION,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "sqlite_version": sqlite3.sqlite_version,
            "database": {"file": DATABASE, "bytes": partial.stat().st_size, "sha256": _digest(partial)},
            **details,
        }
        partial.rename(destination / DATABASE)
        _write_json(destination / (MANIFEST + ".partial"), manifest)
        (destination / (MANIFEST + ".partial")).rename(destination / MANIFEST)
    return manifest


def verify_backup(backup: Path | str) -> dict:
    directory = _local_path(backup)
    manifest_path = directory / MANIFEST
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise BackupError("backup manifest is missing or is not a regular file")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"format", "version", "created_utc", "sqlite_version", "database", "journal", "tables"}
            or manifest["format"] != FORMAT
            or type(manifest["version"]) is not int
            or manifest["version"] != VERSION
        ):
            raise BackupError("unsupported backup manifest format or version")
        timestamp = datetime.fromisoformat(manifest["created_utc"])
        if timestamp.tzinfo is None:
            raise BackupError("backup timestamp requires a timezone")
        info = manifest["database"]
        if not isinstance(info, dict) or set(info) != {"file", "bytes", "sha256"} or info["file"] != DATABASE:
            raise BackupError("invalid backup database descriptor")
        database = directory / DATABASE
        if not database.is_file() or database.is_symlink():
            raise BackupError("backup database is missing or is not a regular file")
        if database.stat().st_size != info["bytes"] or _digest(database) != info["sha256"]:
            raise BackupError("backup database size or SHA256 mismatch")
        details = _inspect_database(database)
        if details["journal"] != manifest["journal"] or details["tables"] != manifest["tables"]:
            raise BackupError("backup manifest metadata differs from the database")
        return manifest
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise BackupError("malformed backup manifest") from exc


def restore_state(backup: Path | str, output: Path | str) -> dict:
    directory, destination = _local_path(backup), _local_path(output)
    manifest = verify_backup(directory)
    with _new_directory(destination):
        partial = destination / (DATABASE + ".partial")
        with (directory / DATABASE).open("rb") as source, partial.open("xb") as target:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        # 再校验复制的字节，避免校验后源文件变化仍被发布为成功。
        if (
            partial.stat().st_size != manifest["database"]["bytes"]
            or _digest(partial) != manifest["database"]["sha256"]
        ):
            raise BackupError("backup changed while restoring")
        details = _inspect_database(partial)
        if details["journal"] != manifest["journal"] or details["tables"] != manifest["tables"]:
            raise BackupError("restored database metadata differs from backup")
        restored_at = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(partial)) as connection:
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS {RESTORE_GUARD_TABLE} "
                "(backup_sha256 TEXT NOT NULL, restored_at TEXT NOT NULL, reason TEXT NOT NULL)"
            )
            connection.execute(
                f"INSERT INTO {RESTORE_GUARD_TABLE} VALUES (?, ?, ?)",
                (manifest["database"]["sha256"], restored_at, "latest_control_epoch_unverified"),
            )
            connection.commit()
        _inspect_database(partial)
        _sync(partial)
        receipt = {
            "format": "qh-trader-offline-restore",
            "version": VERSION,
            "restored_utc": restored_at,
            "backup_sha256": manifest["database"]["sha256"],
            "database_sha256": _digest(partial),
            "journal": manifest["journal"],
            "offline_only": True,
            "trading_authorized": False,
            "guard_table": RESTORE_GUARD_TABLE,
        }
        partial.rename(destination / DATABASE)
        _write_json(destination / "restore.json", receipt)
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="交易库一致性备份、校验和隔离恢复；不启动交易服务")
    actions = parser.add_subparsers(dest="action", required=True)
    create = actions.add_parser("backup", help="在线读取已提交的 SQLite WAL 状态")
    create.add_argument("--journal", required=True)
    create.add_argument("--out", required=True, help="必须不存在的新备份目录")
    verify = actions.add_parser("verify", help="校验清单、SHA256、数据库完整性及载荷摘要")
    verify.add_argument("--backup", required=True)
    restore = actions.add_parser("restore", help="恢复到带数据库内隔离标记的新离线目录")
    restore.add_argument("--backup", required=True)
    restore.add_argument("--out", required=True, help="必须不存在的新恢复目录")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "backup":
            result = backup_state(args.journal, args.out)
        elif args.action == "verify":
            result = verify_backup(args.backup)
        else:
            result = restore_state(args.backup, args.out)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        print(f"备份/恢复失败: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
