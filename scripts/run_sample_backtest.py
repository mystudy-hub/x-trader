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


def main(argv: list[str] | None = None) -> int:
    from qh_trader.data.contracts import ContractResolver

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="SHFE.rb2410")
    parser.add_argument("--interval", choices=["1d", "1h"], default="1d")
    parser.add_argument("--storage-dir", type=Path, default=Path("data_storage"))
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--snapshot")
    parser.add_argument("--fee-per-lot", type=Decimal, default=Decimal("5"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        result = run_trend_backtest(
            args.symbol,
            args.interval,
            args.storage_dir,
            catalog=ContractResolver.from_file(args.catalog),
            snapshot_id=args.snapshot,
            fee_per_lot=args.fee_per_lot,
        )
    except (ValueError, OSError, LookupError) as exc:
        logger.error("样本研究演示未执行：%s", exc)
        return 1
    logger.info("研究原型演示（不作为 S2/S3 交易验收）")
    logger.info("数据快照：%s", result["snapshot_id"])
    logger.info("成本及撮合假设：%s", result["assumptions"])
    logger.info("数据来源与时间假设：%s", result["data_provenance"])
    logger.info(
        "期末权益：%s；收益率：%.4f%%；最大回撤：%.4f%%；成交事件：%s",
        result["final_equity"],
        result["total_return_pct"],
        result["max_drawdown_pct"],
        result["trades_count"],
    )
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
