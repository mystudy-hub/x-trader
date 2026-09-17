"""[Domain 层] 恢复协调器、查询合并与状态对账 (S2-08, FR-REC-04, FR-ORD-07, A04).

核心逻辑:
1. 重启或断线重连后，将柜台查询返回的远端订单、成交、持仓与本地状态合并；
2. 识别外部委托 (本地未知的订单)，标记差异，一致前禁止自动进入 READY 交易状态;
3. 游标重放和查询合并不重复记账;
4. 严格阻断非法的自动重发.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from qh_trader.core.constants import PositionSide
from qh_trader.core.objects import InstrumentId, OrderUpdate, Position
from qh_trader.domain.orders import OrderManager
from qh_trader.domain.positions import PositionDetail


class DiffSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    BLOCKING = "BLOCKING"


@dataclass
class ReconciliationDiff:
    """对账差异项."""

    category: str  # "order", "position", "funds"
    identifier: str
    severity: DiffSeverity
    message: str
    local_value: Any = None
    remote_value: Any = None


class RecoveryCoordinator:
    """恢复对账与协调器."""

    def __init__(self, order_manager: OrderManager) -> None:
        self.order_manager = order_manager

    def reconcile_orders(
        self,
        remote_updates: list[OrderUpdate],
    ) -> list[ReconciliationDiff]:
        """合并远端订单查询结果并识别差异 (A04)."""
        diffs: list[ReconciliationDiff] = []

        for remote in remote_updates:
            ident = remote.identity
            local_order = self.order_manager.find_matching_order(
                client_order_id=ident.client_order_id,
                exchange=ident.exchange,
                exchange_order_id=ident.exchange_order_id,
                front_id=ident.front_id,
                session_id=ident.session_id,
                order_ref=ident.order_ref,
            )

            if local_order is None:
                # 出现本地未知的外部委托 (外部终端或人工委托, FR-ORD-05, A04)
                diffs.append(
                    ReconciliationDiff(
                        category="order",
                        identifier=f"{ident.exchange}.{ident.exchange_order_id or ident.order_ref}",
                        severity=DiffSeverity.BLOCKING,
                        message="external order detected from broker query; not recognized by local system",
                        local_value=None,
                        remote_value=remote.status,
                    )
                )
            else:
                # 本地已知订单，推进状态并更新成交量 (防倒退与去重已在 OrderManager 内置)
                try:
                    self.order_manager.process_order_update(remote)
                except Exception as exc:
                    diffs.append(
                        ReconciliationDiff(
                            category="order",
                            identifier=local_order.client_order_id,
                            severity=DiffSeverity.BLOCKING,
                            message=f"failed to reconcile order update: {exc}",
                        )
                    )

        return diffs

    def reconcile_positions(
        self,
        local_positions: dict[tuple[InstrumentId, PositionSide], PositionDetail],
        remote_positions: list[Position],
    ) -> list[ReconciliationDiff]:
        """比对本地与远端持仓数量 (A04)."""
        diffs: list[ReconciliationDiff] = []
        remote_map = {(p.instrument, p.side): p for p in remote_positions}

        for (inst, side), local_pos in local_positions.items():
            remote_pos = remote_map.get((inst, side))
            if remote_pos is None:
                if local_pos.total_position > 0:
                    diffs.append(
                        ReconciliationDiff(
                            category="position",
                            identifier=f"{inst}.{side}",
                            severity=DiffSeverity.BLOCKING,
                            message="local has position but remote reports none",
                            local_value=local_pos.total_position,
                            remote_value=0,
                        )
                    )
            else:
                # 比对今仓与昨仓
                if local_pos.pos_td != remote_pos.pos_td:
                    diffs.append(
                        ReconciliationDiff(
                            category="position",
                            identifier=f"{inst}.{side}.pos_td",
                            severity=DiffSeverity.BLOCKING,
                            message="today position mismatch",
                            local_value=local_pos.pos_td,
                            remote_value=remote_pos.pos_td,
                        )
                    )
                if local_pos.pos_yd != remote_pos.pos_yd:
                    diffs.append(
                        ReconciliationDiff(
                            category="position",
                            identifier=f"{inst}.{side}.pos_yd",
                            severity=DiffSeverity.BLOCKING,
                            message="yesterday position mismatch",
                            local_value=local_pos.pos_yd,
                            remote_value=remote_pos.pos_yd,
                        )
                    )

        return diffs

    def can_enter_ready(self, diffs: list[ReconciliationDiff]) -> bool:
        """检查是否存在任何阻断性差异. 存在 BLOCKING 差异时禁止进入 READY (A04)."""
        return not any(d.severity == DiffSeverity.BLOCKING for d in diffs)
