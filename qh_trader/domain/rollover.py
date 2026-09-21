"""[Domain 层] 主力移仓两腿状态机与执行计划 (S4-03, FR-CON-05, FR-CON-06, FR-CON-07, A10).

核心逻辑：
1. 状态机严格递进：PLANNED -> LEG_1 -> LEG_2 -> COMPLETED (任一阶段失败进入 PAUSED / FAILED)；
2. 腿序支持：CLOSE_FIRST (先平后开，资金保守) 与 OPEN_FIRST (先开后平，敞口连续)；
3. 支持分批移仓与部分成交推进；
4. 移仓成本严格由实际成交两腿与手续费得出，价差本身不计入新增现金收益。
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    CLOSE_FIRST = "CLOSE_FIRST"  # 先平旧仓，后开新仓 (资金优先)
    OPEN_FIRST = "OPEN_FIRST"  # 先开新仓，后平旧仓 (敞口优先)


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
    position_side: PositionSide  # 移仓的持仓方向 (LONG / SHORT)
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
    retry_count: int = 0
    # 当前在途一批的计划量与已成交量：只有一批全部成交才允许规划下一批 (部分成交不重复下单)
    current_leg1_batch_qty: int = 0
    current_leg1_batch_filled: int = 0
    current_leg2_batch_qty: int = 0
    current_leg2_batch_filled: int = 0
    # 引擎实际生成的两腿委托号 (含重试)，供归因从账本成交事实取手续费 (FR-CON-07)
    leg1_order_ids: list[str] = field(default_factory=list)
    leg2_order_ids: list[str] = field(default_factory=list)

    @property
    def is_done(self) -> bool:
        return self.state in (RollState.COMPLETED, RollState.FAILED)

    @property
    def remaining_exposure_qty(self) -> int:
        """两腿未配平暴露的手数."""
        return abs(self.leg1_filled_qty - self.leg2_filled_qty)

    @property
    def is_incomplete(self) -> bool:
        """已开始但未完成：需要在报告中列出实际剩余持仓与暴露 (FR-CON-06)."""
        return self.state not in (RollState.COMPLETED, RollState.PLANNED)


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

    def task_for_order(self, client_order_id: str) -> RollTask | None:
        """由引擎生成的 client_order_id 反查移仓任务，供策略按实际成交推进两腿."""
        roll_id = self._order_to_task.get(client_order_id)
        return self._tasks.get(roll_id) if roll_id is not None else None

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
            task.current_leg1_batch_qty = batch
            task.current_leg1_batch_filled = 0
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
            task.current_leg2_batch_qty = needed
            task.current_leg2_batch_filled = 0
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

    def bind_submitted_order(self, task: RollTask, leg: int, client_order_id: str) -> None:
        """把引擎实际生成的 client_order_id 绑定到某一腿，供成交推进状态机 (FR-CON-06)."""
        if leg not in (1, 2):
            raise ValueError("leg must be 1 or 2")
        if not isinstance(client_order_id, str) or not client_order_id:
            raise ValueError("client_order_id is required")
        planned = task.current_leg1_order_id if leg == 1 else task.current_leg2_order_id
        if planned is not None:
            self._order_to_task.pop(planned, None)
        self._order_to_task[client_order_id] = task.roll_id
        if leg == 1:
            task.current_leg1_order_id = client_order_id
            task.leg1_order_ids.append(client_order_id)
        else:
            task.current_leg2_order_id = client_order_id
            task.leg2_order_ids.append(client_order_id)

    def on_leg_failed(self, task: RollTask, leg: int, reason: str) -> None:
        """某一腿被拒绝 / 撤销 / 过期：进入 PAUSED，保留已成交事实，等待调用方决定重试或放弃 (FR-CON-06)."""
        if leg not in (1, 2):
            raise ValueError("leg must be 1 or 2")
        if task.is_done:
            return
        task.state = RollState.PAUSED
        task.failure_reason = reason
        if leg == 1:
            task.current_leg1_order_id = None
            task.current_leg1_batch_qty = 0
            task.current_leg1_batch_filled = 0
        else:
            task.current_leg2_order_id = None
            task.current_leg2_batch_qty = 0
            task.current_leg2_batch_filled = 0

    def resume(self, task: RollTask, *, max_retries: int = 3) -> bool:
        """从 PAUSED 恢复：按已成交事实回到相应阶段以便重新规划下一腿；超过重试上限进入 FAILED.

        返回 True 表示可继续规划；False 表示任务已 FAILED (或本来就已完成)。
        """
        if task.state != RollState.PAUSED:
            return not task.is_done
        if task.retry_count >= max_retries:
            task.state = RollState.FAILED
            task.failure_reason = f"retry limit {max_retries} exhausted: {task.failure_reason}"
            return False
        task.retry_count += 1
        if task.leg1_filled_qty >= task.total_quantity:
            task.state = RollState.LEG_2_PARTIAL if task.leg2_filled_qty > 0 else RollState.LEG_1_FILLED
        elif task.leg1_filled_qty > 0:
            task.state = RollState.LEG_1_PARTIAL
        else:
            task.state = RollState.PLANNED
        return True

    def on_fill(self, task: RollTask, trade: Trade, *, leg: int) -> None:
        """按腿记录真实成交.

        引擎可能把 CLOSE 改写为 CLOSE_YESTERDAY 或拆成子单，父单号不再出现在回报里；
        因此按合约归属确定腿序后直接记账，不依赖 client_order_id 映射 (FR-CON-06)。
        当前一批全部成交后才清空该腿的在途单号；部分成交时不重复规划同一腿。
        """
        if task.is_done:
            return
        if leg == 1:
            old_qty = task.leg1_filled_qty
            new_qty = old_qty + trade.quantity
            task.leg1_avg_price = (
                task.leg1_avg_price * Decimal(old_qty) + trade.price * Decimal(trade.quantity)
            ) / Decimal(new_qty)
            task.leg1_filled_qty = new_qty
            task.current_leg1_batch_filled += trade.quantity
            if task.current_leg1_batch_filled >= task.current_leg1_batch_qty:
                task.current_leg1_order_id = None  # 本批完成，允许规划下一批
            task.state = RollState.LEG_1_FILLED if new_qty >= task.total_quantity else RollState.LEG_1_PARTIAL
            return
        if leg != 2:
            raise ValueError("leg must be 1 or 2")
        old_qty = task.leg2_filled_qty
        new_qty = old_qty + trade.quantity
        task.leg2_avg_price = (
            task.leg2_avg_price * Decimal(old_qty) + trade.price * Decimal(trade.quantity)
        ) / Decimal(new_qty)
        task.leg2_filled_qty = new_qty
        task.current_leg2_batch_filled += trade.quantity
        if task.current_leg2_batch_filled >= task.current_leg2_batch_qty:
            task.current_leg2_order_id = None
        task.state = RollState.COMPLETED if new_qty >= task.total_quantity else RollState.LEG_2_PARTIAL

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
            self.on_fill(task, trade, leg=1)
        elif client_order_id == task.current_leg2_order_id:
            self.on_fill(task, trade, leg=2)

    def on_order_rejected_or_cancelled(self, client_order_id: str, reason: str) -> None:
        """单腿撤销或被柜台拒绝：进入 PAUSED 状态，保留已成交事实."""
        roll_id = self._order_to_task.get(client_order_id)
        if not roll_id:
            return
        task = self._tasks.get(roll_id)
        if not task:
            return
        leg = 1 if client_order_id == task.current_leg1_order_id else 2
        self.on_leg_failed(task, leg, reason)
