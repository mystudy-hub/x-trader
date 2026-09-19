#!/usr/bin/env python
"""[S3-06 / FR-VAL-03] 回测报告生成脚本."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def generate_markdown_report(manifest: dict) -> str:
    """根据 run_manifest 生成易于阅读与归档的 Markdown 报告."""
    p = manifest.get("parameters", {})
    s = manifest.get("summary", {})
    lines = [
        f"# 回测绩效与实验报告: {manifest.get('instrument', 'Unknown')}",
        "",
        "## 一、 实验元数据与配置",
        f"- **合约品种**: {manifest.get('instrument')}",
        f"- **代码提交 (Git)**: `{manifest.get('git_commit', 'N/A')}`",
        f"- **Python 版本**: `{manifest.get('python_version', 'N/A')}`",
        f"- **数据快照 ID**: `{manifest.get('snapshot_id', 'N/A')}`",
        f"- **数据周期**: {manifest.get('interval')} ({manifest.get('bar_count')} 根 Bar)",
        f"- **区间范围**: {manifest.get('start_time')} ~ {manifest.get('end_time')}",
        f"- **合约目录版本**: {manifest.get('catalog_version')}",
        f"- **初始资金**: {float(p.get('initial_capital', 0)):,.2f} 元",
        f"- **滑点跳数**: {p.get('slippage_ticks')} 跳",
        f"- **参与率上限**: {p.get('participation_rate')}",
        f"- **涨跌停流动性情景**: {p.get('limit_liquidity_scenario')}",
        f"- **策略参数**: 快均线={p.get('fast_window')}, 慢均线={p.get('slow_window')}, 委托手数={p.get('order_size')}",
        "",
        "## 二、 交易统计与盈亏表现",
        f"- **期末总权益**: {float(s.get('final_equity', 0)):,.2f} 元",
        f"- **累计净盈亏**: {float(s.get('total_pnl', 0)):,.2f} 元",
        f"- **累计收益率**: {float(s.get('total_return_pct', 0)):.2f} %",
        f"- **夏普比率 (Sharpe)**: {float(s.get('sharpe_ratio', 0)):.2f}",
        f"- **最大回撤比例**: {float(s.get('max_drawdown_pct', 0)):.2f} %",
        f"- **平仓胜率**: {float(s.get('win_rate_pct', 0)):.2f} %",
        f"- **盈亏比**: {float(s.get('profit_loss_ratio', 0)):.2f}",
        f"- **累计手续费**: {float(s.get('total_commission', 0)):,.2f} 元",
        f"- **总成交笔数**: {s.get('total_trades')} 笔",
        "",
        "---",
        "*本报告由 QH-Trader 自动交易与回测系统生成，数据源自不可变 Parquet 与事件账本。*",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Report from run_manifest.json")
    parser.add_argument("--manifest", "-m", default="runs/backtest/run_manifest.json", help="Manifest path")
    parser.add_argument("--output", "-o", default="runs/backtest/report.md", help="Output markdown path")
    args = parser.parse_args()

    m_path = ROOT / args.manifest
    if not m_path.exists():
        print(f"Manifest not found: {m_path}", file=sys.stderr)
        return 1

    with open(m_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    report_md = generate_markdown_report(manifest)
    out_path = ROOT / args.output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report_md)

    print(f"报告已生成至: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
