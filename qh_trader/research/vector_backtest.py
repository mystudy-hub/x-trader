"""[Research 层] 向量化快速扫描与参数网格 (S3-08, FR-VAL-04).

支持：
- 快速网格参数扫描 (如双均线 fast_window / slow_window)
- 纯内存轻量级计算，与事件驱动内核共用成本口径
- 筛选最优参数候选，供事件驱动通道进行完整验证
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from qh_trader.core.objects import Bar
from qh_trader.research.cost_assumptions import ResearchCostModel


@dataclass(frozen=True, slots=True)
class ScanResult:
    """参数扫描单个组合结果."""
    fast_window: int
    slow_window: int
    total_return: float
    sharpe_ratio: float
    max_drawdown_pct: float
    total_trades: int


def scan_dma_parameters(
    bars: Sequence[Bar],
    cost_model: ResearchCostModel,
    fast_range: Sequence[int] = range(3, 15, 2),
    slow_range: Sequence[int] = range(15, 60, 5),
    initial_capital: Decimal = Decimal("1000000"),
) -> list[ScanResult]:
    """快速扫描双均线参数组合."""
    if not bars:
        return []

    closes = [float(b.close) for b in bars]
    n = len(closes)
    results: list[ScanResult] = []

    # 预先计算累积和以便 O(1) 计算均线
    cumsum = [0.0] * (n + 1)
    for i, c in enumerate(closes):
        cumsum[i + 1] = cumsum[i] + c

    def get_ma(window: int) -> list[float]:
        ma = [0.0] * n
        for i in range(window - 1, n):
            ma[i] = (cumsum[i + 1] - cumsum[i + 1 - window]) / window
        return ma

    cap = float(initial_capital)
    mult = float(cost_model.multiplier)

    for fast in fast_range:
        fast_ma = get_ma(fast)
        for slow in slow_range:
            if fast >= slow or slow > n:
                continue
            slow_ma = get_ma(slow)

            pos = 0
            trades = 0
            equity = cap
            equities = [equity]
            peak = equity
            max_dd = 0.0

            for i in range(slow, n):
                prev_diff = fast_ma[i - 1] - slow_ma[i - 1]
                curr_diff = fast_ma[i] - slow_ma[i]

                # 模拟开平仓信号
                signal = 0
                if prev_diff <= 0 and curr_diff > 0:
                    signal = 1  # 金叉
                elif prev_diff >= 0 and curr_diff < 0:
                    signal = -1  # 死叉

                price = closes[i]
                cost = float(cost_model.calculate_cost_per_lot(Decimal(str(round(price, 4)))))

                if signal == 1 and pos <= 0:
                    # 平空开多
                    if pos < 0:
                        trades += 1
                        equity -= cost
                    pos = 1
                    trades += 1
                    equity -= cost
                elif signal == -1 and pos >= 0:
                    # 平多开空
                    if pos > 0:
                        trades += 1
                        equity -= cost
                    pos = -1
                    trades += 1
                    equity -= cost

                # 计算价格变动对持仓盈亏的影响
                if i > slow:
                    ret = (closes[i] - closes[i - 1]) * mult * pos
                    equity += ret

                equities.append(equity)
                if equity > peak:
                    peak = equity
                dd = (peak - equity) / peak if peak > 0 else 0.0
                if dd > max_dd:
                    max_dd = dd

            tot_ret = (equity - cap) / cap
            # 年化夏普粗略估计
            rets = [
                (equities[j] - equities[j - 1]) / equities[j - 1]
                for j in range(1, len(equities))
                if equities[j - 1] > 0
            ]
            if len(rets) > 1:
                mean_r = sum(rets) / len(rets)
                var = sum((r - mean_r) ** 2 for r in rets) / (len(rets) - 1)
                import math
                vol = math.sqrt(var * 242)
                ann_r = tot_ret * (242 / max(1, len(equities)))
                sharpe = ann_r / vol if vol > 1e-6 else 0.0
            else:
                sharpe = 0.0

            results.append(
                ScanResult(
                    fast_window=fast,
                    slow_window=slow,
                    total_return=round(tot_ret, 6),
                    sharpe_ratio=round(sharpe, 4),
                    max_drawdown_pct=round(max_dd, 6),
                    total_trades=trades,
                )
            )

    results.sort(key=lambda r: r.sharpe_ratio, reverse=True)
    return results
