"""[Research 层] 研究模式成本与滑点假设 (S3-08, FR-RULE-05, FR-VAL-04).

提供研究通道与事件驱动通道统一的交易摩擦成本口径：
- 固定手续费 (元/手) 或比例手续费 (万分比)
- 滑点跳数与最小变动价位
- 估算单边 / 双边交易摩擦
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from qh_trader.core.objects import require_decimal, require_int


@dataclass(frozen=True, slots=True)
class ResearchCostModel:
    """研究模式成本模型."""
    multiplier: Decimal = Decimal("10")
    price_tick: Decimal = Decimal("1")
    commission_per_lot: Decimal = Decimal("5.0")
    commission_rate: Decimal = Decimal("0.0")
    slippage_ticks: int = 0

    def __post_init__(self) -> None:
        require_decimal(self.multiplier, "multiplier", Decimal("0.000001"))
        require_decimal(self.price_tick, "price_tick", Decimal("0.000001"))
        require_decimal(self.commission_per_lot, "commission_per_lot", Decimal(0))
        require_decimal(self.commission_rate, "commission_rate", Decimal(0))
        require_int(self.slippage_ticks, "slippage_ticks", 0)

    def calculate_cost_per_lot(self, price: Decimal) -> Decimal:
        """计算单手单边交易的总成本 (手续费 + 滑点)."""
        fee = self.commission_per_lot + (price * self.multiplier * self.commission_rate)
        slippage_cost = Decimal(self.slippage_ticks) * self.price_tick * self.multiplier
        return fee + slippage_cost
