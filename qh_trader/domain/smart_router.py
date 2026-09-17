"""[Domain 层] 智能平仓路由与订单计划 (S2-04, FR-ORD-01, FR-ORD-08, A01).

核心规则:
1. 无副作用规划: 先生成 OrderPlan，不直接修改持仓或状态；
2. 交易所开平特性映射:
   - SHFE / INE: 必须严格区分 CLOSE_YESTERDAY 与 CLOSE_TODAY
   - DCE / CZCE / CFFEX / GFEX: 根据品种配置与能力支持进行拆解
3. 平仓优先序配置 (ClosePriority):
   - YESTERDAY_FIRST (平昨优先, 默认)
   - TODAY_FIRST (平今优先, 如平今免手续费品种)
4. 父子单映射: 生成的子订单 parent_order_id 指向原订单 client_order_id.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum

from qh_trader.core.constants import Exchange, Offset
from qh_trader.core.objects import OrderIntent
from qh_trader.domain.positions import PositionDetail


class ClosePriority(StrEnum):
    YESTERDAY_FIRST = "YESTERDAY_FIRST"
    TODAY_FIRST = "TODAY_FIRST"


# 强制区分平今平昨的交易所
EXCHANGES_REQUIRING_EXPLICIT_SPLIT = {Exchange.SHFE, Exchange.INE}


class SmartRouter:
    """开平意图解析与子订单拆单规划器."""

    def __init__(
        self,
        default_priority: ClosePriority = ClosePriority.YESTERDAY_FIRST,
        product_priorities: dict[str, ClosePriority] | None = None,
    ) -> None:
        self.default_priority = default_priority
        self.product_priorities = dict(product_priorities or {})

    def get_priority(self, product_code: str) -> ClosePriority:
        return self.product_priorities.get(product_code.lower(), self.default_priority)

    def plan_order(
        self,
        intent: OrderIntent,
        target_position: PositionDetail | None,
        id_generator: Callable[[], str],
    ) -> list[OrderIntent]:
        """根据持仓情况与交易所能力，将委托意图分解为可直接发给柜台的子单列表.

        注意: 本方法纯函数式执行，不产生任何副作用 (不修改持仓与预占).
        """
        # 开仓无需拆解平今平昨
        if intent.offset == Offset.OPEN:
            return [intent]

        # 已经明确声明了平今或平昨
        if intent.offset in {Offset.CLOSE_TODAY, Offset.CLOSE_YESTERDAY}:
            if target_position is not None:
                if intent.offset == Offset.CLOSE_TODAY and target_position.available_td < intent.quantity:
                    raise ValueError(
                        f"insufficient available today position: available={target_position.available_td}, "
                        f"requested={intent.quantity}"
                    )
                if intent.offset == Offset.CLOSE_YESTERDAY and target_position.available_yd < intent.quantity:
                    raise ValueError(
                        f"insufficient available yesterday position: available={target_position.available_yd}, "
                        f"requested={intent.quantity}"
                    )
            return [intent]

        # 需要智能平仓路由 (平仓请求需要分配今仓与昨仓)
        if target_position is None:
            raise ValueError(f"cannot plan close order without target position for {intent.instrument}")

        total_avail = target_position.total_available
        if total_avail < intent.quantity:
            raise ValueError(
                f"insufficient total available position: total_available={total_avail}, requested={intent.quantity}"
            )

        product = intent.instrument.symbol
        # 提取英文品种前缀
        product_code = "".join(c for c in product if c.isalpha())
        priority = self.get_priority(product_code)

        avail_yd = target_position.available_yd
        avail_td = target_position.available_td
        req_qty = intent.quantity

        qty_yd = 0
        qty_td = 0

        if priority == ClosePriority.YESTERDAY_FIRST:
            qty_yd = min(avail_yd, req_qty)
            qty_td = req_qty - qty_yd
        else:
            qty_td = min(avail_td, req_qty)
            qty_yd = req_qty - qty_td

        plans: list[OrderIntent] = []
        parent_id = intent.client_order_id

        # 构建子单的通用参数模板
        def make_child(sub_offset: Offset, sub_qty: int) -> OrderIntent:
            return OrderIntent(
                client_order_id=id_generator(),
                account_id=intent.account_id,
                strategy_id=intent.strategy_id,
                instrument=intent.instrument,
                side=intent.side,
                offset=sub_offset,
                quantity=sub_qty,
                order_type=intent.order_type,
                limit_price_ticks=intent.limit_price_ticks,
                parent_order_id=parent_id,
                mapping_version=intent.mapping_version,
                created_at=intent.created_at,
            )

        # 按优先级顺序生成子单列表
        if priority == ClosePriority.YESTERDAY_FIRST:
            if qty_yd > 0:
                plans.append(make_child(Offset.CLOSE_YESTERDAY, qty_yd))
            if qty_td > 0:
                plans.append(make_child(Offset.CLOSE_TODAY, qty_td))
        else:
            if qty_td > 0:
                plans.append(make_child(Offset.CLOSE_TODAY, qty_td))
            if qty_yd > 0:
                plans.append(make_child(Offset.CLOSE_YESTERDAY, qty_yd))

        return plans
