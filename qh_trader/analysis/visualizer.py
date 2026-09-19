"""[Analysis 层] 绩效报告格式化与权益曲线输出 (S3-06, S4-06, FR-VAL-03)."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from qh_trader.analysis.performance import PerformanceMetrics
from qh_trader.engine.backtest_engine import EquitySnapshot


def format_performance_summary(metrics: PerformanceMetrics) -> str:
    """生成控制台绩效报告；口径声明与指标一起输出."""
    line = "=" * 54
    thin = "-" * 54
    lines = [
        line,
        "              回测绩效分析报告 (S3-06 / S4-06)",
        line,
        f"样本区间:           {metrics.sample_start} ~ {metrics.sample_end} ({metrics.trading_days} 个交易日)",
        f"初始资金:           {metrics.initial_capital:>15,.2f} 元",
        f"期末总权益:         {metrics.final_equity:>15,.2f} 元",
        f"累计净盈亏:         {metrics.total_pnl:>15,.2f} 元",
        f"累计收益率:         {metrics.total_return * 100:>14.2f} %",
        f"年化收益率:         {metrics.annualized_return * 100:>14.2f} %",
        f"年化波动率:         {metrics.annualized_volatility * 100:>14.2f} %",
        f"夏普比率 (Sharpe):  {metrics.sharpe_ratio:>15.2f}",
        f"最大回撤金额:       {metrics.max_drawdown_amount:>15,.2f} 元",
        f"最大回撤比例:       {metrics.max_drawdown_percent * 100:>14.2f} %",
        f"卡玛比率 (Calmar):  {metrics.calmar_ratio:>15.2f}",
        f"保证金占用峰值:     {metrics.peak_margin_used:>15,.2f} 元",
        thin,
        f"总成交记录数:       {metrics.total_trades:>15d} 笔",
        f"平仓配对笔数:       {metrics.closed_trades:>15d} 笔"
        f" (胜 {metrics.winning_trades} / 负 {metrics.losing_trades})",
        f"平仓胜率:           {metrics.win_rate * 100:>14.2f} %",
        f"盈亏比:             {metrics.profit_loss_ratio:>15.2f}",
        f"平均持仓交易日:     {metrics.average_holding_days:>15.2f}",
        f"换手率 (名义/权益): {metrics.turnover_ratio:>15.2f}",
        f"累计交易手续费:     {metrics.total_commission:>15,.2f} 元",
        f"手续费占毛盈亏:     {metrics.commission_ratio * 100:>14.2f} %",
        f"拒单 / 错过执行 / 未成交: {metrics.rejected_intents} / {metrics.missed_executions}"
        f" / {metrics.unfilled_orders}",
    ]

    if metrics.monthly_returns:
        lines.append(thin)
        lines.append("月度收益率:")
        for month, ret in metrics.monthly_returns.items():
            lines.append(f"  {month}: {ret * 100:>8.2f} %")

    if metrics.instrument_contributions:
        lines.append(thin)
        lines.append("分品种平仓净盈亏 / 成交手数:")
        for inst_sym, pnl in metrics.instrument_contributions.items():
            lots = metrics.instrument_volume.get(inst_sym, 0)
            lines.append(f"  {inst_sym:<20s}: {pnl:>12,.2f} 元 / {lots:>6d} 手")

    lines.append(thin)
    lines.append(
        f"口径: 收益频率={metrics.return_frequency}; 年化因子={metrics.annual_trading_days}; "
        f"无风险利率={metrics.risk_free_rate}; 外部现金流={metrics.external_cash_flow}; "
        "权益含未平仓估值; 胜率/盈亏比按账本逐笔平仓事实 (含实际手续费)"
    )
    lines.append(line)
    return "\n".join(lines)


def equity_curve_csv(snapshots: Sequence[EquitySnapshot]) -> str:
    """扣费权益曲线 CSV (含未平仓估值、保证金占用与持仓)."""
    rows = ["timestamp,trading_day,balance,total_equity,margin_used,long_position,short_position,mark_price"]
    for s in snapshots:
        rows.append(
            f"{s.timestamp.isoformat()},{s.trading_day.isoformat()},{s.balance},{s.total_equity},"
            f"{s.margin_used},{s.long_position},{s.short_position},{s.mark_price}"
        )
    return "\n".join(rows) + "\n"


def equity_curve_svg(snapshots: Sequence[EquitySnapshot], *, width: int = 960, height: int = 320) -> str:
    """无依赖的 SVG 权益曲线 (供报告归档)."""
    if not snapshots:
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"></svg>'
    values = [s.total_equity for s in snapshots]
    low, high = min(values), max(values)
    span = high - low if high != low else Decimal(1)
    pad = 24
    points = []
    n = len(values)
    for i, v in enumerate(values):
        x = pad + (width - 2 * pad) * (Decimal(i) / Decimal(max(1, n - 1)))
        y = pad + (height - 2 * pad) * (1 - (v - low) / span)
        points.append(f"{x:.1f},{y:.1f}")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        f'<rect width="100%" height="100%" fill="#ffffff"/>'
        f'<polyline fill="none" stroke="#2563eb" stroke-width="1.5" points="{" ".join(points)}"/>'
        f'<text x="{pad}" y="{pad - 8}" font-size="12" fill="#333">'
        f"total equity {snapshots[0].trading_day} ~ {snapshots[-1].trading_day} | min {low:,.2f} max {high:,.2f}</text>"
        "</svg>"
    )
