"""[Analysis 层] 绩效分析与风险收益统计 (S3-06, S4-06, FR-VAL-03, FR-OPS-03).

口径声明 (写入报告)：
- 收益序列 = 逐 Bar 含未平仓估值的总权益；年化按"交易日数 / annual_trading_days"折算，不按 Bar 数；
- 波动率与夏普按交易日收益 (同一交易日多根 Bar 先合并为日权益) 计算，注明无风险假设；
- 胜率 / 盈亏比 / 持仓时长使用账本的逐笔平仓事实 (ClosedTradeRecord，含实际扣费)，不重算手续费；
- 换手 = 成交名义金额 / 平均权益；费用贡献 = 手续费 / 毛盈亏；保证金峰值来自逐 Bar 占用保证金；
- 未成交 / 拒单 / 错过执行 / 数据降级来自引擎结果，工程验收以账务与时序正确性为依据。
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from qh_trader.core.constants import Offset, Side
from qh_trader.core.objects import Trade
from qh_trader.engine.backtest_engine import BacktestResult, EquitySnapshot


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    """策略回测绩效评估指标."""

    initial_capital: Decimal
    final_equity: Decimal
    total_pnl: Decimal
    total_return: Decimal
    annualized_return: Decimal
    annualized_volatility: Decimal
    sharpe_ratio: Decimal
    max_drawdown_amount: Decimal
    max_drawdown_percent: Decimal
    calmar_ratio: Decimal
    total_trades: int
    closed_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: Decimal
    profit_loss_ratio: Decimal
    average_holding_days: Decimal
    total_commission: Decimal
    commission_ratio: Decimal
    turnover_ratio: Decimal
    peak_margin_used: Decimal
    trading_days: int
    return_frequency: str
    annual_trading_days: int
    risk_free_rate: Decimal
    external_cash_flow: Decimal
    monthly_returns: Mapping[str, Decimal] = field(default_factory=dict)
    instrument_contributions: Mapping[str, Decimal] = field(default_factory=dict)
    instrument_volume: Mapping[str, int] = field(default_factory=dict)
    rejected_intents: int = 0
    missed_executions: int = 0
    unfilled_orders: int = 0
    sample_start: date | None = None
    sample_end: date | None = None
    rollover_spread_pnl: Decimal = Decimal(0)


def _daily_equity(snapshots: Sequence[EquitySnapshot]) -> list[tuple[date, Decimal]]:
    """同一交易日多根 Bar 取最后一根的权益."""
    by_day: dict[date, Decimal] = {}
    for snap in snapshots:
        by_day[snap.trading_day] = snap.total_equity
    return sorted(by_day.items())


def _decimal(value: float, places: int) -> Decimal:
    return Decimal(str(round(value, places)))


def _holding_days(result: BacktestResult) -> Decimal:
    """按平仓记录匹配的开仓批次估算平均持仓交易日数 (FIFO 配对)."""
    open_dates: dict[str, date] = {t.trade_id: t.trading_day for t in result.trades if t.offset == Offset.OPEN}
    total = Decimal(0)
    count = 0
    for record in result.closed_trades:
        for lot_id in record.matched_lot_ids:
            opened = open_dates.get(lot_id)
            if opened is None:
                continue
            total += Decimal((record.close_date - opened).days)
            count += 1
    return (total / Decimal(count)).quantize(Decimal("0.01")) if count else Decimal(0)


def calculate_performance(
    result: BacktestResult,
    *,
    annual_trading_days: int = 242,
    rf_rate: Decimal = Decimal("0.02"),
) -> PerformanceMetrics:
    """根据 BacktestResult 计算绩效指标；全部金额口径来自账本，不重算成交成本."""
    snapshots = result.equity_snapshots
    initial_capital = result.initial_capital
    final_equity = result.final_equity
    total_pnl = result.total_pnl
    external_cash_flow = Decimal(0)

    daily = _daily_equity(snapshots)
    trading_days = len(daily)
    zero = Decimal(0)
    if trading_days == 0 or initial_capital <= 0:
        return PerformanceMetrics(
            initial_capital=initial_capital,
            final_equity=final_equity,
            total_pnl=total_pnl,
            total_return=zero,
            annualized_return=zero,
            annualized_volatility=zero,
            sharpe_ratio=zero,
            max_drawdown_amount=zero,
            max_drawdown_percent=zero,
            calmar_ratio=zero,
            total_trades=0,
            closed_trades=0,
            winning_trades=0,
            losing_trades=0,
            win_rate=zero,
            profit_loss_ratio=zero,
            average_holding_days=zero,
            total_commission=result.total_commission,
            commission_ratio=zero,
            turnover_ratio=zero,
            peak_margin_used=zero,
            trading_days=0,
            return_frequency="daily",
            annual_trading_days=annual_trading_days,
            risk_free_rate=rf_rate,
            external_cash_flow=external_cash_flow,
        )

    # 1. 收益、年化、波动、夏普 (交易日频率)
    equities = [float(eq) for _, eq in daily]
    series = [float(initial_capital)] + equities
    total_ret = float((final_equity - initial_capital) / initial_capital)
    years = trading_days / annual_trading_days
    ann_return = (1.0 + total_ret) ** (1.0 / years) - 1.0 if years > 0 and total_ret > -1.0 else total_ret
    daily_returns = [(series[i] - series[i - 1]) / series[i - 1] for i in range(1, len(series)) if series[i - 1] > 0]
    if len(daily_returns) > 1:
        mean_r = sum(daily_returns) / len(daily_returns)
        var = sum((r - mean_r) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
        ann_vol = math.sqrt(var * annual_trading_days)
    else:
        ann_vol = 0.0
    sharpe = (ann_return - float(rf_rate)) / ann_vol if ann_vol > 1e-12 else 0.0

    # 2. 回撤 (含未平仓估值的逐 Bar 权益)
    peak = float(initial_capital)
    max_dd_amount = 0.0
    max_dd_pct = 0.0
    for snap in snapshots:
        eq = float(snap.total_equity)
        peak = max(peak, eq)
        dd = peak - eq
        max_dd_amount = max(max_dd_amount, dd)
        if peak > 0:
            max_dd_pct = max(max_dd_pct, dd / peak)
    calmar = ann_return / max_dd_pct if max_dd_pct > 1e-12 else 0.0

    # 3. 月度收益 (交易日权益，按自然月末)
    monthly: dict[str, Decimal] = {}
    last_month_equity = initial_capital
    month_key = None
    month_end_equity = initial_capital
    for day, eq in daily:
        key = f"{day.year:04d}-{day.month:02d}"
        if month_key is not None and key != month_key:
            monthly[month_key] = ((month_end_equity - last_month_equity) / last_month_equity).quantize(
                Decimal("0.000001")
            )
            last_month_equity = month_end_equity
        month_key = key
        month_end_equity = eq
    if month_key is not None:
        monthly[month_key] = ((month_end_equity - last_month_equity) / last_month_equity).quantize(Decimal("0.000001"))

    # 4. 逐笔平仓统计 (账本事实，已含实际手续费)
    closed = result.closed_trades
    net_pnls = [record.trade_close_pnl - record.commission for record in closed]
    winning = [p for p in net_pnls if p > 0]
    losing = [p for p in net_pnls if p < 0]
    if net_pnls:
        win_rate = (Decimal(len(winning)) / Decimal(len(net_pnls))).quantize(Decimal("0.0001"))
    else:
        win_rate = zero
    if winning and losing:
        avg_win = sum(winning) / Decimal(len(winning))
        avg_loss = abs(sum(losing)) / Decimal(len(losing))
        profit_loss_ratio = (avg_win / avg_loss).quantize(Decimal("0.0001")) if avg_loss > 0 else zero
    else:
        profit_loss_ratio = zero

    # 5. 费用贡献、换手、保证金峰值
    gross_pnl = total_pnl + result.total_commission
    commission_ratio = (
        (result.total_commission / abs(gross_pnl)).quantize(Decimal("0.0001")) if gross_pnl != 0 else zero
    )
    notional = Decimal(0)
    volume: dict[str, int] = defaultdict(int)
    contribution: dict[str, Decimal] = defaultdict(Decimal)
    multipliers = {str(r.instrument): r.multiplier for r in closed}
    for trade in result.trades:
        sym = str(trade.instrument)
        volume[sym] += trade.quantity
        notional += trade.price * Decimal(trade.quantity) * multipliers.get(sym, Decimal(1))
    for record in closed:
        contribution[str(record.instrument)] += record.trade_close_pnl - record.commission
    avg_equity = sum((eq for _, eq in daily), Decimal(0)) / Decimal(trading_days)
    turnover = (notional / avg_equity).quantize(Decimal("0.0001")) if avg_equity > 0 else zero
    peak_margin = max((s.margin_used for s in snapshots), default=zero)

    return PerformanceMetrics(
        initial_capital=initial_capital,
        final_equity=final_equity,
        total_pnl=total_pnl,
        total_return=_decimal(total_ret, 6),
        annualized_return=_decimal(ann_return, 6),
        annualized_volatility=_decimal(ann_vol, 6),
        sharpe_ratio=_decimal(sharpe, 4),
        max_drawdown_amount=_decimal(max_dd_amount, 2),
        max_drawdown_percent=_decimal(max_dd_pct, 6),
        calmar_ratio=_decimal(calmar, 4),
        total_trades=result.total_trades,
        closed_trades=len(closed),
        winning_trades=len(winning),
        losing_trades=len(losing),
        win_rate=win_rate,
        profit_loss_ratio=profit_loss_ratio,
        average_holding_days=_holding_days(result),
        total_commission=result.total_commission,
        commission_ratio=commission_ratio,
        turnover_ratio=turnover,
        peak_margin_used=peak_margin,
        trading_days=trading_days,
        return_frequency="daily",
        annual_trading_days=annual_trading_days,
        risk_free_rate=rf_rate,
        external_cash_flow=external_cash_flow,
        monthly_returns=dict(monthly),
        instrument_contributions=dict(contribution),
        instrument_volume=dict(volume),
        rejected_intents=len(result.rejected_intents),
        missed_executions=len(result.missed_executions),
        unfilled_orders=len(result.unfilled_orders),
        sample_start=result.first_trading_day,
        sample_end=result.last_trading_day,
        rollover_spread_pnl=Decimal("0.00"),
    )


def trades_by_side(trades: Sequence[Trade]) -> dict[str, int]:
    """按方向汇总成交手数 (报告辅助)."""
    counts: dict[str, int] = {Side.BUY.value: 0, Side.SELL.value: 0}
    for trade in trades:
        counts[trade.side.value] += trade.quantity
    return counts
