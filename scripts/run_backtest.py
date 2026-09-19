#!/usr/bin/env python
"""[S3-06 / FR-VAL-03 / FR-VAL-07] 单品种事件驱动 Bar 回测入口：装配 -> 运行 -> 绩效 -> run_manifest 与权益曲线归档."""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.analysis.visualizer import format_performance_summary  # noqa: E402
from qh_trader.core.constants import (  # noqa: E402
    AuctionFillPolicy,
    ExecutionPolicy,
    IntrabarTouchRule,
    LimitLiquidityScenario,
    MissedExecutionPolicy,
)
from qh_trader.research.backtest_assembly import BacktestSpec, run_backtest, write_run_artifacts  # noqa: E402


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def spec_from_args(args: argparse.Namespace) -> BacktestSpec:
    return BacktestSpec(
        symbol=args.symbol,
        interval=args.interval,
        storage_dir=args.storage_dir,
        catalog_path=args.catalog,
        calendar_path=args.calendar or None,
        snapshot_id=args.snapshot,
        initial_capital=Decimal(args.capital),
        commission_per_lot=Decimal(args.commission),
        margin_ratio=Decimal(args.margin_ratio),
        slippage_ticks=args.slippage_ticks,
        participation_rate=Decimal(args.participation_rate),
        limit_liquidity_scenario=LimitLiquidityScenario(args.limit_scenario),
        intrabar_touch_rule=IntrabarTouchRule(args.touch_rule),
        auction_fill_policy=AuctionFillPolicy(args.auction_policy),
        order_delay_ms=args.order_delay_ms,
        cancel_delay_ms=args.cancel_delay_ms,
        execution_policy=ExecutionPolicy(args.execution_policy),
        missed_execution=MissedExecutionPolicy(args.missed_execution),
        use_official_settlement=not args.settle_on_close,
        fast_window=args.fast,
        slow_window=args.slow,
        order_size=args.order_size,
    )


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--symbol", "-s", default="SHFE.rb2410", help="Contract symbol, e.g. SHFE.rb2410")
    parser.add_argument("--interval", "-i", default="1d", help="Bar interval, e.g. 1d, 1h")
    parser.add_argument("--storage-dir", default="data_storage")
    parser.add_argument("--catalog", default="config/contract_catalog_2024v1.json")
    parser.add_argument(
        "--calendar", default="config/calendar_2024v1.json", help="Versioned calendar; empty to disable session gate"
    )
    parser.add_argument("--snapshot", default=None, help="Dataset snapshot hash; default = current publication pointer")
    parser.add_argument("--capital", default="1000000", help="Initial capital (decimal string)")
    parser.add_argument("--commission", default="5.0", help="Research commission per lot (decimal string)")
    parser.add_argument("--margin-ratio", default="0.10")
    parser.add_argument("--slippage-ticks", type=int, default=0)
    parser.add_argument("--participation-rate", default="1.0")
    parser.add_argument(
        "--limit-scenario",
        default=LimitLiquidityScenario.DIRECTION_CONSERVATIVE.value,
        choices=[s.value for s in LimitLiquidityScenario],
    )
    parser.add_argument(
        "--touch-rule", default=IntrabarTouchRule.TOUCH.value, choices=[s.value for s in IntrabarTouchRule]
    )
    parser.add_argument(
        "--auction-policy",
        default=AuctionFillPolicy.ASSUME_PARTICIPATION.value,
        choices=[s.value for s in AuctionFillPolicy],
    )
    parser.add_argument("--order-delay-ms", type=int, default=0)
    parser.add_argument("--cancel-delay-ms", type=int, default=0)
    parser.add_argument(
        "--execution-policy", default=ExecutionPolicy.NEXT_BAR_OPEN.value, choices=[s.value for s in ExecutionPolicy]
    )
    parser.add_argument(
        "--missed-execution",
        default=MissedExecutionPolicy.DEFER.value,
        choices=[s.value for s in MissedExecutionPolicy],
    )
    parser.add_argument(
        "--settle-on-close", action="store_true", help="Use bar close instead of official settlement price"
    )
    parser.add_argument("--fast", type=int, default=5)
    parser.add_argument("--slow", type=int, default=20)
    parser.add_argument("--order-size", type=int, default=1)


def main() -> int:
    configure_console()
    parser = argparse.ArgumentParser(description="Run S3 Bar event-driven backtest")
    add_common_arguments(parser)
    parser.add_argument("--output-dir", default="runs/backtest", help="Root directory for per-run artifacts")
    args = parser.parse_args()

    try:
        result, metrics, manifest, _ = run_backtest(spec_from_args(args), root=ROOT)
    except Exception as exc:  # noqa: BLE001 - 入口脚本把失败原因完整报告给操作者
        print(f"回测执行失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    summary = format_performance_summary(metrics)
    print(summary)
    run_dir = write_run_artifacts(ROOT / args.output_dir, manifest, result, summary)
    print(f"实验快照已保存至: {run_dir} (run_id={manifest['run_id']})")
    hashes = manifest["outputs"]["canonical_hashes"]
    print(f"规范哈希: orders={hashes['orders'][:12]} trades={hashes['trades'][:12]} ledger={hashes['ledger'][:12]}")
    if manifest.get("rerun_of"):
        print(f"重跑对照: 与既有 run 哈希一致 = {manifest['rerun_of']['canonical_hashes_match']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
