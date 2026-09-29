"""[Strategy 示例] 面向异步执行的双均线目标仓位策略 (S5-05, FR-ORD-08).

行情积压时继续更新指标及最新目标，但任意时刻只存在一笔在途委托。反转先平仓，
实际成交确认持仓归零后才开反向仓；FILLED 回报不能替代实际成交。取消/拒绝/过期
放弃旧目标，等待新的交叉信号，不循环重试已拒绝的意图。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from qh_trader.core.constants import Offset, OrderStatus, Side
from qh_trader.core.objects import Bar, OrderUpdate, Trade
from qh_trader.strategy.examples.trend_following import DualMovingAverageStrategy


class AsyncDualMovingAverageStrategy(DualMovingAverageStrategy):
    """共享均线参数校验，使用独立异步执行语义，不改变回测示例的成交模型."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._target: int | None = None
        self._pending_id: str | None = None
        self._pending_quantity = 0
        self._received_quantity = 0
        self._reported_quantity = 0
        self._pending_terminal = False
        self._trading_day: date | None = None
        self._inventory: dict[tuple[Side, date], int] = {}
        self._intent_offsets: dict[str, Offset] = {}

    def on_bar(self, bar: Bar) -> None:
        if bar.instrument != self.instrument:
            return
        self._trading_day = max(self._trading_day or bar.meta.trading_day, bar.meta.trading_day)
        self._closes.append(bar.close)
        if len(self._closes) < self.slow_window:
            return
        closes = list(self._closes)
        fast = sum(closes[-self.fast_window :]) / Decimal(self.fast_window)
        slow = sum(closes) / Decimal(self.slow_window)
        if self._last_fast_ma is not None and self._last_slow_ma is not None:
            previous, current = self._last_fast_ma - self._last_slow_ma, fast - slow
            if previous <= 0 < current:
                self._target = self._order_size
            elif previous >= 0 > current:
                self._target = -self._order_size
        self._last_fast_ma, self._last_slow_ma = fast, slow
        self._advance_target()

    def _advance_target(self) -> None:
        if self._pending_id is not None or self._target is None:
            return
        position = self.context.get_position(self.instrument)
        target = self._target
        if position == target:
            return
        if position and position * target <= 0:
            # 反转只发平仓腿；开仓腿必须等已归属成交使本策略持仓归零。
            side_buy = position < 0
            quantity, offset = self._close_bucket(position, abs(position))
        elif abs(position) > abs(target):
            side_buy = position < 0
            quantity, offset = self._close_bucket(position, abs(position) - abs(target))
        else:
            side_buy, quantity, offset = target > position, abs(target - position), Offset.OPEN
        submit = self.buy if side_buy else self.sell
        self._pending_id = submit(self.instrument, quantity=quantity, offset=offset)
        self._intent_offsets[self._pending_id] = offset
        self._pending_quantity = quantity
        self._received_quantity = self._reported_quantity = 0
        self._pending_terminal = False

    def _close_bucket(self, position: int, quantity: int) -> tuple[int, Offset]:
        """只规划本策略已归属成交的子单；仓位/冻结最终仍由执行服务内核校验."""
        side = Side.BUY if position > 0 else Side.SELL
        day = self._trading_day
        if day is None:
            raise ValueError("a close intent requires a committed exchange trading day")
        yesterday = sum(
            size
            for (opened_side, opened_day), size in self._inventory.items()
            if opened_side == side and opened_day < day
        )
        today = self._inventory.get((side, day), 0)
        if yesterday + today != abs(position):
            raise ValueError("strategy trade inventory differs from its attributed position")
        if yesterday:
            return min(quantity, yesterday), Offset.CLOSE_YESTERDAY
        if today:
            return min(quantity, today), Offset.CLOSE_TODAY
        raise ValueError("strategy has no attributed close bucket")

    def on_order(self, order: OrderUpdate) -> None:
        if order.identity.client_order_id != self._pending_id:
            return
        self._reported_quantity = max(self._reported_quantity, order.filled_quantity)
        if order.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
            self._target = None
            self._pending_terminal = True
        elif order.status == OrderStatus.FILLED:
            self._pending_terminal = True
            self._reported_quantity = max(self._reported_quantity, self._pending_quantity)
        self._finish_pending()

    def on_trade(self, trade: Trade) -> None:
        identity = trade.order_identity
        if identity is None or identity.client_order_id not in self._intent_offsets:
            return
        self._trading_day = max(self._trading_day or trade.trading_day, trade.trading_day)
        offset = self._intent_offsets[identity.client_order_id]
        if offset == Offset.OPEN:
            key = trade.side, trade.trading_day
            self._inventory[key] = self._inventory.get(key, 0) + trade.quantity
        else:
            opened_side = Side.SELL if trade.side == Side.BUY else Side.BUY
            remaining = trade.quantity
            for key in sorted(self._inventory, key=lambda item: item[1]):
                eligible = key[1] == trade.trading_day if offset == Offset.CLOSE_TODAY else key[1] < trade.trading_day
                if key[0] == opened_side and eligible:
                    closed = min(remaining, self._inventory[key])
                    self._inventory[key] -= closed
                    remaining -= closed
            if remaining:
                raise ValueError("close trade exceeds the strategy's attributed day bucket")
        if identity.client_order_id != self._pending_id:
            return
        self._received_quantity += trade.quantity
        self._finish_pending()

    def _finish_pending(self) -> None:
        if self._pending_id is None:
            return
        if self._received_quantity >= self._pending_quantity or (
            self._pending_terminal and self._received_quantity >= self._reported_quantity
        ):
            self._pending_id = None
            self._advance_target()
