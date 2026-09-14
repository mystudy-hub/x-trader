#!/usr/bin/env python
"""Validate raw or committed market data and save immutable quality/quarantine reports."""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import ExitStack
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    from qh_trader.data.calendar import TradingCalendar
    from qh_trader.data.contracts import ContractResolver
    from qh_trader.data.downloader import load_import_metadata, parse_instrument
    from qh_trader.data.gaps import DataIssue
    from qh_trader.data.storage import ParquetDataStorage
    from qh_trader.data.validation import ValidationReport, file_reference, validate_dataset, validate_raw_archive
    from qh_trader.infrastructure.observability import configure_logging
    from qh_trader.infrastructure.rule_store import RuleStore
    from scripts.data_reports import emit_report, load_execution_checks, load_gap_evidence, load_limits

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--storage-dir", type=Path, default=ROOT / "data_storage")
    parser.add_argument("--symbol", default="SHFE.rb2410")
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--snapshot")
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--calendar", type=Path)
    parser.add_argument("--timings", type=Path)
    parser.add_argument("--limits", type=Path)
    parser.add_argument("--rules-db", type=Path)
    parser.add_argument("--profile")
    parser.add_argument("--execution-spec", type=Path)
    parser.add_argument("--gap-evidence", type=Path)
    parser.add_argument("--start-day", type=date.fromisoformat)
    parser.add_argument("--end-day", type=date.fromisoformat)
    parser.add_argument("--mode", choices=["exact", "research"], default="exact")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "runs/validation")
    args = parser.parse_args(argv)
    if args.raw is None and not all((args.catalog, args.calendar, args.timings)):
        parser.error("canonical validation requires --catalog, --calendar and --timings")
    logger = logging.getLogger("qh_trader.validate_data")
    with configure_logging(context={"component": "validate_data"}, path_root=ROOT):
        try:
            if args.raw is not None:
                report = validate_raw_archive(args.raw, expected_sha256=args.expected_sha256)
            else:
                refs = {
                    name: file_reference(getattr(args, name), ROOT)
                    for name in ("catalog", "calendar", "timings", "limits", "execution_spec", "gap_evidence")
                    if getattr(args, name) is not None
                }
                timings, _ = load_import_metadata(args.timings)
                with ExitStack() as stack:
                    rules = stack.enter_context(RuleStore(args.rules_db, readonly=True)) if args.rules_db else None
                    if rules is not None:
                        stack.enter_context(rules.read_snapshot())
                    report = validate_dataset(
                        ParquetDataStorage(args.storage_dir),
                        parse_instrument(args.symbol),
                        args.interval,
                        calendar=TradingCalendar.from_file(args.calendar),
                        catalog=ContractResolver.from_file(args.catalog),
                        timings=timings,
                        mode=args.mode,
                        snapshot_id=args.snapshot,
                        start_day=args.start_day,
                        end_day=args.end_day,
                        rules=rules,
                        profile=args.profile,
                        limits=load_limits(args.limits),
                        execution_checks=load_execution_checks(args.execution_spec),
                        gap_evidence=load_gap_evidence(args.gap_evidence),
                        input_refs=refs,
                    )
        except Exception as exc:
            logger.error("数据校验未完成：%s", exc)
            report = ValidationReport(
                "input_validation",
                args.mode,
                {},
                {},
                (DataIssue("input_error", "required input loading or validation failed"),),
            )
        path, quarantine = emit_report(report.as_dict(), args.output_dir)
        logger.info("数据质量报告已保存", extra={"path": path, "quarantine_path": quarantine, "passed": report.passed})
        return 0 if report.passed else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
