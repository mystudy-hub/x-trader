#!/usr/bin/env python
"""[S3-07 / S3-08 / FR-VAL-02 / FR-VAL-04] 参数扫描与敏感性矩阵.

- `--mode grid`：向量化通道扫描双均线参数 (与事件驱动通道共用执行时点与成本口径)；
- `--mode sensitivity`：以事件驱动引擎对手续费、滑点、参与率、触价规则、涨跌停情景逐维扰动，
  保存每次试验与失败结果到 sensitivity.json (供 report.py 合并)。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.analysis.validation import run_sensitivity, train_test_split  # noqa: E402
from qh_trader.core.constants import IntrabarTouchRule, LimitLiquidityScenario  # noqa: E402
from qh_trader.data.contracts import ContractResolver  # noqa: E402
from qh_trader.data.storage import ParquetDataStorage, normalize_interval  # noqa: E402
from qh_trader.research.backtest_assembly import BacktestSpec, run_backtest  # noqa: E402
from qh_trader.research.cost_assumptions import ResearchCostModel  # noqa: E402
from qh_trader.research.vector_backtest import scan_dma_parameters  # noqa: E402

SENSITIVITY_DIMENSIONS = {
    "commission_per_lot": [Decimal("0"), Decimal("5.0"), Decimal("10.0"), Decimal("20.0")],
    "slippage_ticks": [0, 1, 2, 4],
    "participation_rate": [Decimal("1.0"), Decimal("0.2"), Decimal("0.05"), Decimal("0.01")],
    "intrabar_touch_rule": [IntrabarTouchRule.TOUCH, IntrabarTouchRule.CROSS_ONE_TICK],
    "limit_liquidity_scenario": [
        LimitLiquidityScenario.DIRECTION_CONSERVATIVE,
        LimitLiquidityScenario.TOUCH_LIMIT_NO_FILL,
    ],
}


def run_grid(args: argparse.Namespace) -> int:
    catalog = ContractResolver.from_file(ROOT / args.catalog)
    instrument, _, _ = catalog.resolve(args.symbol)
    spec = catalog.get_spec(args.symbol)
    storage = ParquetDataStorage(ROOT / args.storage_dir)
    bars = list(storage.read_bars(instrument, normalize_interval(args.interval)))
    if not bars:
        print(f"No bars found for {args.symbol}", file=sys.stderr)
        return 1
    in_sample, out_sample = train_test_split(bars, split_ratio=args.split_ratio)
    cost_model = ResearchCostModel(
        multiplier=spec.multiplier,
        price_tick=spec.price_tick,
        commission_per_lot=Decimal(args.commission),
        slippage_ticks=args.slippage_ticks,
    )
    results = scan_dma_parameters(in_sample, cost_model, fast_range=range(3, 12, 2), slow_range=range(15, 45, 5))
    print(
        f"=== 双均线参数扫描 (样本内 {len(in_sample)} 根 / 样本外 {len(out_sample)} 根保留未用, 共 {len(results)} 组) ==="
    )
    print(f"{'Fast':<6} {'Slow':<6} {'Total Return':<14} {'Sharpe':<10} {'Max Drawdown':<14} {'Trades':<8}")
    print("-" * 62)
    for r in results[: args.top]:
        print(
            f"{r.fast_window:<6d} {r.slow_window:<6d} {r.total_return * 100:>10.2f}% {r.sharpe_ratio:>10.2f} {r.max_drawdown_pct * 100:>12.2f}% {r.total_trades:>8d}"
        )
    print("样本外未参与排序；候选参数须经事件驱动回测 (scripts/run_backtest.py) 复核。")
    return 0


def run_sensitivity_matrix(args: argparse.Namespace) -> int:
    base = BacktestSpec(
        symbol=args.symbol,
        interval=args.interval,
        storage_dir=args.storage_dir,
        catalog_path=args.catalog,
        commission_per_lot=Decimal(args.commission),
        slippage_ticks=args.slippage_ticks,
        fast_window=args.fast,
        slow_window=args.slow,
    )
    dimensions = {k: v for k, v in SENSITIVITY_DIMENSIONS.items() if not args.dimensions or k in args.dimensions}

    def runner(params: dict) -> dict:
        spec = replace(base, **params)
        result, metrics, manifest, _ = run_backtest(spec, root=ROOT)
        return {
            "final_equity": result.final_equity,
            "total_pnl": result.total_pnl,
            "total_commission": result.total_commission,
            "total_trades": result.total_trades,
            "unfilled_orders": len(result.unfilled_orders),
            "sharpe_ratio": metrics.sharpe_ratio,
            "max_drawdown_pct": metrics.max_drawdown_percent * 100,
            "trades_hash": manifest["outputs"]["canonical_hashes"]["trades"][:12],
        }

    baseline = {k: getattr(base, k) for k in dimensions}
    report = run_sensitivity(baseline, dimensions, runner, mode=args.matrix_mode)
    out = Path(args.output)
    out = out if out.is_absolute() else ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"=== 敏感性矩阵完成: {report.trial_count} 次试验, {report.failure_count} 次失败 -> {out} ===")
    for t in report.trials:
        state = json.dumps(t.summary, ensure_ascii=False) if t.succeeded else f"FAILED {t.error}"
        print(f"  #{t.trial_id} {json.dumps(t.parameters, ensure_ascii=False)} -> {state}")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Parameter scan and sensitivity matrix")
    parser.add_argument("--mode", choices=["grid", "sensitivity"], default="grid")
    parser.add_argument("--symbol", "-s", default="SHFE.rb2410")
    parser.add_argument("--interval", "-i", default="1d")
    parser.add_argument("--storage-dir", default="data_storage")
    parser.add_argument("--catalog", default="config/contract_catalog_2024v1.json")
    parser.add_argument("--commission", default="5.0")
    parser.add_argument("--slippage-ticks", type=int, default=0)
    parser.add_argument("--split-ratio", type=float, default=0.7, help="In-sample share for grid scan")
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--fast", type=int, default=5)
    parser.add_argument("--slow", type=int, default=20)
    parser.add_argument("--dimensions", nargs="*", default=None, help="Subset of sensitivity dimensions")
    parser.add_argument("--matrix-mode", choices=["one_at_a_time", "grid"], default="one_at_a_time")
    parser.add_argument("--output", default="runs/backtest/sensitivity.json")
    args = parser.parse_args()
    return run_grid(args) if args.mode == "grid" else run_sensitivity_matrix(args)


if __name__ == "__main__":
    sys.exit(main())
