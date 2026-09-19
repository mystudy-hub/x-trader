"""[Analysis 层] 绩效可视化与控制台报告格式化 (S3-06, FR-VAL-03)."""

from __future__ import annotations

from qh_trader.analysis.performance import PerformanceMetrics
from qh_trader.engine.backtest_engine import BacktestResult


def format_performance_summary(metrics: PerformanceMetrics) -> str:
    """生成整洁规范的 ASCII / Markdown 绩效报告."""
    lines = [
        "==================================================",
        "              回测绩效分析报告 (S3-06)            ",
        "==================================================",
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
        "--------------------------------------------------",
        f"总交易记录数:       {metrics.total_trades:>15d} 笔",
        f"累计交易手续费:     {metrics.total_commission:>15,.2f} 元",
        f"手续费占盈亏比:     {metrics.commission_ratio * 100:>14.2f} %",
        "==================================================",
    ]
    return "\n".join(lines)
