#!/usr/bin/env python
"""[S3-06 / FR-VAL-03] 由 run_manifest.json 生成 Markdown 回测报告 (含假设、口径、拒单与敏感性)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _f(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def generate_markdown_report(manifest: dict, sensitivity: dict | None = None) -> str:
    inputs = manifest.get("inputs", {})
    spec = inputs.get("spec", {})
    data = inputs.get("data", {})
    rules = inputs.get("rules", {})
    execution = inputs.get("execution", {})
    matching = execution.get("matching_assumptions", {})
    outputs = manifest.get("outputs", {})
    summary = outputs.get("summary", {})
    conventions = outputs.get("metric_conventions", {})
    hashes = outputs.get("canonical_hashes", {})
    code = inputs.get("code", {})
    lines = [
        f"# 回测绩效与实验报告: {manifest.get('instrument', 'Unknown')}",
        "",
        f"- run_id: `{manifest.get('run_id')}`；输入摘要 `{manifest.get('input_digest', '')[:16]}`；生成于 {manifest.get('created_at')}",
        "",
        "## 一、实验元数据",
        f"- **代码提交**: `{code.get('commit')}`（工作区有未提交改动: {code.get('dirty')}）",
        f"- **Python / 平台**: `{inputs.get('environment', {}).get('python_version')}` / {inputs.get('environment', {}).get('platform')}",
        f"- **依赖锁 (uv.lock sha256)**: `{inputs.get('environment', {}).get('lockfile_sha256')}`",
        f"- **数据集快照**: `{data.get('dataset_snapshot', {}).get('snapshot_id')}`；来源版本 {data.get('source_versions')}",
        f"- **数据周期与区间**: {spec.get('interval')}，{data.get('bar_count')} 根 Bar，{data.get('start_time')} ~ {data.get('end_time')}",
        f"- **结算价来源**: {data.get('settlement_source')}（{data.get('settlement_days')} 个交易日有官方结算价）",
        f"- **合约目录**: {rules.get('contract_catalog', {}).get('version')} (`{str(rules.get('contract_catalog', {}).get('sha256'))[:12]}`)",
        f"- **交易日历 / 时段版本**: {(rules.get('calendar') or {}).get('version')}",
        f"- **合约经济参数**: {json.dumps(rules.get('instrument_economics'), ensure_ascii=False)}",
        "",
        "## 二、执行与撮合假设 (FR-MATCH-03/05, FR-EXEC-02)",
        f"- **执行时点策略**: {execution.get('execution_policy')}；错过执行: {execution.get('missed_execution')}",
        f"- **信号 / 执行数据分辨率**: {execution.get('signal_resolution')} / {execution.get('execution_resolution')}",
        f"- **滑点跳数**: {matching.get('slippage_ticks')}；价格步长 {matching.get('price_tick')}",
        f"- **参与率预算**: {matching.get('participation_rate')}（共享预算；整根 Bar 成交量为事后容量近似）",
        f"- **涨跌停流动性情景**: {matching.get('limit_liquidity_scenario')}（研究假设，不代表市场事实）",
        f"- **盘中触价规则**: {matching.get('intrabar_touch_rule')}；含竞价 Bar 开盘: {matching.get('auction_fill_policy')}",
        f"- **成交时刻近似**: 开盘候选={matching.get('open_fill_time')}；盘中={matching.get('intrabar_fill_time')}",
        f"- **报单 / 撤单到达延迟**: {matching.get('order_delay_ms')} ms / {matching.get('cancel_delay_ms')} ms；订单有效期 {matching.get('order_validity')}",
        f"- **同时刻事件优先级**: {execution.get('event_priorities')}；随机种子 {execution.get('random_seed')}",
        "",
        "## 三、交易统计与盈亏表现",
        f"- **样本区间**: {summary.get('sample_start')} ~ {summary.get('sample_end')}（{summary.get('trading_days')} 个交易日）",
        f"- **期末总权益**: {_f(summary.get('final_equity')):,.2f} 元；累计净盈亏 {_f(summary.get('total_pnl')):,.2f} 元",
        f"- **累计 / 年化收益率**: {_f(summary.get('total_return_pct')):.2f} % / {_f(summary.get('annualized_return_pct')):.2f} %",
        f"- **年化波动率**: {_f(summary.get('annualized_volatility_pct')):.2f} %；夏普 {_f(summary.get('sharpe_ratio')):.2f}；卡玛 {_f(summary.get('calmar_ratio')):.2f}",
        f"- **最大回撤**: {_f(summary.get('max_drawdown_pct')):.2f} %",
        f"- **保证金占用峰值**: {_f(summary.get('peak_margin_used')):,.2f} 元；换手率 {_f(summary.get('turnover_ratio')):.2f}",
        f"- **成交 / 平仓配对**: {summary.get('total_trades')} 笔 / {summary.get('closed_trades')} 笔；平均持仓 {_f(summary.get('average_holding_days')):.2f} 个交易日",
        f"- **平仓胜率 / 盈亏比**: {_f(summary.get('win_rate_pct')):.2f} % / {_f(summary.get('profit_loss_ratio')):.2f}",
        f"- **手续费**: {_f(summary.get('total_commission')):,.2f} 元，占毛盈亏 {_f(summary.get('commission_ratio_pct')):.2f} %",
        f"- **拒单 / 错过执行 / 未成交**: {summary.get('rejected_intents')} / {summary.get('missed_executions')} / {summary.get('unfilled_orders')}",
        "",
        "### 月度收益率",
        "| 月份 | 收益率 |",
        "| :--- | ---: |",
    ]
    for month, ret in (summary.get("monthly_returns") or {}).items():
        lines.append(f"| {month} | {_f(ret) * 100:.2f} % |")
    lines += [
        "",
        "## 四、指标口径 (FR-VAL-03)",
        f"- 收益频率 {conventions.get('return_frequency')}；年化因子 {conventions.get('annual_trading_days')}；无风险利率 {conventions.get('risk_free_rate')}；外部现金流 {conventions.get('external_cash_flow')}",
        f"- 权益含未平仓估值: {conventions.get('equity_includes_open_positions')}；配对规则: {conventions.get('pairing_rule')}",
        "- 工程验收以账务、时序与故障恢复正确性为依据；上述收益仅为研究样例，样本覆盖周期见区间。",
        "",
        "## 五、规范输出哈希 (A15 / FR-VAL-07)",
        f"- orders `{hashes.get('orders')}`",
        f"- trades `{hashes.get('trades')}`",
        f"- ledger `{hashes.get('ledger')}`",
        f"- equity_curve `{hashes.get('equity_curve')}`",
    ]
    rejected = outputs.get("rejected_intents") or []
    if rejected:
        lines += [
            "",
            "## 六、风控拒单明细 (FR-RISK-03)",
            "| 订单 | 策略 | 时刻 | 阶段 | 原因 |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ]
        for r in rejected[:200]:
            lines.append(
                f"| {r.get('client_order_id')} | {r.get('strategy_id')} | {r.get('at')} | {r.get('stage')} | {r.get('reason')} |"
            )
    if sensitivity:
        lines += [
            "",
            "## 七、敏感性分析 (FR-VAL-02)",
            f"- 试验数 {sensitivity.get('trial_count')}，失败 {sensitivity.get('failure_count')}；维度 {json.dumps(sensitivity.get('dimensions'), ensure_ascii=False)}",
            "| 试验 | 参数 | 结果 |",
            "| :--- | :--- | :--- |",
        ]
        for t in sensitivity.get("trials", []):
            body = json.dumps(t.get("summary"), ensure_ascii=False) if t.get("succeeded") else f"失败: {t.get('error')}"
            lines.append(f"| {t.get('trial_id')} | {json.dumps(t.get('parameters'), ensure_ascii=False)} | {body} |")
    lines += [
        "",
        "---",
        "*本报告由 QH-Trader 回测系统生成；数据来自不可变 Parquet 快照与事件账本，回放结果为离线验证证据。*",
    ]
    return "\n".join(lines)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Generate report from run_manifest.json")
    parser.add_argument("--manifest", "-m", required=True, help="Path to run_manifest.json")
    parser.add_argument("--sensitivity", default=None, help="Optional sensitivity.json produced by scan_params.py")
    parser.add_argument(
        "--output", "-o", default=None, help="Output markdown path (default: report.md beside the manifest)"
    )
    args = parser.parse_args()

    m_path = Path(args.manifest)
    if not m_path.is_absolute():
        m_path = ROOT / m_path
    if not m_path.exists():
        print(f"Manifest not found: {m_path}", file=sys.stderr)
        return 1
    manifest = json.loads(m_path.read_text(encoding="utf-8"))
    sensitivity = None
    if args.sensitivity:
        s_path = Path(args.sensitivity)
        s_path = s_path if s_path.is_absolute() else ROOT / s_path
        sensitivity = json.loads(s_path.read_text(encoding="utf-8"))

    out_path = Path(args.output) if args.output else m_path.parent / "report.md"
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(generate_markdown_report(manifest, sensitivity), encoding="utf-8")
    print(f"报告已生成至: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
