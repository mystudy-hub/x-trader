"""[Domain 层] 主力移仓两腿状态机与执行计划 (S4-03, FR-CON-05, FR-CON-06, FR-CON-07, A10).

核心逻辑：
1. 状态机严格递进：PLANNED -> LEG_1 -> LEG_2 -> COMPLETED (任一阶段失败进入 PAUSED / FAILED)；
2. 腿序支持：CLOSE_FIRST (先平后开，资金保守) 与 OPEN_FIRST (先开后平，敞口连续)；
3. 支持分批移仓与部分成交推进；
4. 移仓成本严格由实际成交两腿与手续费得出，价差本身不计入新增现金收益。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from qh_trader.core.constants import Offset, OrderType, PositionSide, Side
from qh_trader.core.objects import (
    InstrumentId,
    OrderIntent,
    ProductId,
    Trade,
)


class LegOrderPolicy(StrEnum):
    CLOSE_FIRST = "CLOSE_FIRST"      # 先平旧仓，后开新仓 (资金优先)
    OPEN_FIRST = "OPEN_FIRST"        # 先开新仓，后平旧仓 (敞口优先)


class RollState(StrEnum):
    PLANNED = "PLANNED"
    LEG_1_SUBMITTED = "LEG_1_SUBMITTED"
    LEG_1_PARTIAL = "LEG_1_PARTIAL"
    LEG_1_FILLED = "LEG_1_FILLED"
    LEG_2_SUBMITTED = "LEG_2_SUBMITTED"
    LEG_2_PARTIAL = "LEG_2_PARTIAL"
    COMPLETED = "COMPLETED"
    PAUSED = "PAUSED"
    FAILED = "FAILED"


@dataclass
class RollTask:
    """单个主力移仓任务状态聚合."""
    roll_id: str
    account_id: str
    product: ProductId
    from_instrument: InstrumentId
    to_instrument: InstrumentId
    position_side: PositionSide      # 移仓的持仓方向 (LONG / SHORT)
    total_quantity: int
    batch_size: int
    policy: LegOrderPolicy = LegOrderPolicy.CLOSE_FIRST
    state: RollState = RollState.PLANNED

    leg1_filled_qty: int = 0
    leg2_filled_qty: int = 0

    leg1_avg_price: Decimal = Decimal(0)
    leg2_avg_price: Decimal = Decimal(0)
    total_commission: Decimal = Decimal(0)

    current_leg1_order_id: str | None = None
    current_leg2_order_id: str | None = None
    failure_reason: str | None = None

    @property
    def is_done(self) -> bool:
        return self.state in (RollState.COMPLETED, RollState.FAILED)

    @property
    def remaining_exposure_qty(self) -> int:
        """两腿未配平暴露的手数."""
        return abs(self.leg1_filled_qty - self.leg2_filled_qty)


class RollManager:
    """主力移仓管理聚合根."""

    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self._tasks: dict[str, RollTask] = {}
        self._order_to_task: dict[str, str] = {}  # client_order_id -> roll_id
        self._counter = 0

    def create_roll_task(
        self,
        product: ProductId,
        from_instrument: InstrumentId,
        to_instrument: InstrumentId,
        position_side: PositionSide,
        quantity: int,
        *,
        batch_size: int = 1,
        policy: LegOrderPolicy = LegOrderPolicy.CLOSE_FIRST,
    ) -> RollTask:
        """创建移仓任务."""
        if quantity <= 0 or batch_size <= 0:
            raise ValueError("quantity and batch_size must be positive")
        if from_instrument == to_instrument:
            raise ValueError("cannot rollover to the exact same instrument")

        self._counter += 1
        roll_id = f"roll-{self._counter}"
        task = RollTask(
            roll_id=roll_id,
            account_id=self.account_id,
            product=product,
            from_instrument=from_instrument,
            to_instrument=to_instrument,
            position_side=position_side,
            total_quantity=quantity,
            batch_size=min(batch_size, quantity),
            policy=policy,
            state=RollState.PLANNED,
        )
        self._tasks[roll_id] = task
        return task

    def get_task(self, roll_id: str) -> RollTask | None:
        return self._tasks.get(roll_id)

    def plan_next_order(
        self,
        task: RollTask,
        now: datetime,
    ) -> OrderIntent | None:
        """根据当前状态与腿序规划下一笔委托意图."""
        if task.is_done or task.state == RollState.PAUSED:
            return None

        # 1. 第一腿：若尚未开始或第一腿未达到目标
        if task.leg1_filled_qty < task.total_quantity and task.current_leg1_order_id is None:
            batch = min(task.batch_size, task.total_quantity - task.leg1_filled_qty)
            order_id = f"{task.roll_id}-leg1-{task.leg1_filled_qty + 1}"
            task.current_leg1_order_id = order_id
            self._order_to_task[order_id] = task.roll_id

            if task.policy == LegOrderPolicy.CLOSE_FIRST:
                # 第一腿平旧仓：如果是多头移仓，平多用 SELL CLOSE；如果是空头移仓，平空用 BUY CLOSE
                side = Side.SELL if task.position_side == PositionSide.LONG else Side.BUY
                intent = OrderIntent(
                    client_order_id=order_id,
                    account_id=self.account_id,
                    strategy_id="roll-manager",
                    instrument=task.from_instrument,
                    side=side,
                    offset=Offset.CLOSE,
                    quantity=batch,
                    order_type=OrderType.MARKET,
                    created_at=now,
                )
            else:
                # 第一腿开新仓：如果是多头移仓，开多用 BUY OPEN；如果是空头移仓，开空用 SELL OPEN
                side = Side.BUY if task.position_side == PositionSide.LONG else Side.SELL
                intent = OrderIntent(
                    client_order_id=order_id,
                    account_id=self.account_id,
                    strategy_id="roll-manager",
                    instrument=task.to_instrument,
                    side=side,
                    offset=Offset.OPEN,
                    quantity=batch,
                    order_type=OrderType.MARKET,
                    created_at=now,
                )

            task.state = RollState.LEG_1_SUBMITTED
            return intent

        # 2. 第二腿：当第一腿已有成交，第二腿需要跟进配平
        if task.leg2_filled_qty < task.leg1_filled_qty and task.current_leg2_order_id is None:
            needed = task.leg1_filled_qty - task.leg2_filled_qty
            order_id = f"{task.roll_id}-leg2-{task.leg2_filled_qty + 1}"
            task.current_leg2_order_id = order_id
            self._order_to_task[order_id] = task.roll_id

            if task.policy == LegOrderPolicy.CLOSE_FIRST:
                # 第二腿开新仓
                side = Side.BUY if task.position_side == PositionSide.LONG else Side.SELL
                intent = OrderIntent(
                    client_order_id=order_id,
                    account_id=self.account_id,
                    strategy_id="roll-manager",
                    instrument=task.to_instrument,
                    side=side,
                    offset=Offset.OPEN,
                    quantity=needed,
                    order_type=OrderType.MARKET,
                    created_at=now,
                )
            else:
                # 第二腿平旧仓
                side = Side.SELL if task.position_side == PositionSide.LONG else Side.BUY
                intent = OrderIntent(
                    client_order_id=order_id,
                    account_id=self.account_id,
                    strategy_id="roll-manager",
                    instrument=task.from_instrument,
                    side=side,
                    offset=Offset.CLOSE,
                    quantity=needed,
                    order_type=OrderType.MARKET,
                    created_at=now,
                )

            task.state = RollState.LEG_2_SUBMITTED
            return intent

        return None

    def on_trade(self, trade: Trade, client_order_id: str | None) -> None:
        """根据真实成交推进两腿状态机."""
        if client_order_id is None:
            return
        roll_id = self._order_to_task.get(client_order_id)
        if not roll_id:
            return
        task = self._tasks.get(roll_id)
        if not task:
            return

        # 判断成交归属于第一腿还是第二腿
        if client_order_id == task.current_leg1_order_id:
            old_qty = task.leg1_filled_qty
            new_qty = old_qty + trade.quantity
            task.leg1_avg_price = (task.leg1_avg_price * Decimal(old_qty) + trade.price * Decimal(trade.quantity)) / Decimal(new_qty)
            task.leg1_filled_qty = new_qty
            task.current_leg1_order_id = None  # 允许规划下一笔

            if task.leg1_filled_qty >= task.total_quantity:
                task.state = RollState.LEG_1_FILLED
            else:
                task.state = RollState.LEG_1_PARTIAL

        elif client_order_id == task.current_leg2_order_id:
            old_qty = task.leg2_filled_qty
            new_qty = old_qty + trade.quantity
            task.leg2_avg_price = (task.leg2_avg_price * Decimal(old_qty) + trade.price * Decimal(trade.quantity)) / Decimal(new_qty)
            task.leg2_filled_qty = new_qty
            task.current_leg2_order_id = None

            if task.leg2_filled_qty >= task.total_quantity:
                task.state = RollState.COMPLETED
            else:
                task.state = RollState.LEG_2_PARTIAL

    def on_order_rejected_or_cancelled(self, client_order_id: str, reason: str) -> None:
        """单腿撤销或被柜台拒绝：进入 PAUSED 状态，保留已成交事实."""
        roll_id = self._order_to_task.get(client_order_id)
        if not roll_id:
            return
        task = self._tasks.get(roll_id)
        if not task:
            return

        task.state = RollState.PAUSED
        task.failure_reason = reason
        if client_order_id == task.current_leg1_order_id:
            task.current_leg1_order_id = None
        elif client_order_id == task.current_leg2_order_id:
            task.current_leg2_order_id = None
