"""第 2 层：共享交易领域内核（纯内存业务逻辑与规则）."""

from .ledger import (
    AccountFundsState,
    AccountLedger,
    ClosedTradeRecord,
    InstrumentLedger,
)
from .lifecycle import (
    DailyLifecycleManager,
    LifecyclePhase,
)
from .limits import (
    ExchangeLimits,
    LimitViolationError,
)
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
from .recovery import (
    DiffSeverity,
    ReconciliationDiff,
    RecoveryCoordinator,
)
from .risk import (
    EpochViolationError,
    RiskManager,
    RiskState,
    RiskViolationError,
)
from .rules import (
    RuleEngine,
    calculate_commission,
    calculate_margin,
)
from .smart_router import (
    ClosePriority,
    SmartRouter,
)

__all__ = [
    "AccountFundsState",
    "AccountLedger",
    "ClosePriority",
    "ClosedTradeRecord",
    "DailyLifecycleManager",
    "DiffSeverity",
    "EpochViolationError",
    "ExchangeLimits",
    "InstrumentLedger",
    "LifecyclePhase",
    "LimitViolationError",
    "Order",
    "OrderManager",
    "PositionDetail",
    "PositionManager",
    "PositionReservation",
    "ReconciliationDiff",
    "RecoveryCoordinator",
    "RiskManager",
    "RiskState",
    "RiskViolationError",
    "RuleEngine",
    "SmartRouter",
    "TradeDeduplicator",
    "UnlinkedTrade",
    "calculate_commission",
    "calculate_margin",
]
