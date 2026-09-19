"""[Strategy 层] StrategyBase 抽象基类与 StrategyContext 协议 (S3-04, FR-ORD-08).

严格遵守架构隔离原则：
- 策略层仅依赖 Core 协议，禁止直接导入具体的 Engine、Gateway、Domain 数据库或外部数据源；
- 策略以事件驱动方式响应 on_bar, on_order, on_trade；
- 意图通过 StrategyContextPort 提交（buy, sell, cancel 等）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from qh_trader.core.constants import Offset
from qh_trader.core.event import TimerEvent
from qh_trader.core.objects import Bar, InstrumentId, OrderUpdate, Trade, require_text
from qh_trader.core.ports import StrategyContextPort, StrategyPort

# 保持对外类型别名兼容
StrategyContext = StrategyContextPort


class StrategyBase(StrategyPort, ABC):
    """CTA 策略抽象基类."""

    def __init__(self, strategy_id: str, context: StrategyContextPort) -> None:
        require_text(strategy_id, "strategy_id")
        self._strategy_id = strategy_id
        self._context = context
        self._is_active = False

    @property
    def strategy_id(self) -> str:
        return self._strategy_id

    @property
    def context(self) -> StrategyContextPort:
        return self._context

    @property
    def is_active(self) -> bool:
        return self._is_active

    def on_init(self) -> None:
        """初始化回调（参数配置、指标预热等）."""
        pass

    def on_start(self) -> None:
        """策略启动回调."""
        self._is_active = True

    def on_stop(self) -> None:
        """策略停止回调."""
        self._is_active = False

    @abstractmethod
    def on_bar(self, bar: Bar) -> None:
        """每根 Bar 到达时的核心逻辑回调."""
        raise NotImplementedError

    def on_order(self, order: OrderUpdate) -> None:
        """订单回报回调."""
        pass

    def on_trade(self, trade: Trade) -> None:
        """成交回报回调."""
        pass

    def on_timer(self, timer: TimerEvent) -> None:
        """定时器回调 (空行情时段同样触发)."""
        pass

    # ------------------------------------------------------------------ 带归因的下单便捷方法
    def buy(
        self,
        instrument: InstrumentId,
        quantity: int,
        offset: Offset = Offset.OPEN,
        limit_price_ticks: int | None = None,
    ) -> str:
        return self._context.buy(instrument, quantity, offset, limit_price_ticks, strategy_id=self._strategy_id)

    def sell(
        self,
        instrument: InstrumentId,
        quantity: int,
        offset: Offset = Offset.CLOSE,
        limit_price_ticks: int | None = None,
    ) -> str:
        return self._context.sell(instrument, quantity, offset, limit_price_ticks, strategy_id=self._strategy_id)
