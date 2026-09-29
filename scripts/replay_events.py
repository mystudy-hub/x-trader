#!/usr/bin/env python
"""[Scripts 层] 从 SQLite 快照游标离线重放并核对交易投影 (S5-10, FR-REC-03/05, A04/A14/A18).

先用 SQLite backup API 读取一致性临时副本，再验证事务链、快照与事件表。
本工具只复核已有的 Journal 事实与投影，不连接柜台、不执行命令，不冒充实时验收。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.core.constants import JournalConflictError  # noqa: E402
from qh_trader.core.event import CanonicalEvent, JournalSnapshot, JournalTransaction  # noqa: E402
from qh_trader.infrastructure import journal_codec  # noqa: E402
from qh_trader.infrastructure.memory_journal import MemoryJournal  # noqa: E402
from scripts.backup_state import RESTORE_GUARD_TABLE, _copy_sqlite, _digest, _local_path, _write_json  # noqa: E402


class ReplayError(ValueError):
    """输入损坏或无法建立可靠重放顺序。"""


def _decode(row: sqlite3.Row, *, digest: str = "sha256") -> object:
    payload = row["payload"]
    if hashlib.sha256(payload.encode("utf-8")).hexdigest() != row[digest]:
        raise ReplayError("journal payload checksum mismatch")
    return journal_codec.loads(payload)


def _fingerprint(value: object) -> str:
    return hashlib.sha256(journal_codec.dumps(value).encode("utf-8")).hexdigest()


def _difference(differences: list[dict], field: str, expected: object, actual: object) -> None:
    if expected != actual:
        differences.append(
            {"field": field, "expected_sha256": _fingerprint(expected), "actual_sha256": _fingerprint(actual)}
        )


def _compare_snapshots(
    differences: list[dict], prefix: str, expected: JournalSnapshot, actual: JournalSnapshot
) -> None:
    for field in ("account_id", "journal_seq", "cursor", "deduplication_keys", "control_record"):
        _difference(differences, prefix + field, getattr(expected, field), getattr(actual, field))
    for key in sorted(set(expected.state) | set(actual.state)):
        # 使用存在性区分无投影与合法 None；删除语义由 JournalSnapshot.advance 负责。
        _difference(
            differences,
            prefix + "state." + key,
            (key in expected.state, expected.state.get(key)),
            (key in actual.state, actual.state.get(key)),
        )


def _replay_snapshot(database: Path, account_id: str, snapshot_seq: int | None) -> dict:
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
            raise ReplayError("SQLite integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise ReplayError("SQLite foreign key check failed")
        if [row[0] for row in connection.execute("SELECT version FROM journal_schema")] != [1]:
            raise ReplayError("unsupported journal schema version")
        metas = connection.execute("SELECT * FROM journal_meta").fetchall()
        if len(metas) != 1 or metas[0]["singleton"] != 1 or metas[0]["account_id"] != account_id:
            raise ReplayError("journal does not belong to the requested account")
        meta = metas[0]
        snapshots = {}
        for row in connection.execute("SELECT * FROM journal_snapshots ORDER BY journal_seq"):
            value = _decode(row)
            snapshot = JournalSnapshot(**value)
            if snapshot.journal_seq != row["journal_seq"] or snapshot.account_id != account_id:
                raise ReplayError("snapshot identity differs from its index")
            if snapshot.journal_seq > meta["head_seq"]:
                raise ReplayError("snapshot is beyond committed journal history")
            snapshots[snapshot.journal_seq] = snapshot
        if snapshot_seq is None:
            start = max(snapshots, default=0)
        elif type(snapshot_seq) is not int or snapshot_seq < 0 or snapshot_seq > meta["head_seq"]:
            raise ReplayError("snapshot sequence is outside committed history")
        else:
            start = snapshot_seq
        if start and start not in snapshots:
            raise ReplayError("requested snapshot does not exist")
        empty = JournalSnapshot(account_id, 0, 0, {}, frozenset(), None)
        expected, replayed = empty, snapshots[start] if start else empty
        validator = MemoryJournal(account_id)
        differences: list[dict] = []
        if 0 in snapshots:
            _compare_snapshots(differences, "snapshot[0].", empty, snapshots[0])
        event_count, last_ingress = 0, -1
        for row in connection.execute("SELECT * FROM journal_transactions ORDER BY journal_seq"):
            sequence = row["journal_seq"]
            transaction = _decode(row)
            if not isinstance(transaction, JournalTransaction):
                raise ReplayError("transaction payload is not a JournalTransaction")
            if sequence != expected.journal_seq + 1:
                raise ReplayError("journal transaction sequence has a gap")
            if (
                transaction.transaction_id != row["transaction_id"]
                or transaction.cursor_before != row["cursor_before"]
                or transaction.cursor_after != row["cursor_after"]
            ):
                raise ReplayError("transaction index differs from payload")
            # 共用 JournalPort 守卫验证账户、游标、事件序、成交去重及控制代次单调。
            if validator.append(transaction) != sequence:
                raise ReplayError("transaction identity was reused")
            events = connection.execute(
                "SELECT * FROM journal_events WHERE journal_seq=? ORDER BY ordinal", (sequence,)
            ).fetchall()
            if len(events) != len(transaction.events):
                raise ReplayError("transaction events differ from the event table")
            for ordinal, (event_row, expected_event) in enumerate(zip(events, transaction.events, strict=True)):
                event = _decode(event_row)
                if (
                    not isinstance(event, CanonicalEvent)
                    or event != expected_event
                    or event_row["ordinal"] != ordinal
                    or event_row["event_id"] != event.event_id
                    or event_row["ingress_seq"] != event.sequence
                ):
                    raise ReplayError("event index or payload differs from its transaction")
                last_ingress = event.sequence
                event_count += 1
            expected = expected.advance(sequence, transaction)
            if sequence in snapshots:
                _compare_snapshots(differences, f"snapshot[{sequence}].", expected, snapshots[sequence])
            if sequence > start:
                replayed = replayed.advance(sequence, transaction)
        if expected.journal_seq != meta["head_seq"]:
            raise ReplayError("journal head differs from committed transaction history")
        if event_count != connection.execute("SELECT count(*) FROM journal_events").fetchone()[0]:
            raise ReplayError("orphan journal events exist")
        state = {}
        for row in connection.execute("SELECT * FROM journal_state ORDER BY state_key"):
            if row["journal_seq"] > meta["head_seq"]:
                raise ReplayError("projection is beyond committed journal history")
            state[row["state_key"]] = _decode(row)
        keys = frozenset(
            _decode(row, digest="key_hash") for row in connection.execute("SELECT * FROM journal_trade_keys")
        )
        controls = connection.execute("SELECT * FROM journal_control").fetchall()
        if len(controls) > 1:
            raise ReplayError("multiple control records exist")
        control = _decode(controls[0]) if controls else None
        if control is not None and control.journal_seq != controls[0]["journal_seq"]:
            raise ReplayError("control record index differs from payload")
        materialized = JournalSnapshot(account_id, meta["head_seq"], meta["cursor"], state, keys, control)
        _compare_snapshots(differences, "projection.", expected, materialized)
        _compare_snapshots(differences, "replayed.", expected, replayed)
        _difference(differences, "last_ingress_seq", last_ingress, meta["last_ingress_seq"])
        return {
            "format": "qh-trader-journal-replay",
            "version": 1,
            "evidence_type": "historical_replay",
            "validation_scope": "journal_facts_and_projections",
            "account_id": account_id,
            "snapshot_seq": start,
            "head_seq": expected.journal_seq,
            "cursor": expected.cursor,
            "validated_transactions": expected.journal_seq,
            "replayed_transactions": expected.journal_seq - start,
            "validated_events": event_count,
            "validated_snapshots": len(snapshots),
            "consistent": not differences,
            "differences": differences,
            "offline_restore": bool(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (RESTORE_GUARD_TABLE,)
                ).fetchone()
            ),
            "trading_authorized": False,
        }


def replay_events(journal: Path | str, account_id: str, *, snapshot_seq: int | None = None) -> dict:
    source = _local_path(journal)
    if not source.is_file():
        raise ReplayError("source journal does not exist")
    with TemporaryDirectory(prefix="qh-offline-replay-") as workspace:
        snapshot = Path(workspace) / "snapshot.db"
        _copy_sqlite(source, snapshot)
        try:
            report = _replay_snapshot(snapshot, account_id, snapshot_seq)
        except (JournalConflictError, TypeError, KeyError, AttributeError) as exc:
            raise ReplayError(f"journal replay contract failed: {exc}") from exc
        report["input_sha256"] = _digest(snapshot)
        report["created_utc"] = datetime.now(timezone.utc).isoformat()
        return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="离线历史 Journal 重放与投影核对；不连接交易端口")
    parser.add_argument("--journal", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--snapshot-seq", type=int, default=None, help="已保存的快照序号；0 从头，默认最新快照")
    parser.add_argument("--out", required=True, help="新的 JSON 证据文件，不覆盖已有文件")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = replay_events(args.journal, args.account, snapshot_seq=args.snapshot_seq)
        output = _local_path(args.out)
        output.parent.mkdir(parents=True, exist_ok=True)
        _write_json(output, report)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["consistent"] else 1
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        print(f"离线重放失败: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
