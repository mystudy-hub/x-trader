#!/usr/bin/env python
"""Archive public source observations; publish canonical datasets only with complete import evidence."""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logger = logging.getLogger(__name__)


def load_default_symbol() -> str:
    import yaml

    path = ROOT / "config/settings.yaml"
    if not path.is_file():
        path = ROOT / "config/settings.yaml.example"
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    symbol = data.get("data", {}).get("engineering_sample", {}).get("contract")
    if not symbol:
        raise ValueError("declare the actual sample contract in configuration or --symbols")
    return symbol


def _run(args: argparse.Namespace, metrics) -> int:
    from qh_trader.data.calendar import TradingCalendar
    from qh_trader.data.contracts import ContractResolver
    from qh_trader.data.downloader import FuturesDataDownloader, load_import_metadata
    from qh_trader.infrastructure.observability import log_context

    try:
        symbols = [
            value.strip()
            for value in (args.symbols if args.symbols is not None else load_default_symbol()).split(",")
            if value.strip()
        ]
        intervals = [value.strip() for value in args.intervals.split(",") if value.strip()]
        if not symbols or not intervals:
            raise ValueError("at least one contract and interval must be provided")
        metadata = {}
        for name in ("catalog", "calendar", "timings"):
            path = getattr(args, name)
            if path is not None:
                path = path.resolve()
                if not path.is_relative_to(ROOT):
                    raise ValueError("import evidence files must be inside the repository")
                metadata[name] = {
                    "path": path.relative_to(ROOT).as_posix(),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
        timings, publications = load_import_metadata(args.timings) if args.timings is not None else ({}, {})
        downloader = FuturesDataDownloader(
            args.source,
            ROOT / args.storage_dir,
            resolver=ContractResolver.from_file(args.catalog) if args.catalog is not None else None,
            calendar=TradingCalendar.from_file(args.calendar) if args.calendar is not None else None,
            timings=timings,
            publications=publications,
            metadata_refs=metadata,
        )
    except (OSError, ValueError, KeyError, ImportError) as exc:
        metrics.increment("data.initialization_failures")
        logger.error("数据接入未启动：%s", exc)
        return 1
    failures = 0
    for symbol in symbols:
        for interval in intervals:
            labels = {"source": args.source, "interval": interval}
            with log_context(
                instrument_id=symbol,
                interval=interval,
                source_id=downloader.data_source.source_id,
                rule_version=downloader.calendar.version if downloader.calendar is not None else None,
            ):
                try:
                    with metrics.timer("data.import_seconds", labels=labels):
                        if args.publish:
                            path, _, bars = downloader.download_bars(symbol, interval, args.start_date, args.end_date)
                            count = len(bars)
                            logger.info(
                                "规范数据已发布", extra={"record_count": count, "path": path, "mode": "canonical"}
                            )
                        else:
                            raw = downloader.download_raw(symbol, interval, args.start_date, args.end_date)
                            count = len(raw.records)
                            if not count:
                                raise ValueError("source returned no observations")
                            metrics.increment("data.quality_issues", len(raw.quality.issues), labels=labels)
                            logger.info(
                                "原始行情已归档",
                                extra={
                                    "record_count": count,
                                    "quality_issues": len(raw.quality.issues),
                                    "path": raw.path,
                                    "mode": "raw",
                                },
                            )
                    metrics.increment("data.records", count, labels=labels)
                    metrics.increment("data.imports", labels={**labels, "result": "success"})
                except (OSError, ValueError, LookupError) as exc:
                    metrics.increment("data.imports", labels={**labels, "result": "failed"})
                    logger.error("数据接入失败：%s", exc)
                    failures += 1
    if not args.publish:
        logger.info("本次仅归档来源观察数据；规范发布需补齐字段、合约目录、日历及时间证据。")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    from qh_trader.infrastructure.observability import MetricsRegistry, configure_logging

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols")
    parser.add_argument("--intervals", default="1d,1h")
    parser.add_argument("--source", choices=["sina", "akshare", "tushare"], default="sina")
    parser.add_argument("--storage-dir", type=Path, default=Path("data_storage"))
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--log-file", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--publish", action="store_true", help="publish only with complete import evidence")
    mode.add_argument("--raw-only", action="store_true", help="archive observations only (the default)")
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--calendar", type=Path)
    parser.add_argument("--timings", type=Path)
    args = parser.parse_args(argv)
    if args.publish and not all((args.catalog, args.calendar, args.timings)):
        parser.error("--publish requires --catalog, --calendar and --timings")
    try:
        with configure_logging(file=args.log_file, context={"component": "download_data"}, path_root=ROOT):
            metrics = MetricsRegistry()
            status = _run(args, metrics)
            logger.info("数据接入运行指标", extra={"metrics": metrics.snapshot()})
            return status
    except OSError:
        print("结构化日志不可用，本次命令失败。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
