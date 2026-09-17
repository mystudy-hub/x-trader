"""[Domain 层] 交易所硬约束 (S2-05, FR-RISK-03, A19).

核心约束:
1. 日内开仓限额 (中金所/商品特定合约)
2. 一般持仓限额与阶段递减
3. 最大撤单次数限制 (防止超频撤单被监管处罚)
4. 回测与实盘共用同一套硬约束逻辑
"""

from __future__ import annotations

from dataclasses import dataclass, field

from qh_trader.core.constants import Offset
from qh_trader.core.objects import InstrumentId, OrderIntent


class LimitViolationError(ValueError):
    """违反交易所硬约束异常."""


@dataclass
class ExchangeLimits:
    """交易所硬约束规则集."""

    # 合约或品种 -> 日内最大开仓手数
    max_open_lots_per_day: dict[str, int] = field(default_factory=dict)
    # 合约或品种 -> 最大单边持仓手数
    max_position_lots: dict[str, int] = field(default_factory=dict)
    # 合约或品种 -> 单日最大撤单次数 (如 490 次)
    max_cancels_per_day: dict[str, int] = field(default_factory=dict)

    # 默认兜底限制
    default_max_position: int = 500
    default_max_cancels: int = 490

    def _key(self, instrument: InstrumentId) -> str:
        return str(instrument)

    def _product_key(self, instrument: InstrumentId) -> str:
        # 提取英文品种代码 (如 SHFE.rb2410 -> rb)
        sym = instrument.symbol
        return "".join(c for c in sym if c.isalpha()).lower()

    def get_max_open_lots(self, instrument: InstrumentId) -> int | None:
        key = self._key(instrument)
        pkey = self._product_key(instrument)
        if key in self.max_open_lots_per_day:
            return self.max_open_lots_per_day[key]
        if pkey in self.max_open_lots_per_day:
            return self.max_open_lots_per_day[pkey]
        return None

    def get_max_position_lots(self, instrument: InstrumentId) -> int:
        key = self._key(instrument)
        pkey = self._product_key(instrument)
        if key in self.max_position_lots:
            return self.max_position_lots[key]
        if pkey in self.max_position_lots:
            return self.max_position_lots[pkey]
        return self.default_max_position

    def get_max_cancels(self, instrument: InstrumentId) -> int:
        key = self._key(instrument)
        pkey = self._product_key(instrument)
        if key in self.max_cancels_per_day:
            return self.max_cancels_per_day[key]
        if pkey in self.max_cancels_per_day:
            return self.max_cancels_per_day[pkey]
        return self.default_max_cancels

    def check_order(
        self,
        order: OrderIntent,
        current_open_lots_today: int,
        current_holding_lots: int,
    ) -> None:
        """检查委托是否违反硬约束. 若违反则抛出 LimitViolationError."""
        inst = order.instrument

        # 仅对开仓检查开仓限额与持仓限额
        if order.offset == Offset.OPEN:
            # 1. 检查日内开仓限额
            limit_open = self.get_max_open_lots(inst)
            if limit_open is not None:
                if current_open_lots_today + order.quantity > limit_open:
                    raise LimitViolationError(
                        f"daily open limit exceeded for {inst}: current={current_open_lots_today}, "
                        f"order={order.quantity}, limit={limit_open}"
                    )

            # 2. 检查持仓限额
            limit_pos = self.get_max_position_lots(inst)
            if current_holding_lots + order.quantity > limit_pos:
                raise LimitViolationError(
                    f"position limit exceeded for {inst}: current={current_holding_lots}, "
                    f"order={order.quantity}, limit={limit_pos}"
                )

    def check_cancel(self, instrument: InstrumentId, current_cancels_today: int) -> None:
        """检查撤单是否超限."""
        limit_cancels = self.get_max_cancels(instrument)
        if current_cancels_today >= limit_cancels:
            raise LimitViolationError(
                f"cancel limit reached for {instrument}: current={current_cancels_today}, limit={limit_cancels}"
            )
