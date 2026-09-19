#!/usr/bin/env python
"""[S3-08 / FR-VAL-04] 双均线快速参数网格扫描脚本."""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.core.constants import MissingRuleError
from qh_trader.data.contracts import ContractResolver
from qh_trader.data.storage import ParquetDataStorage, normalize_interval
from qh_trader.research.cost_assumptions import ResearchCostModel
from qh_trader.research.vector_backtest import scan_dma_parameters


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan DMA parameters quickly")
    parser.add_argument("--symbol", "-s", default="SHFE.rb2410", help="Contract symbol")
    parser.add_argument("--interval", "-i", default="1d", help="Bar interval")
    parser.add_argument("--storage-dir", default="data_storage", help="Storage directory")
    parser.add_argument("--catalog", default="config/contract_catalog_2024v1.json", help="Contract catalog")
    parser.add_argument("--top", type=int, default=5, help="Top N results to display")
    args = parser.parse_args()

    cat_path = ROOT / args.catalog
    if not cat_path.exists():
        print(f"Catalog not found: {cat_path}", file=sys.stderr)
        return 1

    catalog = ContractResolver.from_file(cat_path)
    instrument, _, _ = catalog.resolve(args.symbol)
    spec = catalog.get_spec(args.symbol)

    storage = ParquetDataStorage(ROOT / args.storage_dir)
    bars = list(storage.read_bars(instrument, normalize_interval(args.interval)))
    if not bars:
        print(f"No bars found for {args.symbol}", file=sys.stderr)
        return 1

    cost_model = ResearchCostModel(
        multiplier=spec.multiplier,
        price_tick=spec.price_tick,
        commission_per_lot=Decimal("5.0"),
    )

    results = scan_dma_parameters(
        bars,
        cost_model,
        fast_range=range(3, 12, 2),
        slow_range=range(15, 45, 5),
    )

    print(f"=== 双均线参数扫描完成 (共测试 {len(results)} 组组合) ===")
    print(f"{'Fast':<6} {'Slow':<6} {'Total Return':<14} {'Sharpe':<10} {'Max Drawdown':<14} {'Trades':<8}")
    print("-" * 62)
    for r in results[: args.top]:
        print(
            f"{r.fast_window:<6d} {r.slow_window:<6d} {r.total_return * 100:>10.2f}% {r.sharpe_ratio:>10.2f} "
            f"{r.max_drawdown_pct * 100:>12.2f}% {r.total_trades:>8d}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
