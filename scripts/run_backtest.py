#!/usr/bin/env python
"""[S3-06 / FR-VAL-03 / FR-VAL-07] 单品种事件驱动 Bar 回测入口与实验快照生成脚本."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.analysis.performance import calculate_performance
from qh_trader.analysis.visualizer import format_performance_summary
from qh_trader.core.constants import LimitLiquidityScenario, MissingRuleError
from qh_trader.data.contracts import ContractResolver
from qh_trader.data.storage import ParquetDataStorage, normalize_interval
from qh_trader.engine.backtest_engine import BacktestEngine, BacktestResult
from qh_trader.gateway.simulated_gateway import SimulatedGateway
from qh_trader.infrastructure.memory_journal import MemoryJournal
from qh_trader.strategy.examples.trend_following import DualMovingAverageStrategy

logger = logging.getLogger(__name__)


def run_single_backtest(
    instrument_str: str = "SHFE.rb2410",
    interval: str = "1d",
    storage_dir: str | Path = "data_storage",
    *,
    catalog_path: str | Path = "config/contract_catalog_2024v1.json",
    snapshot_id: str | None = None,
    initial_capital: Decimal = Decimal("1000000"),
    slippage_ticks: int = 0,
    participation_rate: Decimal = Decimal("1.0"),
    limit_liquidity_scenario: LimitLiquidityScenario = LimitLiquidityScenario.DIRECTION_CONSERVATIVE,
    fast_window: int = 5,
    slow_window: int = 20,
    order_size: int = 1,
) -> tuple[BacktestResult, dict]:
    # 1. 加载合约与规则
    cat_path = ROOT / catalog_path
    if not cat_path.exists():
        raise MissingRuleError(f"contract catalog not found at: {cat_path}")
    catalog = ContractResolver.from_file(cat_path)
    instrument, _, _ = catalog.resolve(instrument_str)
    spec = catalog.get_spec(instrument_str)

    # 2. 从不可变存储中读取 Bar 数据
    storage = ParquetDataStorage(ROOT / storage_dir)
    bars = list(storage.read_bars(instrument, normalize_interval(interval), snapshot=snapshot_id))
    if not bars:
        raise ValueError(f"no bars found for {instrument_str} at interval {interval} in {storage_dir}")

    # 3. 组装系统组件
    start_time = bars[0].bar_start
    gateway = SimulatedGateway(
        account_id="backtest-account",
        trading_day=bars[0].meta.trading_day,
        slippage_ticks=slippage_ticks,
        price_tick=spec.price_tick,
        participation_rate=participation_rate,
        limit_liquidity_scenario=limit_liquidity_scenario,
    )
    journal = MemoryJournal("backtest-account")

    engine = BacktestEngine(
        account_id="backtest-account",
        gateway=gateway,
        start_time=start_time,
        initial_capital=initial_capital,
        contract_multiplier=spec.multiplier,
        commission_per_lot=Decimal("5.0"),  # 标准手续费
        margin_ratio=Decimal("0.10"),
        journal=journal,
    )

    strategy = DualMovingAverageStrategy(
        strategy_id=f"dma-{instrument.symbol}",
        context=engine,
        instrument=instrument,
        fast_window=fast_window,
        slow_window=slow_window,
        order_size=order_size,
    )
    engine.add_strategy(strategy)

    # 4. 执行回测
    result = engine.run(bars)

    # 5. 生成 run_manifest 快照
    manifest = {
        "manifest_version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "account_id": result.account_id,
        "instrument": str(instrument),
        "interval": interval,
        "bar_count": len(bars),
        "start_time": bars[0].bar_start.isoformat(),
        "end_time": bars[-1].bar_end.isoformat(),
        "catalog_version": catalog.catalog_version,
        "parameters": {
            "initial_capital": str(initial_capital),
            "slippage_ticks": slippage_ticks,
            "participation_rate": str(participation_rate),
            "limit_liquidity_scenario": limit_liquidity_scenario.value,
            "fast_window": fast_window,
            "slow_window": slow_window,
            "order_size": order_size,
        },
        "summary": {
            "initial_capital": str(result.initial_capital),
            "final_equity": str(result.final_equity),
            "total_pnl": str(result.total_pnl),
            "total_commission": str(result.total_commission),
            "total_trades": result.total_trades,
        },
    }

    return result, manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Run S3 Bar Event-driven Backtest")
    parser.add_argument("--symbol", "-s", default="SHFE.rb2410", help="Contract symbol, e.g. SHFE.rb2410")
    parser.add_argument("--interval", "-i", default="1d", help="Bar interval, e.g. 1d, 1h")
    parser.add_argument("--storage-dir", default="data_storage", help="Storage directory path")
    parser.add_argument("--catalog", default="config/contract_catalog_2024v1.json", help="Contract catalog JSON path")
    parser.add_argument("--snapshot", default=None, help="Snapshot ID")
    parser.add_argument("--capital", type=float, default=1000000.0, help="Initial capital")
    parser.add_argument("--output-dir", default="runs/backtest", help="Manifest and report output dir")

    args = parser.parse_args()

    try:
        result, manifest = run_single_backtest(
            instrument_str=args.symbol,
            interval=args.interval,
            storage_dir=args.storage_dir,
            catalog_path=args.catalog,
            snapshot_id=args.snapshot,
            initial_capital=Decimal(str(args.capital)),
        )
    except Exception as exc:
        print(f"回测执行失败: {exc}", file=sys.stderr)
        return 1

    # 计算绩效指标
    metrics = calculate_performance(result)
    summary_text = format_performance_summary(metrics)
    print(summary_text)

    # 保存快照与清单
    out_dir = ROOT / args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "run_manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(f"实验快照已保存至: {manifest_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
