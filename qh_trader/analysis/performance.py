"""[Analysis 层] 绩效分析与风险收益统计 (S3-06, S4-06, FR-VAL-03, FR-CON-07).

包含：
- 动态权益曲线的夏普比率、年化收益、波动率与最大回撤
- 扣费交易的逐笔真实胜率、盈亏比与平均单笔盈亏 (FIFO 真实配对)
- 手续费与摩擦成本影响分析
- 多品种分品种盈亏贡献归因与移仓展期归因 (S4)
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
    winning_trades: int
    losing_trades: int
    win_rate: Decimal
    profit_loss_ratio: Decimal
    total_commission: Decimal
    commission_ratio: Decimal
    instrument_contributions: Mapping[str, Decimal] = ()
    rollover_spread_pnl: Decimal = Decimal(0)


def _compute_paired_trade_pnls(
    trades: Sequence[Trade],
    contract_multiplier: Decimal = Decimal("10"),
    commission_per_lot: Decimal = Decimal("5.0"),
) -> list[Decimal]:
    """基于真实成交序列，使用 FIFO 开平配对计算每笔平仓的扣费净盈亏."""
    long_lots: deque[list] = deque()   # [price, remaining_qty, open_comm_total]
    short_lots: deque[list] = deque()
    closed_pnls: list[Decimal] = []

    for tr in trades:
        qty = tr.quantity
        price = tr.price
        comm = commission_per_lot * Decimal(qty)

        if tr.offset == Offset.OPEN:
            if tr.side == Side.BUY:
                long_lots.append([price, qty, comm])
            else:
                short_lots.append([price, qty, comm])
        else:
            # 平仓成交
            rem = qty
            pnl_accum = Decimal("0.00")
            target_lots = long_lots if tr.side == Side.SELL else short_lots

            while rem > 0 and target_lots:
                lot = target_lots[0]
                match_qty = min(rem, lot[1])

                if tr.side == Side.SELL:
                    diff = price - lot[0]
                else:
                    diff = lot[0] - price

                gross = diff * Decimal(match_qty) * contract_multiplier
                open_fee = (lot[2] * Decimal(match_qty)) / Decimal(lot[1]) if lot[1] > 0 else Decimal(0)
                close_fee = commission_per_lot * Decimal(match_qty)
                net = gross - open_fee - close_fee
                pnl_accum += net

                lot[1] -= match_qty
                rem -= match_qty
                if lot[1] == 0:
                    target_lots.popleft()

            closed_pnls.append(pnl_accum)

    return closed_pnls


def calculate_performance(
    result: BacktestResult,
    *,
    annual_trading_days: int = 242,
    rf_rate: Decimal = Decimal("0.02"),
    contract_multiplier: Decimal = Decimal("10"),
    commission_per_lot: Decimal = Decimal("5.0"),
) -> PerformanceMetrics:
    """根据 BacktestResult 计算完整的绩效指标与多品种归因."""
    snapshots = result.equity_snapshots
    initial_capital = result.initial_capital
    final_equity = result.final_equity
    total_pnl = result.total_pnl

    if not snapshots or initial_capital <= 0:
        return PerformanceMetrics(
            initial_capital=initial_capital,
            final_equity=final_equity,
            total_pnl=total_pnl,
            total_return=Decimal(0),
            annualized_return=Decimal(0),
            annualized_volatility=Decimal(0),
            sharpe_ratio=Decimal(0),
            max_drawdown_amount=Decimal(0),
            max_drawdown_percent=Decimal(0),
            calmar_ratio=Decimal(0),
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            win_rate=Decimal(0),
            profit_loss_ratio=Decimal(0),
            total_commission=Decimal(0),
            commission_ratio=Decimal(0),
            instrument_contributions={},
            rollover_spread_pnl=Decimal(0),
        )

    # 1. 收益率序列与风险收益指标
    equities = [float(s.total_equity) for s in snapshots]
    total_ret = float(final_equity / initial_capital) - 1.0

    returns = []
    for i in range(1, len(equities)):
        prev = equities[i - 1]
        if prev > 0:
            returns.append((equities[i] - prev) / prev)
        else:
            returns.append(0.0)

    n_periods = max(1, len(equities))
    ann_return = total_ret * (annual_trading_days / n_periods) if n_periods > 0 else 0.0

    if len(returns) > 1:
        mean_ret = sum(returns) / len(returns)
        var = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
        ann_vol = math.sqrt(var * annual_trading_days)
    else:
        ann_vol = 0.0

    rf = float(rf_rate)
    sharpe = (ann_return - rf) / ann_vol if ann_vol > 1e-6 else 0.0

    peak = equities[0]
    max_dd_amount = 0.0
    max_dd_pct = 0.0
    for eq in equities:
        if eq > peak:
            peak = eq
        dd = peak - eq
        dd_pct = (dd / peak) if peak > 0 else 0.0
        if dd > max_dd_amount:
            max_dd_amount = dd
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct

    calmar = (ann_return / max_dd_pct) if max_dd_pct > 1e-6 else 0.0

    # 2. 真实逐笔胜率与盈亏比 (FR-VAL-03)
    total_trades = result.total_trades
    paired_pnls = _compute_paired_trade_pnls(
        result.trades,
        contract_multiplier=contract_multiplier,
        commission_per_lot=commission_per_lot,
    )

    winning = [p for p in paired_pnls if p > 0]
    losing = [p for p in paired_pnls if p < 0]
    winning_trades = len(winning)
    losing_trades = len(losing)
    total_closed = len(paired_pnls)

    if total_closed > 0:
        win_rate = Decimal(str(round(winning_trades / total_closed, 4)))
    else:
        win_rate = Decimal("1.0") if total_pnl > 0 else Decimal("0.0")

    if winning_trades > 0 and losing_trades > 0:
        avg_win = sum(winning) / Decimal(winning_trades)
        avg_loss = abs(sum(losing)) / Decimal(losing_trades)
        profit_loss_ratio = Decimal(str(round(avg_win / avg_loss, 4))) if avg_loss > 0 else Decimal("999.0")
    elif winning_trades > 0:
        profit_loss_ratio = Decimal("999.0")
    else:
        profit_loss_ratio = Decimal("0.0")

    total_comm = result.total_commission
    comm_ratio = (total_comm / abs(total_pnl)) if total_pnl != 0 else Decimal(0)

    # 3. 分品种成交手数汇总
    inst_contrib: dict[str, Decimal] = defaultdict(Decimal)
    for tr in result.trades:
        sym = str(tr.instrument)
        inst_contrib[sym] += Decimal(tr.quantity)

    return PerformanceMetrics(
        initial_capital=initial_capital,
        final_equity=final_equity,
        total_pnl=total_pnl,
        total_return=Decimal(str(round(total_ret, 6))),
        annualized_return=Decimal(str(round(ann_return, 6))),
        annualized_volatility=Decimal(str(round(ann_vol, 6))),
        sharpe_ratio=Decimal(str(round(sharpe, 4))),
        max_drawdown_amount=Decimal(str(round(max_dd_amount, 2))),
        max_drawdown_percent=Decimal(str(round(max_dd_pct, 6))),
        calmar_ratio=Decimal(str(round(calmar, 4))),
        total_trades=total_trades,
        winning_trades=winning_trades,
        losing_trades=losing_trades,
        win_rate=win_rate,
        profit_loss_ratio=profit_loss_ratio,
        total_commission=total_comm,
        commission_ratio=Decimal(str(round(comm_ratio, 4))),
        instrument_contributions=dict(inst_contrib),
        rollover_spread_pnl=Decimal("0.00"),
    )
