"""[Strategy 示例] 双均线趋势跟踪策略 (DualMovingAverageStrategy).

经典 CTA 均线金叉开多/平空、死叉开空/平多策略：
- 严格基于当时可见的已完成 Bar 序列计算均线指标；
- 通过 StrategyContext 提交标准下单意图；
- 不含任何未来信息。
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal

from qh_trader.core.constants import Offset, OrderType, Side
from qh_trader.core.objects import Bar, InstrumentId, require_int
from qh_trader.strategy.base import StrategyBase, StrategyContext


class DualMovingAverageStrategy(StrategyBase):
    """双均线趋势跟踪策略."""

    def __init__(
        self,
        strategy_id: str,
        context: StrategyContext,
        instrument: InstrumentId,
        *,
        fast_window: int = 5,
        slow_window: int = 20,
        order_size: int = 1,
    ) -> None:
        super().__init__(strategy_id, context)
        require_int(fast_window, "fast_window", 1)
        require_int(slow_window, "slow_window", 2)
        if fast_window >= slow_window:
            raise ValueError("fast_window must be smaller than slow_window")
        require_int(order_size, "order_size", 1)

        self._instrument = instrument
        self._fast_window = fast_window
        self._slow_window = slow_window
        self._order_size = order_size

        self._closes: deque[Decimal] = deque(maxlen=slow_window)
        self._last_fast_ma: Decimal | None = None
        self._last_slow_ma: Decimal | None = None

    @property
    def instrument(self) -> InstrumentId:
        return self._instrument

    @property
    def fast_window(self) -> int:
        return self._fast_window

    @property
    def slow_window(self) -> int:
        return self._slow_window

    def on_bar(self, bar: Bar) -> None:
        if bar.instrument != self._instrument:
            return

        self._closes.append(bar.close)
        if len(self._closes) < self._slow_window:
            return

        # 计算当前双均线
        closes_list = list(self._closes)
        fast_ma = sum(closes_list[-self._fast_window:]) / Decimal(self._fast_window)
        slow_ma = sum(closes_list) / Decimal(self._slow_window)

        if self._last_fast_ma is not None and self._last_slow_ma is not None:
            # 检查金叉 / 死叉
            prev_diff = self._last_fast_ma - self._last_slow_ma
            curr_diff = fast_ma - slow_ma

            pos = self.context.get_position(self._instrument)

            # 金叉：前值 <= 0 且 当前 > 0
            if prev_diff <= 0 and curr_diff > 0:
                # 若持空仓，先买入平空
                if pos < 0:
                    self.context.buy(
                        self._instrument,
                        quantity=abs(pos),
                        offset=Offset.CLOSE,
                    )
                # 买入开多
                self.context.buy(
                    self._instrument,
                    quantity=self._order_size,
                    offset=Offset.OPEN,
                )

            # 死叉：前值 >= 0 且 当前 < 0
            elif prev_diff >= 0 and curr_diff < 0:
                # 若持多仓，先卖出平多
                if pos > 0:
                    self.context.sell(
                        self._instrument,
                        quantity=pos,
                        offset=Offset.CLOSE,
                    )
                # 卖出开空
                self.context.sell(
                    self._instrument,
                    quantity=self._order_size,
                    offset=Offset.OPEN,
                )

        self._last_fast_ma = fast_ma
        self._last_slow_ma = slow_ma
