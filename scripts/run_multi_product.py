#!/usr/bin/env python
"""[脚本] S4-06 多品种组合回测：主力拼接 + 真实移仓 + 样本外报告 (FR-VAL-01/02/03, FR-CON-07).

用法:
    python scripts/run_multi_product.py --products rb,MA,i,m,AP,jd,au,cu --out runs/s4/multi_product

产出:
    <out>/report.md          多品种样本内外指标、分品种贡献与移仓归因
    <out>/run_manifest.json  运行清单（口径、日历版本、经济参数来源、缺口登记）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.data.storage import ParquetDataStorage  # noqa: E402
from qh_trader.research.multi_product import (  # noqa: E402
    BAR_CLOSE_SETTLEMENT,
    DEFAULT_SESSION_TEMPLATE,
    roll_attribution,
    run_portfolio,
    split_metrics,
    write_multi_product_report,
)

DEFAULT_PRODUCTS = "rb,MA,i,m,AP,jd,au,cu"


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S4-06 多品种样本外组合回测")
    parser.add_argument("--products", default=DEFAULT_PRODUCTS, help="逗号分隔品种列表")
    parser.add_argument("--storage", default="data_storage", help="行情存储目录")
    parser.add_argument("--out", default="runs/s4/multi_product", help="输出目录")
    parser.add_argument("--fast-window", type=int, default=5)
    parser.add_argument("--slow-window", type=int, default=20)
    parser.add_argument("--order-size", type=int, default=1)
    parser.add_argument("--capital", type=Decimal, default=Decimal("400000"), help="单品种分配资金")
    parser.add_argument("--train-ratio", type=Decimal, default=Decimal("0.6"), help="拟合窗占比")
    parser.add_argument("--interval", default="1d")
    parser.add_argument(
        "--session-template",
        default=str(DEFAULT_SESSION_TEMPLATE),
        help="S4-05 时段模板 (schema_version 2)；缺失时明确失败",
    )
    args = parser.parse_args(argv)

    storage = ParquetDataStorage(args.storage)
    products = [item.strip() for item in args.products.split(",") if item.strip()]
    run = run_portfolio(
        products,
        storage=storage,
        fast_window=args.fast_window,
        slow_window=args.slow_window,
        order_size=args.order_size,
        initial_capital_per_product=args.capital,
        train_ratio=args.train_ratio,
        interval=args.interval,
        session_template=args.session_template,
    )

    out_dir = Path(args.out)
    report_path = write_multi_product_report(run, out_dir / "report.md")
    in_sample, out_of_sample = split_metrics(run)

    manifest = {
        "stage": "S4-06",
        "kind": "multi_product_research_backtest",
        "products": sorted(run.products),
        "sample": {
            "start": run.sample_start.isoformat(),
            "end": run.sample_end.isoformat(),
            "train_end_exclusive": run.train_end.isoformat(),
            "interval": args.interval,
        },
        "capital": {
            "per_product": str(args.capital),
            "total": str(run.initial_capital),
            "portfolio_equity_points": len(run.portfolio_equity),
        },
        "signal": {
            "type": "dual_moving_average_on_continuous_series",
            "fast_window": args.fast_window,
            "slow_window": args.slow_window,
            "order_size": args.order_size,
            "adjustment": "DIFF",
        },
        "execution": {
            "policy": "NEXT_BAR_OPEN",
            "session_template": str(args.session_template),
            "session_template_sha256": _sha256(Path(args.session_template)),
            "session_gate": "CalendarSessionGate (projected per product contracts)",
            "rollover": "RollManager CLOSE_FIRST, two legs reported separately",
        },
        "assumptions": {
            "settlement_source": BAR_CLOSE_SETTLEMENT,
            "detail": "研究模式以当日收盘价结算，非官方结算价",
            "turnover": "TURNOVER_UNAVAILABLE：免费源无成交额字段，不参与精确核算",
            "economics": "product_registry 研究假设，待规则核验 (FR-RULE-05)",
            "leg_gap": "日线粒度下先平后开存在自然日暴露，已逐笔记录 exposure_calendar_days",
        },
        "gaps": ["GAP-S0-03 研究数据集未采购：样本区间受免费源与可用合约限制"],
        "per_product": {
            product: {
                "contracts": [str(instrument) for instrument in product_run.dataset.contracts],
                "trades": product_run.metrics.total_trades,
                "closed_trades": product_run.metrics.closed_trades,
                "final_equity": str(product_run.metrics.final_equity),
                "total_return_pct": str(product_run.metrics.total_return * 100),
                "total_commission": str(product_run.metrics.total_commission),
                "rollover_spread_pnl": str(product_run.metrics.rollover_spread_pnl),
                "rollover_count": len(product_run.roll_records),
                "rejected_intents": len(product_run.result.rejected_intents),
                "missed_executions": len(product_run.result.missed_executions),
                "degraded_bars": len(product_run.result.degraded_bars),
            }
            for product, product_run in sorted(run.products.items())
        },
        "rollover_attribution": {
            product: {key: str(value) for key, value in summary.items()}
            for product, summary in roll_attribution(run).items()
        },
        "out_of_sample": {
            "in_sample": {
                "start": str(in_sample["portfolio"].start),
                "end": str(in_sample["portfolio"].end),
                "total_return_pct": str(in_sample["portfolio"].total_return * 100),
                "max_drawdown_pct": str(in_sample["portfolio"].max_drawdown_percent * 100),
                "sharpe": str(in_sample["portfolio"].sharpe_ratio),
            },
            "holdout": {
                "start": str(out_of_sample["portfolio"].start),
                "end": str(out_of_sample["portfolio"].end),
                "total_return_pct": str(out_of_sample["portfolio"].total_return * 100),
                "max_drawdown_pct": str(out_of_sample["portfolio"].max_drawdown_percent * 100),
                "sharpe": str(out_of_sample["portfolio"].sharpe_ratio),
            },
            "rule": "保留窗不参与参数选择，仅作事后评价",
        },
        "artifacts": {"report": str(report_path)},
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "run_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {report_path}")
    print(f"wrote {manifest_path}")
    print(
        f"portfolio: {run.initial_capital} -> {run.portfolio_equity[-1][1]} | "
        f"holdout {out_of_sample['portfolio'].total_return * 100:.2f}%"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
