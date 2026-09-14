"""Shared input/report helpers for the data-quality command entry points."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from qh_trader.core.constants import Exchange, PriceType
from qh_trader.core.objects import InstrumentId, VersionedValue
from qh_trader.data.gaps import GapEvidence
from qh_trader.data.schemas import parse_time
from qh_trader.data.validation import ExecutionCheck, file_hash, report_json
from qh_trader.infrastructure.observability import Redactor

ROOT = Path(__file__).resolve().parents[1]


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_limits(path: Path | None):
    if path is None:
        return {}
    document = read_json(path)
    if document.get("schema_version") != 1:
        raise ValueError("unsupported price-limit input schema")
    limits = {}
    for row in document["limits"]:
        day = date.fromisoformat(row["trading_day"])
        if day in limits:
            raise ValueError("multiple price-limit versions require an explicit interval selection")
        limits[day] = VersionedValue(
            value=(Decimal(row["lower"]), Decimal(row["upper"])),
            source_id=row["source_id"],
            version=row["version"],
            effective_from=parse_time(row["effective_from"]),
            available_at=parse_time(row["available_at"]),
            effective_to=parse_time(row["effective_to"]) if row.get("effective_to") is not None else None,
        )
    return limits


def load_execution_checks(path: Path | None):
    if path is None:
        return None
    document = read_json(path)
    if document.get("schema_version") != 1:
        raise ValueError("unsupported execution requirement schema")
    return [
        ExecutionCheck(
            parse_time(row["reference_time"]),
            row["session_id"],
            PriceType(row["price_type"]),
            parse_time(row["known_at"]),
        )
        for row in document["checks"]
    ]


def load_gap_evidence(path: Path | None, root: Path = ROOT):
    if path is None:
        return ()
    document = read_json(path)
    if document.get("schema_version") != 1:
        raise ValueError("unsupported gap evidence schema")
    evidence = []
    for row in document["windows"]:
        proof = row["proof"]
        artifact = (root / proof["path"]).resolve()
        if not artifact.is_relative_to(root.resolve()) or file_hash(artifact) != proof["sha256"]:
            raise ValueError("gap cause evidence is missing or its checksum does not match")
        evidence.append(
            GapEvidence(
                InstrumentId(Exchange(row["exchange"]), row["symbol"]),
                date.fromisoformat(row["trading_day"]),
                parse_time(row["start"]),
                parse_time(row["end"]),
                row["kind"],
                row["source_id"],
                proof["path"],
            )
        )
    return tuple(evidence)


def _write_immutable(directory: Path, prefix: str, body: str) -> Path:
    payload = body.encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{prefix}-{digest}.json"
    if target.exists():
        if target.read_bytes() != payload:
            raise ValueError("quality report hash collision or modified report")
        return target
    temporary = directory / f".tmp-{uuid4().hex}.json"
    try:
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def emit_report(payload: dict, output_dir: Path, *, root: Path = ROOT) -> tuple[Path, Path | None]:
    safe = Redactor(path_root=root, max_text=100_000).clean(payload)
    body = report_json(safe)
    report = _write_immutable(output_dir, "report", body)
    quarantine = None
    if safe.get("quarantine"):
        quarantine = _write_immutable(
            output_dir,
            "quarantine",
            report_json(
                {
                    "schema_version": 1,
                    "source_report": report.name,
                    "source_report_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                    "quarantine": safe["quarantine"],
                    "action": "exclude_failed_scope_from_use; source files unchanged",
                }
            ),
        )
    print(body)
    return report, quarantine
