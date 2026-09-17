"""第 2 层：共享交易领域内核（纯内存业务逻辑与规则）."""

from .orders import (
    Order,
    OrderManager,
    TradeDeduplicator,
    UnlinkedTrade,
)
from .positions import (
    PositionDetail,
    PositionManager,
    PositionReservation,
)
from .rules import (
    RuleEngine,
    calculate_commission,
    calculate_margin,
)

__all__ = [
    "Order",
    "OrderManager",
    "PositionDetail",
    "PositionManager",
    "PositionReservation",
    "RuleEngine",
    "TradeDeduplicator",
    "UnlinkedTrade",
    "calculate_commission",
    "calculate_margin",
]
