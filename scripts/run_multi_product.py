#!/usr/bin/env python
"""[脚本] S4-06 多品种组合回测：主力拼接 + 真实移仓 + 样本外报告 (FR-VAL-01/02/03, FR-CON-06/07, FR-VAL-07).

用法:
    python scripts/run_multi_product.py --products rb,MA,i,m,AP,jd,au,cu --out runs/s4/multi_product

产出:
    <out>/<run_id>/report.md          多品种样本内外指标、分品种贡献、逐次移仓与未完成移仓
    <out>/<run_id>/run_manifest.json  运行清单（数据快照、代码、依赖、日历/目录版本、假设、逐品种规范哈希）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.data.storage import ParquetDataStorage  # noqa: E402
from qh_trader.research.backtest_assembly import file_sha256, git_state  # noqa: E402
from qh_trader.research.multi_product import (  # noqa: E402
    BAR_CLOSE_SETTLEMENT,
    DEFAULT_CONTRACT_CATALOG,
    DEFAULT_RESEARCH_STORAGE,
    DEFAULT_SESSION_TEMPLATE,
    roll_attribution,
    run_portfolio,
    session_split,
    split_metrics,
    write_multi_product_report,
)

DEFAULT_PRODUCTS = "rb,MA,i,m,AP,jd,au,cu"


def _sha256(path: Path) -> str | None:
    return file_sha256(path) if path.exists() else None


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="S4-06 多品种样本外组合回测")
    parser.add_argument("--products", default=DEFAULT_PRODUCTS, help="逗号分隔品种列表")
    parser.add_argument("--storage", default=str(DEFAULT_RESEARCH_STORAGE), help="研究行情存储目录 (与工程样本分开)")
    parser.add_argument("--out", default="runs/s4/multi_product", help="输出根目录 (按 run_id 建子目录)")
    parser.add_argument("--fast-window", type=int, default=5)
    parser.add_argument("--slow-window", type=int, default=20)
    parser.add_argument("--order-size", type=int, default=1)
    parser.add_argument("--capital", type=Decimal, default=Decimal("400000"), help="单品种分配资金")
    parser.add_argument("--train-ratio", type=Decimal, default=Decimal("0.6"), help="拟合窗占比")
    parser.add_argument("--interval", default="1d")
    parser.add_argument("--session-template", default=str(DEFAULT_SESSION_TEMPLATE), help="S4-05 时段模板 (schema 2)")
    parser.add_argument("--contract-catalog", default=str(DEFAULT_CONTRACT_CATALOG), help="S4-05 实际合约目录")
    parser.add_argument("--holiday-days-before", type=int, default=1, help="节前禁止新开仓的交易日数；-1 关闭")
    parser.add_argument("--max-leg-retries", type=int, default=3, help="单腿失败后的重试上限")
    args = parser.parse_args(argv)

    storage = ParquetDataStorage(ROOT / args.storage)
    products = [item.strip() for item in args.products.split(",") if item.strip()]
    holiday_days = (
        None if args.holiday_days_before is not None and args.holiday_days_before < 0 else args.holiday_days_before
    )
    run = run_portfolio(
        products,
        storage=storage,
        fast_window=args.fast_window,
        slow_window=args.slow_window,
        order_size=args.order_size,
        initial_capital_per_product=args.capital,
        train_ratio=args.train_ratio,
        interval=args.interval,
        session_template=ROOT / args.session_template,
        contract_catalog=ROOT / args.contract_catalog,
        holiday_days_before=holiday_days,
        max_leg_retries=args.max_leg_retries,
    )
    in_sample, out_of_sample = split_metrics(run)

    inputs = {
        "products": sorted(run.products),
        "storage": args.storage,
        "dataset_snapshot_id": run.dataset_snapshot_id,
        "sample": {
            "start": run.sample_start.isoformat(),
            "end": run.sample_end.isoformat(),
            "train_end_exclusive": run.train_end.isoformat(),
            "interval": args.interval,
        },
        "capital": {"per_product": str(args.capital), "total": str(run.initial_capital)},
        "signal": {
            "type": "dual_moving_average_on_continuous_series",
            "fast_window": args.fast_window,
            "slow_window": args.slow_window,
            "order_size": args.order_size,
            "adjustment": "DIFF (same-day two-contract spread; degraded switch days suppress the signal)",
        },
        "rules": {
            "session_template": {
                "path": args.session_template,
                "version": run.session_template_version,
                "sha256": _sha256(ROOT / args.session_template),
            },
            "contract_catalog": {
                "path": args.contract_catalog,
                "sha256": _sha256(ROOT / args.contract_catalog),
                "version": next((pr.dataset.catalog_version for pr in run.products.values()), None),
            },
            "economics": {
                product: {
                    "multiplier": str(pr.spec.multiplier),
                    "price_tick": str(pr.spec.price_tick),
                    "commission_per_lot": str(pr.spec.commission_per_lot),
                    "margin_ratio": str(pr.spec.margin_ratio),
                    "source": pr.spec.source,
                    "verification_status": pr.spec.verification_status,
                }
                for product, pr in sorted(run.products.items())
            },
        },
        "execution": {
            "policy": "NEXT_BAR_OPEN",
            "session_gate": "CalendarSessionGate (projected per product contracts)",
            "rollover": "RollManager CLOSE_FIRST; dominant switch effective at T+1 first matchable session",
            "max_leg_retries": args.max_leg_retries,
            "holiday_days_before": holiday_days,
            "slippage_ticks": 0,
            "participation_rate": "1.0",
        },
        "assumptions": {
            "settlement_source": BAR_CLOSE_SETTLEMENT,
            "settlement_detail": "研究模式以当日收盘价结算，非官方结算价",
            "turnover": "TURNOVER_UNAVAILABLE：免费源无成交额字段，不参与精确核算",
            "daily_open_timing": "有夜盘品种日线 Open 按交易所日线惯例视为夜盘首笔 (SYNTHETIC)，来源语义待核验 (A21)",
            "economics": "product_registry 研究假设，待规则核验 (FR-RULE-05)",
        },
        "gaps": ["GAP-S0-03 研究数据集未采购：样本区间受免费源与可用合约限制"],
        "code": git_state(ROOT),
        "environment": {
            "python_version": sys.version.split()[0],
            "platform": platform.platform(),
            "lockfile_sha256": _sha256(ROOT / "uv.lock"),
        },
    }
    input_digest = hashlib.sha256(
        json.dumps(inputs, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()
    run_id = input_digest[:16]
    out_dir = ROOT / args.out / run_id
    n = 1
    while out_dir.exists():
        out_dir = ROOT / args.out / f"{run_id}-rerun-{n}"
        n += 1
    report_path = write_multi_product_report(run, out_dir / "report.md")

    manifest = {
        "manifest_version": "2.0",
        "stage": "S4-06",
        "kind": "multi_product_research_backtest",
        "run_id": run_id,
        "input_digest": input_digest,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "inputs": inputs,
        "outputs": {
            "per_product": {
                product: {
                    "contracts": [str(instrument) for instrument in pr.dataset.contracts],
                    "canonical_hashes": pr.result.canonical_hashes(),
                    "trades": pr.metrics.total_trades,
                    "closed_trades": pr.metrics.closed_trades,
                    "session_split": session_split(pr),
                    "final_equity": str(pr.metrics.final_equity),
                    "total_return_pct": str(pr.metrics.total_return * 100),
                    "total_commission": str(pr.metrics.total_commission),
                    "rollover_spread_pnl": str(pr.metrics.rollover_spread_pnl),
                    "rollover_count": len(pr.roll_records),
                    "rollover_incomplete": len(pr.incomplete_rolls),
                    "rollovers": [
                        {
                            "from": str(r.from_instrument),
                            "to": str(r.to_instrument),
                            "side": r.side.value,
                            "quantity": r.quantity,
                            "from_price": str(r.from_price),
                            "to_price": str(r.to_price),
                            "spread": str(r.spread_cost),
                            "commission": str(r.commission),
                            "leg1_filled_at": r.leg1_filled_at.isoformat() if r.leg1_filled_at else None,
                            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                            "exposure_trading_days": r.exposure_trading_days,
                        }
                        for r in pr.roll_records
                    ],
                    "incomplete_rollovers": [
                        {
                            "roll_id": i.roll_id,
                            "from": str(i.from_instrument),
                            "to": str(i.to_instrument),
                            "state": i.state,
                            "leg1_filled": i.leg1_filled_qty,
                            "leg2_filled": i.leg2_filled_qty,
                            "remaining_exposure": i.remaining_exposure_qty,
                            "reason": i.reason,
                            "retries": i.retries,
                            "remaining_position_from": i.remaining_position_from,
                            "remaining_position_to": i.remaining_position_to,
                        }
                        for i in pr.incomplete_rolls
                    ],
                    "rejected_intents": len(pr.result.rejected_intents),
                    "holiday_rejections": pr.holiday_rejections,
                    "missed_executions": len(pr.result.missed_executions),
                    "degraded_bars": len(pr.result.degraded_bars),
                    "signal_suppressed_days": [d.isoformat() for d in pr.signal_suppressed_days],
                    "adjustment_degradations": len(pr.dataset.adjustment_degradations),
                    "dominant_mapping": [
                        {
                            "instrument": str(e.instrument),
                            "effective_from": e.effective_from.isoformat(),
                            "effective_to": e.effective_to.isoformat() if e.effective_to else None,
                            "decision_time": e.decision_time.isoformat(),
                            "basis": e.effective_basis,
                        }
                        for e in pr.dataset.resolver.entries
                    ],
                }
                for product, pr in sorted(run.products.items())
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
            "portfolio": {
                "initial": str(run.initial_capital),
                "final": str(run.portfolio_equity[-1][1]),
                "equity_points": len(run.portfolio_equity),
            },
        },
        "artifacts": {"report": str(report_path.relative_to(ROOT))},
    }
    manifest_path = out_dir / "run_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"wrote {report_path}")
    print(f"wrote {manifest_path}")
    print(
        f"portfolio: {run.initial_capital} -> {run.portfolio_equity[-1][1]} | "
        f"holdout {out_of_sample['portfolio'].total_return * 100:.2f}% | run_id {run_id}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
