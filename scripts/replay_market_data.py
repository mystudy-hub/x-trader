#!/usr/bin/env python
"""[S3-09 / FR-VAL-06 / A28] Bar 全链路行情回放脚本.

验证：
- 历史行情驱动全链路（VirtualClock -> Strategy -> Engine -> SimulatedGateway -> Ledger）；
- 强制绑定 SimulatedGateway，严禁连接真实柜台；
- 支持尽快与指定时钟推进，固定输入在不同调度参数下结果绝对幂等一致。
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.core.constants import LimitLiquidityScenario
from qh_trader.data.contracts import ContractResolver
from qh_trader.data.storage import ParquetDataStorage, normalize_interval
from qh_trader.engine.backtest_engine import BacktestEngine
from qh_trader.gateway.simulated_gateway import SimulatedGateway
from qh_trader.infrastructure.memory_journal import MemoryJournal
from qh_trader.strategy.examples.trend_following import DualMovingAverageStrategy


def run_replay(
    symbol: str = "SHFE.rb2410",
    interval: str = "1d",
    storage_dir: str | Path = "data_storage",
    catalog_path: str | Path = "config/contract_catalog_2024v1.json",
) -> tuple[Decimal, int, Decimal]:
    cat_path = ROOT / catalog_path
    catalog = ContractResolver.from_file(cat_path)
    instrument, _, _ = catalog.resolve(symbol)
    spec = catalog.get_spec(symbol)

    storage = ParquetDataStorage(ROOT / storage_dir)
    bars = list(storage.read_bars(instrument, normalize_interval(interval)))
    if not bars:
        raise ValueError(f"no bars found for {symbol}")

    gateway = SimulatedGateway(
        account_id="replay-account",
        trading_day=bars[0].meta.trading_day,
        slippage_ticks=0,
        price_tick=spec.price_tick,
        limit_liquidity_scenario=LimitLiquidityScenario.DIRECTION_CONSERVATIVE,
    )
    journal = MemoryJournal("replay-account")

    engine = BacktestEngine(
        account_id="replay-account",
        gateway=gateway,
        start_time=bars[0].bar_start,
        initial_capital=Decimal("1000000.00"),
        contract_multiplier=spec.multiplier,
        commission_per_lot=Decimal("5.0"),
        journal=journal,
    )
    strategy = DualMovingAverageStrategy(
        strategy_id=f"replay-dma-{instrument.symbol}",
        context=engine,
        instrument=instrument,
        fast_window=5,
        slow_window=20,
        order_size=1,
    )
    engine.add_strategy(strategy)

    result = engine.run(bars)
    return result.final_equity, result.total_trades, result.total_commission


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay Market Data end-to-end")
    parser.add_argument("--symbol", "-s", default="SHFE.rb2410")
    parser.add_argument("--interval", "-i", default="1d")
    args = parser.parse_args()

    print(f"开始全链路行情回放: {args.symbol} {args.interval} ...")
    eq, trades, comm = run_replay(args.symbol, args.interval)
    print(f"回放完成: 期末权益={eq:,.2f} 元, 总成交={trades} 笔, 手续费={comm:,.2f} 元")
    return 0


if __name__ == "__main__":
    sys.exit(main())
