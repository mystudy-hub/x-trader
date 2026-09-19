"""[Research 层] 向量化快速扫描 (S3-08, FR-VAL-04, FR-VAL-08).

与事件驱动通道共用执行时点与成本口径：
- 信号在 Bar i 收盘可见；持仓最早从 Bar i+1 开盘生效，执行价 = open[i+1] ± 滑点 (与 SimulatedGateway 开盘撮合一致)；
- 持仓收益从 Bar i+1 开盘起算，绝不把 Bar i 的涨跌计入在 Bar i 收盘才决定的持仓；
- 手续费按 ResearchCostModel 每手计一次，滑点计入执行价而不是另计成本；
- 结果只用于筛选候选参数，进入成交 / 资金 / 风控评估仍须运行事件驱动回测。

向量化通道不模拟成交量预算、涨跌停流动性与订单有效期；这些差异由 FR-VAL-08 容差测试限定。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from qh_trader.core.objects import Bar
from qh_trader.research.cost_assumptions import ResearchCostModel


@dataclass(frozen=True, slots=True)
class VectorFill:
    """向量化通道的一次模拟成交 (用于与事件驱动通道逐笔对照)."""

    bar_index: int
    side: str  # "BUY" / "SELL"
    offset: str  # "OPEN" / "CLOSE"
    price: Decimal
    quantity: int


@dataclass(frozen=True, slots=True)
class ScanResult:
    """参数扫描单个组合结果."""

    fast_window: int
    slow_window: int
    total_return: float
    sharpe_ratio: float
    max_drawdown_pct: float
    total_trades: int
    final_equity: Decimal
    total_commission: Decimal
    fills: tuple[VectorFill, ...] = ()


def dma_signals(closes: Sequence[Decimal], fast: int, slow: int) -> list[int]:
    """与 DualMovingAverageStrategy 相同的信号规则：返回每根 Bar 收盘时的信号 (1 金叉, -1 死叉, 0 无)."""
    if fast >= slow:
        raise ValueError("fast_window must be smaller than slow_window")
    n = len(closes)
    signals = [0] * n
    prev_diff: Decimal | None = None
    for i in range(slow - 1, n):
        window = closes[i - slow + 1 : i + 1]
        fast_ma = sum(window[-fast:]) / Decimal(fast)
        slow_ma = sum(window) / Decimal(slow)
        diff = fast_ma - slow_ma
        if prev_diff is not None:
            if prev_diff <= 0 and diff > 0:
                signals[i] = 1
            elif prev_diff >= 0 and diff < 0:
                signals[i] = -1
        prev_diff = diff
    return signals


def simulate_dma(
    bars: Sequence[Bar],
    cost_model: ResearchCostModel,
    fast: int,
    slow: int,
    *,
    initial_capital: Decimal = Decimal("1000000"),
    order_size: int = 1,
    annual_trading_days: int = 242,
) -> ScanResult:
    """单组参数的向量化回测；成交在信号后的下一 Bar 开盘，收益从成交后开始累计."""
    closes = [b.close for b in bars]
    opens = [b.open for b in bars]
    n = len(bars)
    signals = dma_signals(closes, fast, slow)
    mult = cost_model.multiplier
    slip = Decimal(cost_model.slippage_ticks) * cost_model.price_tick

    pos = 0
    entry_price: Decimal | None = None
    cash = initial_capital  # 已实现权益 (扣费)
    commission_total = Decimal(0)
    fills: list[VectorFill] = []
    equities: list[Decimal] = []

    def execute(index: int, side: str, offset: str, qty: int) -> Decimal:
        nonlocal cash, commission_total
        price = opens[index] + slip if side == "BUY" else opens[index] - slip
        fee = cost_model.commission_per_lot * Decimal(qty) + price * mult * cost_model.commission_rate * Decimal(qty)
        commission_total += fee
        cash -= fee
        fills.append(VectorFill(index, side, offset, price, qty))
        return price

    for i in range(n):
        # 1. 上一根 Bar 收盘信号在本根开盘执行
        if i > 0:
            sig = signals[i - 1]
            if sig == 1 and pos <= 0:
                if pos < 0:
                    price = execute(i, "BUY", "CLOSE", -pos)
                    assert entry_price is not None
                    cash += (entry_price - price) * mult * Decimal(-pos)
                    pos = 0
                entry_price = execute(i, "BUY", "OPEN", order_size)
                pos = order_size
            elif sig == -1 and pos >= 0:
                if pos > 0:
                    price = execute(i, "SELL", "CLOSE", pos)
                    assert entry_price is not None
                    cash += (price - entry_price) * mult * Decimal(pos)
                    pos = 0
                entry_price = execute(i, "SELL", "OPEN", order_size)
                pos = -order_size
        # 2. 本根收盘估值 (含未平仓浮动盈亏)
        unrealized = Decimal(0)
        if pos != 0 and entry_price is not None:
            unrealized = (closes[i] - entry_price) * mult * Decimal(pos)
        equities.append(cash + unrealized)

    final_equity = equities[-1] if equities else initial_capital
    total_return = float((final_equity - initial_capital) / initial_capital)
    returns = [
        float((equities[j] - equities[j - 1]) / equities[j - 1]) for j in range(1, len(equities)) if equities[j - 1] > 0
    ]
    if len(returns) > 1:
        mean_r = sum(returns) / len(returns)
        var = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        vol = math.sqrt(var * annual_trading_days)
        ann_r = mean_r * annual_trading_days
        sharpe = ann_r / vol if vol > 1e-12 else 0.0
    else:
        sharpe = 0.0
    peak = equities[0] if equities else initial_capital
    max_dd = Decimal(0)
    for eq in equities:
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)

    return ScanResult(
        fast_window=fast,
        slow_window=slow,
        total_return=round(total_return, 6),
        sharpe_ratio=round(sharpe, 4),
        max_drawdown_pct=round(float(max_dd), 6),
        total_trades=len(fills),
        final_equity=final_equity,
        total_commission=commission_total,
        fills=tuple(fills),
    )


def scan_dma_parameters(
    bars: Sequence[Bar],
    cost_model: ResearchCostModel,
    fast_range: Sequence[int] = range(3, 15, 2),
    slow_range: Sequence[int] = range(15, 60, 5),
    initial_capital: Decimal = Decimal("1000000"),
    order_size: int = 1,
) -> list[ScanResult]:
    """快速扫描双均线参数组合，按夏普降序."""
    if not bars:
        return []
    results: list[ScanResult] = []
    for fast in fast_range:
        for slow in slow_range:
            if fast >= slow or slow > len(bars):
                continue
            results.append(
                simulate_dma(bars, cost_model, fast, slow, initial_capital=initial_capital, order_size=order_size)
            )
    results.sort(key=lambda r: (r.sharpe_ratio, r.total_return), reverse=True)
    return results
