#!/usr/bin/env python
"""Run the explicitly labelled research demonstration on a committed dataset snapshot."""

from __future__ import annotations

import argparse
import logging
import sys
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if TYPE_CHECKING:
    from qh_trader.data.contracts import ContractResolver

logger = logging.getLogger(__name__)


def run_trend_backtest(
    instrument_str: str = "SHFE.rb2410",
    interval: str = "1d",
    storage_dir: str | Path = "data_storage",
    fast_window: int = 5,
    slow_window: int = 20,
    initial_capital: Decimal = Decimal("1000000"),
    *,
    catalog: ContractResolver | None = None,
    snapshot_id: str | None = None,
    fee_per_lot: Decimal = Decimal("5"),
) -> dict[str, Any]:
    from qh_trader.core.constants import MissingRuleError
    from qh_trader.data.replay import HistoricalMarketDataAdapter
    from qh_trader.data.storage import ParquetDataStorage, normalize_interval
    from qh_trader.research.sample_backtest import simulate_trend

    if catalog is None:
        raise MissingRuleError("supply a versioned contract catalog; contract multiplier cannot be guessed")
    instrument, _, _ = catalog.resolve(instrument_str)
    spec = catalog.get_spec(instrument_str)
    interval = normalize_interval(interval)
    storage = ParquetDataStorage(ROOT / storage_dir)
    adapter = HistoricalMarketDataAdapter(storage, snapshot=snapshot_id, execution_interval=interval)
    if adapter.snapshot.snapshot_id is None:
        raise ValueError("no committed canonical snapshot; legacy samples require corrected metadata and re-import")
    adapter.subscribe([instrument])
    observations, openings = adapter.replay_schedule(instrument, interval)
    for bar in storage.read_bars(instrument, interval, snapshot=adapter.snapshot):
        catalog.resolve(str(instrument), as_of=bar.meta.trading_day)
    result = simulate_trend(
        adapter,
        instrument,
        interval,
        observations,
        openings,
        multiplier=spec.multiplier,
        fast_window=fast_window,
        slow_window=slow_window,
        initial_capital=initial_capital,
        fee_per_lot=fee_per_lot,
    )
    result["snapshot_id"] = adapter.snapshot.snapshot_id
    result["catalog_version"] = catalog.catalog_version
    result["data_provenance"] = [
        entry.get("provenance", {})
        for entry in adapter.snapshot.as_dict()["datasets"].values()
        if entry.get("instrument") == str(instrument)
    ]
    return result


def _run(args: argparse.Namespace, metrics) -> int:
    from qh_trader.data.contracts import ContractResolver
    from qh_trader.infrastructure.observability import log_context

    try:
        with metrics.timer("research.run_seconds"):
            result = run_trend_backtest(
                args.symbol,
                args.interval,
                args.storage_dir,
                catalog=ContractResolver.from_file(args.catalog),
                snapshot_id=args.snapshot,
                fee_per_lot=args.fee_per_lot,
            )
    except (ValueError, OSError, LookupError) as exc:
        metrics.increment("research.runs", labels={"result": "failed"})
        logger.error("样本研究演示未执行：%s", exc)
        return 1
    metrics.increment("research.runs", labels={"result": "success"})
    metrics.increment("research.fills", result["trades_count"])
    with log_context(rule_version=result["catalog_version"], instrument_id=args.symbol):
        logger.info(
            "研究原型演示完成（不作为 S2/S3 交易验收）",
            extra={
                "snapshot_id": result["snapshot_id"],
                "assumptions": result["assumptions"],
                "data_provenance": result["data_provenance"],
                "final_equity": result["final_equity"],
                "return_pct": result["total_return_pct"],
                "drawdown_pct": result["max_drawdown_pct"],
                "trades_count": result["trades_count"],
            },
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    from qh_trader.infrastructure.observability import MetricsRegistry, configure_logging

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="SHFE.rb2410")
    parser.add_argument("--interval", choices=["1d", "1h"], default="1d")
    parser.add_argument("--storage-dir", type=Path, default=Path("data_storage"))
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--snapshot")
    parser.add_argument("--fee-per-lot", type=Decimal, default=Decimal("5"))
    parser.add_argument("--log-file", type=Path)
    args = parser.parse_args(argv)
    try:
        with configure_logging(
            file=args.log_file,
            path_root=ROOT,
            context={"component": "sample_backtest", "account_alias": "research", "strategy_id": "ma-demo"},
        ):
            metrics = MetricsRegistry()
            status = _run(args, metrics)
            logger.info("研究运行指标", extra={"metrics": metrics.snapshot()})
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
