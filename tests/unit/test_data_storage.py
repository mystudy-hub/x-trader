"""Immutable versions and manifest commit failures, using isolated real Parquet files."""

import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from qh_trader.core.constants import Exchange, QualityFlag
from qh_trader.core.objects import InstrumentId
from qh_trader.data.storage import ParquetDataStorage, safe_filename_for_instrument


def test_safe_filename_encodes_windows_reserved_characters():
    assert safe_filename_for_instrument(InstrumentId(Exchange.SHFE, "rb2410")) == "SHFE_rb2410.parquet"
    assert ":" not in safe_filename_for_instrument(InstrumentId(Exchange.SHFE, "rb:2410"))


def test_parquet_roundtrip_preserves_complete_metadata_and_is_idempotent(tmp_path, sample_instrument, sample_bars):
    storage = ParquetDataStorage(tmp_path)
    path = storage.save_bars(sample_bars, sample_instrument, "1d")
    snapshot = storage.capture_snapshot()
    assert storage.read_bars(sample_instrument, "1d") == sample_bars
    assert path.is_file()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == path.stem
    storage.save_bars(sample_bars, sample_instrument, "1d")
    assert storage.capture_snapshot().snapshot_id == snapshot.snapshot_id


def test_revisions_keep_old_data_and_snapshots(tmp_path, sample_instrument, sample_bars):
    storage = ParquetDataStorage(tmp_path)
    old_path = storage.save_bars(sample_bars, sample_instrument, "1d")
    old_bytes = old_path.read_bytes()
    old_snapshot = storage.capture_snapshot()
    changed = replace(
        sample_bars[0],
        open=Decimal(101),
        high=Decimal(101),
        low=Decimal(101),
        close=Decimal(101),
        meta=replace(sample_bars[0].meta, source_version="revision-v2"),
    )
    new_path = storage.save_bars([changed], sample_instrument, "1d")
    assert new_path != old_path
    assert old_path.read_bytes() == old_bytes
    assert storage.read_bars(sample_instrument, "1d", snapshot=old_snapshot) == sample_bars
    assert storage.get_bar_file_path(sample_instrument, "1d", snapshot=old_snapshot) == old_path
    assert storage.read_bars(sample_instrument, "1d")[0].close == Decimal(101)


def test_save_returns_its_own_version_if_another_publication_changes_current(
    tmp_path,
    sample_instrument,
    sample_bars,
    monkeypatch,
):
    storage = ParquetDataStorage(tmp_path)
    original_publish = storage.publish_batch
    snapshots = []

    def publish_then_replace(*args, **kwargs):
        own = original_publish(*args, **kwargs)
        snapshots.append(own)
        changed = replace(sample_bars[0], open=Decimal(101), high=Decimal(101), low=Decimal(101), close=Decimal(101))
        original_publish(sample_instrument, "1d", bars=[changed])
        return own

    monkeypatch.setattr(storage, "publish_batch", publish_then_replace)
    returned = storage.save_bars(sample_bars, sample_instrument, "1d")
    assert returned == storage.get_bar_file_path(sample_instrument, "1d", snapshot=snapshots[0])
    assert returned != storage.get_bar_file_path(sample_instrument, "1d")


@pytest.mark.parametrize("existing", [False, True])
def test_manifest_failure_never_exposes_uncommitted_records(tmp_path, sample_instrument, sample_bars, existing):
    import os

    storage = ParquetDataStorage(tmp_path)
    if existing:
        storage.save_bars(sample_bars[:1], sample_instrument, "1d")
    before = storage.capture_snapshot()
    actual_replace = os.replace

    def fail_commit(source, target):
        if Path(target) == storage.manifest_path:
            raise OSError("injected manifest commit failure")
        return actual_replace(source, target)

    with patch("qh_trader.data.storage.os.replace", side_effect=fail_commit):
        with pytest.raises(OSError, match="injected"):
            storage.save_bars(sample_bars[1:], sample_instrument, "1d")
    assert storage.capture_snapshot() == before
    assert storage.read_bars(sample_instrument, "1d") == (sample_bars[:1] if existing else [])
    assert not list(tmp_path.rglob(".tmp-*"))


def test_corrupt_data_and_manifest_are_rejected(tmp_path, sample_instrument, sample_bars):
    storage = ParquetDataStorage(tmp_path)
    path = storage.save_bars(sample_bars, sample_instrument, "1d")
    snapshot = storage.capture_snapshot()
    path.write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="manifest hash"):
        storage.read_bars(sample_instrument, "1d")
    manifest = tmp_path / "manifests" / f"{snapshot.snapshot_id}.json"
    manifest.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest"):
        storage.capture_snapshot()


@pytest.mark.parametrize("problem", ["instrument", "interval", "duplicate", "quality"])
def test_publication_rejects_mismatched_or_invalid_records(tmp_path, sample_instrument, sample_bars, problem):
    rows = sample_bars[:1]
    if problem == "instrument":
        rows = [replace(rows[0], instrument=InstrumentId(Exchange.SHFE, "rb2501"))]
    elif problem == "interval":
        rows = [replace(rows[0], interval="1h")]
    elif problem == "duplicate":
        rows = rows * 2
    else:
        rows = [replace(rows[0], meta=replace(rows[0].meta, quality_flags=QualityFlag.INVALID))]
    storage = ParquetDataStorage(tmp_path)
    with pytest.raises(ValueError):
        storage.save_bars(rows, sample_instrument, "1d")
    assert storage.capture_snapshot().snapshot_id is None


def test_legacy_files_are_retained_but_not_read_as_canonical(tmp_path, sample_instrument, sample_bars):
    legacy = tmp_path / "bar/1d/SHFE_rb2410.parquet"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy sample with unverified time metadata")
    (tmp_path / "manifest.json").write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    storage = ParquetDataStorage(tmp_path)
    assert storage.read_bars(sample_instrument, "1d") == []
    storage.save_bars(sample_bars, sample_instrument, "1d")
    assert legacy.read_bytes() == b"legacy sample with unverified time metadata"
    assert (tmp_path / "manifest.json").read_text(encoding="utf-8") == '{"schema_version": 1}'


def test_overlapping_writers_are_rejected(tmp_path):
    first, second = ParquetDataStorage(tmp_path), ParquetDataStorage(tmp_path)
    with first._writer():
        with pytest.raises(OSError):
            with second._writer():
                pytest.fail("concurrent writer acquired the lock")
