"""[Core 层] 不依赖柜台枚举的稳定业务标识与状态。"""

from enum import IntFlag, StrEnum


class Exchange(StrEnum):
    SHFE = "SHFE"
    INE = "INE"
    DCE = "DCE"
    CZCE = "CZCE"
    CFFEX = "CFFEX"
    GFEX = "GFEX"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class PositionSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class Offset(StrEnum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    CLOSE_TODAY = "CLOSE_TODAY"
    CLOSE_YESTERDAY = "CLOSE_YESTERDAY"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class SendState(StrEnum):
    NOT_SENT = "NOT_SENT"
    SENT_UNKNOWN = "SENT_UNKNOWN"
    CONFIRMED_REMOTE = "CONFIRMED_REMOTE"


class MarketPhase(StrEnum):
    AUCTION_SUBMIT = "AUCTION_SUBMIT"
    AUCTION_MATCH = "AUCTION_MATCH"
    CANCEL_ONLY = "CANCEL_ONLY"
    WAITING = "WAITING"
    CONTINUOUS = "CONTINUOUS"
    BREAK = "BREAK"
    SUSPENDED = "SUSPENDED"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"


class ExecutionPolicy(StrEnum):
    NEXT_SESSION_OPEN = "NEXT_SESSION_OPEN"
    NEXT_DAY_SESSION_OPEN = "NEXT_DAY_SESSION_OPEN"
    NEXT_DAY_FIXED_TIME = "NEXT_DAY_FIXED_TIME"
    NEXT_BAR_OPEN = "NEXT_BAR_OPEN"


class MissedExecutionPolicy(StrEnum):
    """错过目标执行时点后的处置 (FR-EXEC-03)：顺延到下一符合条件的时点，或取消本次意图."""

    DEFER = "DEFER"
    CANCEL = "CANCEL"


class PriceType(StrEnum):
    SESSION_OPEN = "SESSION_OPEN"
    DAY_SESSION_OPEN = "DAY_SESSION_OPEN"
    FIXED_TIME = "FIXED_TIME"
    BAR_OPEN = "BAR_OPEN"


class SeriesKind(StrEnum):
    SPREAD = "SPREAD"
    ADJUSTED = "ADJUSTED"


class QualityFlag(IntFlag):
    OK = 0
    MISSING = 1
    INVALID = 2
    STALE = 4
    SYNTHETIC = 8
    PARTIAL = 16


class EventKind(StrEnum):
    TIMER = "TIMER"
    MARKET_DATA = "MARKET_DATA"
    ORDER_ARRIVAL = "ORDER_ARRIVAL"
    CANCEL_ARRIVAL = "CANCEL_ARRIVAL"
    ORDER_REPORT = "ORDER_REPORT"
    TRADE_REPORT = "TRADE_REPORT"
    SESSION_CHANGE = "SESSION_CHANGE"
    SETTLEMENT = "SETTLEMENT"
    CONTROL = "CONTROL"


class ReplayOrder(StrEnum):
    SIMULATED = "SIMULATED"
    RECORDED = "RECORDED"


class LimitLiquidityScenario(StrEnum):
    DIRECTION_CONSERVATIVE = "DIRECTION_CONSERVATIVE"
    TOUCH_LIMIT_NO_FILL = "TOUCH_LIMIT_NO_FILL"


class IntrabarTouchRule(StrEnum):
    """盘中限价候选成交规则 (FR-MATCH-02)：触价即候选，或必须穿价至少一个价格步长."""

    TOUCH = "TOUCH"
    CROSS_ONE_TICK = "CROSS_ONE_TICK"


class AuctionFillPolicy(StrEnum):
    """含竞价的 Bar 开盘价能否用作成交候选 (FR-MATCH-02, A25-07)."""

    ASSUME_PARTICIPATION = "ASSUME_PARTICIPATION"
    REJECT = "REJECT"


class MissingRuleError(LookupError):
    """所需版本或能力未知，调用方必须显式处置。"""


class AmbiguousRuleError(LookupError):
    """规则或合约查询存在多个适用结果。"""


class JournalConflictError(RuntimeError):
    """事务与已提交的标识、游标或控制权冲突，调用方必须重新核对。"""


class DuplicateFactError(JournalConflictError):
    """带作用域的成交已经入账，本次事务未应用任何变更。"""


class JournalCorruptionError(RuntimeError):
    """已提交日志的校验和或数据契约不一致，不能继续发布状态。"""
