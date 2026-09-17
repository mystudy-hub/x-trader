"""[Domain 层] 原子风控、自成交防范、熔断状态机与控制代次校验 (S2-06, S2-10, FR-RISK-01~07, A08, A09, A23).

主要职责:
1. 控制代次 (Control Epoch) 校验: 旧代次交易命令被拒，但旧代次真实成交必须客观入账 (A23)
2. 熔断状态机 (RiskState): NORMAL -> REDUCE_ONLY -> CANCELING -> FLATTENING -> HALTED
3. 合法平仓/减仓不被开仓风控误拦 (FR-RISK-05)
4. 自成交防范 (Self-Trade Prevention, FR-RISK-04): 拦截相反方向且价格交叉的活动挂单
5. 结合账户资金状态进行开仓资金可用性核验 (FR-LED-05)
6. 统计持久化计数器 (开仓手数、撤单次数)
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from qh_trader.core.constants import Offset, OrderType, Side
from qh_trader.core.objects import InstrumentId, OrderIntent
from qh_trader.domain.ledger import AccountFundsState
from qh_trader.domain.limits import ExchangeLimits
from qh_trader.domain.orders import Order
from qh_trader.domain.positions import PositionDetail


class RiskViolationError(ValueError):
    """风控检查拒绝异常."""


class EpochViolationError(ValueError):
    """控制代次失效异常."""


class RiskState(StrEnum):
    """熔断与风控运行状态 (FR-RISK-05)."""

    NORMAL = "NORMAL"
    REDUCE_ONLY = "REDUCE_ONLY"
    CANCELING = "CANCELING"
    FLATTENING = "FLATTENING"
    HALTED = "HALTED"


class RiskManager:
    """原子事前风控与风险状态机聚合根."""

    def __init__(
        self,
        account_id: str,
        initial_epoch: int = 1,
        limits: ExchangeLimits | None = None,
    ) -> None:
        self.account_id = account_id
        self.current_epoch: int = initial_epoch
        self.risk_state: RiskState = RiskState.NORMAL
        self.limits: ExchangeLimits = limits or ExchangeLimits()

        # 当日统计计数器
        self.today_open_lots: dict[str, int] = {}
        self.today_cancels_count: dict[str, int] = {}

    def advance_epoch(self, new_epoch: int) -> None:
        """推进控制代次 (严格递增)."""
        if new_epoch <= self.current_epoch:
            raise ValueError(f"new epoch ({new_epoch}) must be greater than current ({self.current_epoch})")
        self.current_epoch = new_epoch

    def trigger_circuit_breaker(self, target_state: RiskState = RiskState.HALTED, reason: str = "") -> None:
        """触发熔断状态 (FR-RISK-05)."""
        self.risk_state = target_state

    def reset_risk_state(self) -> None:
        """重置风控状态为正常."""
        self.risk_state = RiskState.NORMAL

    def check_command_epoch(self, command_epoch: int) -> None:
        """校验交易命令代次 (FR-RISK-07, A23).

        旧代次交易命令必须被拒绝!
        """
        if command_epoch < self.current_epoch:
            raise EpochViolationError(
                f"command epoch expired: command_epoch={command_epoch}, current_epoch={self.current_epoch}"
            )

    def check_order(
        self,
        order: OrderIntent,
        command_epoch: int,
        funds: AccountFundsState,
        current_pos: PositionDetail,
        active_orders: list[Order] | None = None,
        estimated_margin_needed: Decimal = Decimal("0.00"),
    ) -> None:
        """事前风控统一校验入口 (FR-RISK-01).

        若未通过校验，抛出 RiskViolationError 或 EpochViolationError.
        """
        # 1. 控制代次校验
        self.check_command_epoch(command_epoch)

        inst_str = str(order.instrument)

        # 2. 熔断状态机校验 (FR-RISK-05)
        if self.risk_state == RiskState.HALTED:
            raise RiskViolationError(f"risk state is HALTED; new orders are blocked: {order.client_order_id}")

        if self.risk_state in {RiskState.REDUCE_ONLY, RiskState.CANCELING, RiskState.FLATTENING}:
            if order.offset == Offset.OPEN:
                raise RiskViolationError(
                    f"risk state is {self.risk_state}; open orders are blocked: {order.client_order_id}"
                )

        # 3. 交易所硬约束校验 (limits)
        current_open = self.today_open_lots.get(inst_str, 0)
        current_holding = current_pos.total_position
        self.limits.check_order(order, current_open, current_holding)

        # 4. 开仓资金充足性校验 (FR-LED-05)
        if order.offset == Offset.OPEN:
            if estimated_margin_needed > funds.available_for_new_trades:
                raise RiskViolationError(
                    f"insufficient available funds for open: needed={estimated_margin_needed}, "
                    f"available={funds.available_for_new_trades}"
                )

        # 5. 自成交防范 (Self-Trade Prevention, FR-RISK-04)
        if active_orders and order.order_type == OrderType.LIMIT and order.limit_price_ticks is not None:
            self._check_self_trade(order, active_orders)

    def _check_self_trade(self, order: OrderIntent, active_orders: list[Order]) -> None:
        """检查是否有同合约、相反方向、且限价价格重叠的在途挂单."""
        target_inst = order.instrument
        target_side = order.side
        target_price = order.limit_price_ticks

        for active in active_orders:
            if not active.is_active or active.instrument != target_inst:
                continue
            if active.order_type != OrderType.LIMIT or active.limit_price_ticks is None:
                continue
            # 相反方向
            if active.side != target_side:
                # 若本次是买单，买价 >= 既有卖价，构成交叉自成交
                if target_side == Side.BUY and target_price >= active.limit_price_ticks:
                    raise RiskViolationError(
                        f"self-trade prevented: new buy price ({target_price}) >= "
                        f"existing sell order ({active.client_order_id}, price={active.limit_price_ticks})"
                    )
                # 若本次是卖单，卖价 <= 既有买价，构成交叉自成交
                if target_side == Side.SELL and target_price <= active.limit_price_ticks:
                    raise RiskViolationError(
                        f"self-trade prevented: new sell price ({target_price}) <= "
                        f"existing buy order ({active.client_order_id}, price={active.limit_price_ticks})"
                    )

    def on_order_submitted(self, order: OrderIntent) -> None:
        """报单发送后累加开仓统计."""
        if order.offset == Offset.OPEN:
            key = str(order.instrument)
            self.today_open_lots[key] = self.today_open_lots.get(key, 0) + order.quantity

    def on_order_canceled(self, instrument: InstrumentId) -> None:
        """撤单成功后累加撤单计数."""
        key = str(instrument)
        self.today_cancels_count[key] = self.today_cancels_count.get(key, 0) + 1

    def reset_daily_counters(self) -> None:
        """跨日清空当日临时统计计数."""
        self.today_open_lots.clear()
        self.today_cancels_count.clear()
