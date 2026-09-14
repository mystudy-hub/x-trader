#!/usr/bin/env python
"""Report calendar-based gaps or the registered unresolved preparation gaps without filling data."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    import yaml

    from qh_trader.data.calendar import TradingCalendar
    from qh_trader.data.downloader import parse_instrument
    from qh_trader.data.gaps import scan_gaps
    from qh_trader.data.schemas import parse_day
    from qh_trader.data.storage import ParquetDataStorage
    from qh_trader.data.validation import file_hash, file_reference
    from qh_trader.infrastructure.observability import configure_logging
    from scripts.data_reports import emit_report, load_gap_evidence

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, help="inspect the preparation gap registry instead of market coverage")
    parser.add_argument("--storage-dir", type=Path, default=ROOT / "data_storage")
    parser.add_argument("--symbol", default="SHFE.rb2410")
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--snapshot")
    parser.add_argument("--calendar", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--start-day", type=date.fromisoformat)
    parser.add_argument("--end-day", type=date.fromisoformat)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "runs/gaps")
    args = parser.parse_args(argv)
    if args.registry is None and not all((args.calendar, args.start_day, args.end_day)):
        parser.error("market gap scan requires --calendar, --start-day and --end-day")
    logger = logging.getLogger("qh_trader.gaps")
    with configure_logging(context={"component": "gaps"}, path_root=ROOT):
        try:
            if args.registry is not None:
                data = yaml.safe_load(args.registry.read_text(encoding="utf-8-sig"))
                if data.get("schema_version") != 1 or not isinstance(data.get("gaps"), list):
                    raise ValueError("invalid gap registry")
                identifiers = [row["gap_id"] for row in data["gaps"]]
                if len(set(identifiers)) != len(identifiers):
                    raise ValueError("duplicate registered gap identifiers")
                for row in data["gaps"]:
                    if row.get("status") not in {"未关闭", "已关闭"}:
                        raise ValueError("unknown gap status")
                    if row["status"] == "已关闭":
                        parse_day(row.get("closed_at"))
                        proof = row.get("close_evidence")
                        if not isinstance(proof, dict):
                            raise ValueError("closed gaps require actual closure evidence")
                        source = (ROOT / proof["path"]).resolve()
                        if not source.is_relative_to(ROOT) or file_hash(source) != proof["sha256"]:
                            raise ValueError("gap closure evidence is missing or changed")
                unresolved = [
                    {
                        key: row.get(key)
                        for key in (
                            "gap_id",
                            "item",
                            "current_handling",
                            "handling_details",
                            "close_condition",
                            "status",
                        )
                    }
                    for row in data["gaps"]
                    if row.get("status") != "已关闭"
                ]
                payload = {
                    "schema_version": 1,
                    "scope": "preparation_gap_registry",
                    "passed": not unresolved,
                    "input_refs": {"registry": file_reference(args.registry, ROOT)},
                    "unresolved": unresolved,
                    "quarantine": [],
                }
            else:
                storage = ParquetDataStorage(args.storage_dir)
                snapshot = storage.capture_snapshot(args.snapshot)
                if snapshot.snapshot_id is None:
                    raise ValueError("no committed canonical snapshot; no empty-success gap report is allowed")
                instrument = parse_instrument(args.symbol)
                report = scan_gaps(
                    storage.read_bars(instrument, args.interval, snapshot=snapshot),
                    instrument,
                    TradingCalendar.from_file(args.calendar),
                    start_day=args.start_day,
                    end_day=args.end_day,
                    evidence=load_gap_evidence(args.evidence),
                )
                refs = {"snapshot_id": snapshot.snapshot_id, "calendar": file_reference(args.calendar, ROOT)}
                if args.evidence is not None:
                    refs["evidence"] = file_reference(args.evidence, ROOT)
                payload = {
                    **report.as_dict(),
                    "input_refs": refs,
                    "quarantine": []
                    if report.passed
                    else [
                        {
                            "status": "blocked_for_use",
                            "input_refs": refs,
                            "reason": "unresolved active-session coverage",
                        }
                    ],
                }
        except Exception as exc:
            logger.error("缺口检查未完成：%s", exc)
            payload = {
                "schema_version": 1,
                "scope": "gap_input_validation",
                "passed": False,
                "issues": [{"code": "input_error", "message": "required gap inputs are unavailable or invalid"}],
                "quarantine": [],
            }
        path, quarantine = emit_report(payload, args.output_dir)
        logger.info("缺口报告已保存", extra={"path": path, "quarantine_path": quarantine, "passed": payload["passed"]})
        return 0 if payload["passed"] else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
