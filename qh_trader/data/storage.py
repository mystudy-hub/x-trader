"""Content-addressed Parquet versions with one atomic manifest commit point."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from qh_trader.core.constants import QualityFlag
from qh_trader.core.objects import Bar, ExecutionReference, InstrumentId, Settlement
from qh_trader.data.schemas import (
    arrow_table_to_bars,
    arrow_table_to_execution_references,
    arrow_table_to_settlements,
    bars_to_arrow_table,
    execution_references_to_arrow_table,
    settlements_to_arrow_table,
)
from qh_trader.data.snapshots import DataSnapshot

INTERVALS = {"1m", "5m", "15m", "30m", "1h", "1d"}
CODECS: dict[str, tuple[Callable[[Sequence[Any]], pa.Table], Callable[[pa.Table], list[Any]]]] = {
    "bar": (bars_to_arrow_table, arrow_table_to_bars),
    "settlement": (settlements_to_arrow_table, arrow_table_to_settlements),
    "execution_reference": (execution_references_to_arrow_table, arrow_table_to_execution_references),
}


def normalize_interval(interval: str) -> str:
    normalized = "1h" if interval == "60m" else interval
    if normalized not in INTERVALS:
        raise ValueError("unsupported bar interval")
    return normalized


def safe_filename_for_instrument(instrument: InstrumentId) -> str:
    if not isinstance(instrument, InstrumentId):
        raise TypeError("physical datasets require an actual contract")
    return f"{instrument.exchange.value}_{quote(instrument.symbol, safe='')}.parquet"


def _unfreeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _unfreeze(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_unfreeze(v) for v in value]
    return value


def _json_bytes(value: Any) -> bytes:
    cleaned = _unfreeze(value)
    return json.dumps(cleaned, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class ParquetDataStorage:
    """Single-writer publications, immutable versions and readers bound to a checked manifest."""

    def __init__(self, root_dir: Path | str = "data_storage", compression: str = "ZSTD") -> None:
        self.root_dir = Path(root_dir).resolve()
        self.compression = compression
        # Legacy manifest.json and unversioned files are retained but never read as canonical v2 data.
        self.manifest_path = self.root_dir / "current.json"

    def _path(self, relative: str) -> Path:
        if not isinstance(relative, str) or Path(relative).is_absolute():
            raise ValueError("dataset paths must be repository-relative")
        result = (self.root_dir / relative).resolve()
        if not result.is_relative_to(self.root_dir):
            raise ValueError("dataset path escapes its storage root")
        return result

    @contextmanager
    def _writer(self):
        self.root_dir.mkdir(parents=True, exist_ok=True)
        with (self.root_dir / ".publish.lock").open("a+b") as lock:
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                lock.seek(0)
                if sys.platform == "win32":
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def capture_snapshot(self, snapshot_id: str | None = None) -> DataSnapshot:
        if snapshot_id is None:
            if not self.manifest_path.is_file():
                return DataSnapshot(None, {})
            pointer = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if pointer.get("schema_version") != 2:
                raise ValueError("invalid dataset publication pointer")
            snapshot_id = pointer["snapshot_id"]
        if (
            not isinstance(snapshot_id, str)
            or len(snapshot_id) != 64
            or any(c not in "0123456789abcdef" for c in snapshot_id)
        ):
            raise ValueError("invalid snapshot hash")
        path = self._path(f"manifests/{snapshot_id}.json")
        if not path.is_file() or _hash(path) != snapshot_id:
            raise ValueError("dataset manifest is missing or its hash has changed")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("schema_version") != 2 or not isinstance(manifest.get("datasets"), dict):
            raise ValueError("invalid dataset manifest")
        return DataSnapshot(snapshot_id, manifest["datasets"])

    def _snapshot(self, snapshot: DataSnapshot | str | None) -> DataSnapshot:
        return snapshot if isinstance(snapshot, DataSnapshot) else self.capture_snapshot(snapshot)

    @staticmethod
    def _key(kind: str, instrument: InstrumentId, interval: str) -> str:
        return f"{kind}/{instrument}/{interval}"

    def _read(
        self,
        kind: str,
        instrument: InstrumentId,
        interval: str,
        snapshot: DataSnapshot | str | None = None,
    ) -> list:
        captured = self._snapshot(snapshot)
        entry = captured.datasets.get(self._key(kind, instrument, interval))
        if entry is None:
            return []
        path = self._path(entry["relative_path"])
        if not path.is_file() or _hash(path) != entry["sha256"]:
            raise ValueError("published data is missing or does not match its manifest hash")
        table = pq.read_table(path)
        if table.num_rows != entry["record_count"]:
            raise ValueError("published row count does not match its manifest")
        records = CODECS[kind][1](table)
        self._validate(kind, instrument, interval, records)
        return records

    def read_bars(
        self,
        instrument: InstrumentId,
        interval: str,
        start_time=None,
        end_time=None,
        *,
        snapshot: DataSnapshot | str | None = None,
    ) -> list[Bar]:
        from qh_trader.core.clock import utc_timestamp

        records = self._read("bar", instrument, normalize_interval(interval), snapshot)
        start = utc_timestamp(start_time) if start_time is not None else None
        end = utc_timestamp(end_time) if end_time is not None else None
        return [
            row for row in records if (start is None or row.bar_end > start) and (end is None or row.bar_start < end)
        ]

    def read_settlements(
        self, instrument: InstrumentId, *, snapshot: DataSnapshot | str | None = None
    ) -> list[Settlement]:
        return self._read("settlement", instrument, "1d", snapshot)

    def read_execution_references(
        self,
        instrument: InstrumentId,
        interval: str,
        *,
        snapshot: DataSnapshot | str | None = None,
    ) -> list[ExecutionReference]:
        return self._read("execution_reference", instrument, normalize_interval(interval), snapshot)

    def has_bar_data(self, instrument: InstrumentId, interval: str) -> bool:
        return bool(self.read_bars(instrument, interval))

    def get_bar_file_path(
        self,
        instrument: InstrumentId,
        interval: str,
        *,
        snapshot: DataSnapshot | str | None = None,
    ) -> Path:
        entry = self._snapshot(snapshot).datasets.get(self._key("bar", instrument, normalize_interval(interval)))
        if entry is None:
            raise FileNotFoundError("no committed canonical dataset; legacy files require explicit re-import")
        return self._path(entry["relative_path"])

    def read_arrow_table(self, instrument: InstrumentId, interval: str) -> pa.Table | None:
        rows = self.read_bars(instrument, interval)
        return bars_to_arrow_table(rows) if rows else None

    @staticmethod
    def _record_key(kind: str, row):
        if kind == "bar":
            return row.bar_start
        if kind == "settlement":
            return row.meta.trading_day
        return row.reference_time, row.session_id, row.price_type.value

    def _validate(self, kind: str, instrument: InstrumentId, interval: str, records: Sequence) -> None:
        keys = set()
        previous = None
        for row in records:
            if row.instrument != instrument:
                raise ValueError("record instrument does not match its dataset")
            if row.meta.quality_flags & (
                QualityFlag.INVALID | QualityFlag.MISSING | QualityFlag.STALE | QualityFlag.PARTIAL
            ):
                raise ValueError("invalid or incomplete records cannot be published as canonical data")
            if kind == "bar" and normalize_interval(row.interval) != interval:
                raise ValueError("bar interval does not match its dataset")
            if kind == "execution_reference" and normalize_interval(row.resolution) != interval:
                raise ValueError("execution reference resolution does not match its dataset")
            key = self._record_key(kind, row)
            if key in keys:
                raise ValueError("duplicate record identity within one publication")
            if previous is not None and key <= self._record_key(kind, previous):
                raise ValueError("publication records are not strictly ordered")
            if kind == "bar" and previous is not None and previous.bar_end > row.bar_start:
                raise ValueError("overlapping bar intervals cannot be published")
            keys.add(key)
            previous = row

    def _write_immutable(self, target: Path, payload: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != payload:
                raise ValueError("immutable manifest content was modified")
            return
        temporary = target.with_name(f".tmp-{uuid4().hex}.json")
        try:
            with temporary.open("wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            _sync_directory(target.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _write_table(self, kind: str, instrument: InstrumentId, interval: str, records: Sequence) -> dict:
        directory = self.root_dir / kind / interval / safe_filename_for_instrument(instrument).removesuffix(".parquet")
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / f".tmp-{uuid4().hex}.parquet"
        table = CODECS[kind][0](records)
        try:
            with temporary.open("wb") as stream:
                pq.write_table(table, stream, compression=self.compression)
                stream.flush()
                os.fsync(stream.fileno())
            read_back = pq.read_table(temporary)
            if not read_back.equals(table):
                raise ValueError("Parquet read-back validation failed")
            digest = _hash(temporary)
            target = directory / f"{digest}.parquet"
            if target.exists():
                if _hash(target) != digest:
                    raise ValueError("immutable data version was modified")
            else:
                os.replace(temporary, target)
                _sync_directory(directory)
            return {
                "relative_path": target.relative_to(self.root_dir).as_posix(),
                "sha256": digest,
                "record_count": len(records),
                "kind": kind,
                "instrument": str(instrument),
                "interval": interval,
            }
        finally:
            temporary.unlink(missing_ok=True)

    def publish_batch(
        self,
        instrument: InstrumentId,
        interval: str,
        *,
        bars: Sequence[Bar] = (),
        settlements: Sequence[Settlement] = (),
        execution_references: Sequence[ExecutionReference] = (),
        merge_existing: bool = True,
        provenance: Mapping[str, Any] | None = None,
    ) -> DataSnapshot:
        normalized = normalize_interval(interval)
        incoming = [
            ("bar", normalized, bars),
            ("settlement", "1d", settlements),
            ("execution_reference", normalized, execution_references),
        ]
        if not any(records for _, _, records in incoming):
            raise ValueError("cannot publish an empty batch")
        with self._writer():
            old = self.capture_snapshot()
            datasets = {key: dict(value) for key, value in old.datasets.items()}
            for kind, period, records in incoming:
                if not records:
                    continue
                records = list(records)
                self._validate(kind, instrument, period, records)
                if merge_existing:
                    previous = self._read(kind, instrument, period, old)
                    merged = {self._record_key(kind, row): row for row in previous}
                    merged.update({self._record_key(kind, row): row for row in records})
                    records = [merged[key] for key in sorted(merged)]
                    self._validate(kind, instrument, period, records)
                entry = self._write_table(kind, instrument, period, records)
                entry["provenance"] = dict(provenance or {})
                datasets[self._key(kind, instrument, period)] = entry
            payload = _json_bytes({"schema_version": 2, "datasets": datasets})
            snapshot_id = hashlib.sha256(payload).hexdigest()
            self._write_immutable(self.root_dir / "manifests" / f"{snapshot_id}.json", payload)
            pointer = _json_bytes({"schema_version": 2, "snapshot_id": snapshot_id})
            temporary = self.root_dir / f".tmp-current-{uuid4().hex}.json"
            try:
                with temporary.open("wb") as stream:
                    stream.write(pointer)
                    stream.flush()
                    os.fsync(stream.fileno())
                # This is the only commit point. Failures propagate; readers keep the previous snapshot.
                os.replace(temporary, self.manifest_path)
                _sync_directory(self.root_dir)
            finally:
                temporary.unlink(missing_ok=True)
            return self.capture_snapshot(snapshot_id)

    def save_bars(
        self,
        bars: Sequence[Bar],
        instrument: InstrumentId,
        interval: str,
        merge_existing: bool = True,
    ) -> Path:
        committed = self.publish_batch(instrument, interval, bars=bars, merge_existing=merge_existing)
        return self.get_bar_file_path(instrument, interval, snapshot=committed)

    def save_settlements(self, records: Sequence[Settlement], instrument: InstrumentId) -> DataSnapshot:
        return self.publish_batch(instrument, "1d", settlements=records)

    def save_execution_references(
        self,
        records: Sequence[ExecutionReference],
        instrument: InstrumentId,
        interval: str,
    ) -> DataSnapshot:
        return self.publish_batch(instrument, interval, execution_references=records)

    def retire_datasets(self, keys: Sequence[str]) -> DataSnapshot:
        """发布一个不再引用给定数据集键的新清单指针；内容寻址文件与旧清单原样保留，旧快照仍可按 ID 读取.

        用于把误入工程样本存储的研究数据集从当前指针移除 (07 §4.4 工程样本与研究数据集分开管理)。
        """
        with self._writer():
            old = self.capture_snapshot()
            missing = [key for key in keys if key not in old.datasets]
            if missing:
                raise KeyError(f"datasets not present in the current publication: {missing}")
            datasets = {key: dict(value) for key, value in old.datasets.items() if key not in set(keys)}
            payload = _json_bytes({"schema_version": 2, "datasets": datasets})
            snapshot_id = hashlib.sha256(payload).hexdigest()
            self._write_immutable(self.root_dir / "manifests" / f"{snapshot_id}.json", payload)
            pointer = _json_bytes({"schema_version": 2, "snapshot_id": snapshot_id})
            temporary = self.root_dir / f".tmp-current-{uuid4().hex}.json"
            try:
                with temporary.open("wb") as stream:
                    stream.write(pointer)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.manifest_path)
                _sync_directory(self.root_dir)
            finally:
                temporary.unlink(missing_ok=True)
            return self.capture_snapshot(snapshot_id)
