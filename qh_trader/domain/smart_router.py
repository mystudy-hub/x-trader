"""[Domain 层] 智能平仓路由与订单计划 (S2-04, FR-ORD-01, FR-ORD-08, A01).

核心规则:
1. 无副作用规划: 只生成 OrderPlan，不修改持仓或预占；
2. 能力表驱动 (ExchangeCloseCapability, 带版本):
   - requires_explicit_close_today (SHFE / INE): CLOSE 必须拆成 CLOSE_YESTERDAY / CLOSE_TODAY
   - supports_unified_close (DCE / CZCE / CFFEX / GFEX 的常见口径): CLOSE 原样透传单个子单，
     但仍计算今昨归属供费用核算 (持仓归属、费用归属、报单标志是三个概念)
   - 不支持显式平今 / 平昨的交易所收到显式 CLOSE_TODAY / CLOSE_YESTERDAY 时明确拒绝 (UnsupportedOffsetError)
   能力表不内置任何交易所默认值：调用方必须显式传入经核验的能力配置。
3. 平仓优先序 (ClosePriority) 由账户 / 品种配置决定，品种键大小写不敏感。
4. 品种级意图 (ProductIntent, FR-ORD-08) 通过注入的 resolve_contract 解析为实际合约，并记录 mapping_version。
5. 父子单映射: 子订单 parent_order_id 指向原订单 client_order_id。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from qh_trader.core.constants import Exchange, MissingRuleError, Offset, OrderType, Side
from qh_trader.core.objects import InstrumentId, OrderIntent, ProductId, require_text
from qh_trader.domain.positions import PositionDetail


class ClosePriority(StrEnum):
    YESTERDAY_FIRST = "YESTERDAY_FIRST"
    TODAY_FIRST = "TODAY_FIRST"


class UnsupportedOffsetError(ValueError):
    """目标交易所 / 柜台能力表不支持该开平标志 (A01: 不支持的类型明确拒绝)."""


# 强制区分平今平昨的交易所 (用于校验能力表配置与之一致)
EXCHANGES_REQUIRING_EXPLICIT_SPLIT = frozenset({Exchange.SHFE, Exchange.INE})


@dataclass(frozen=True, slots=True)
class ExchangeCloseCapability:
    """单个交易所的平仓能力 (经柜台实测核验)."""

    supports_close_today: bool
    requires_explicit_close_today: bool
    supports_unified_close: bool
    evidence_ref: str | None = None

    def __post_init__(self) -> None:
        if self.requires_explicit_close_today and not self.supports_close_today:
            raise ValueError("an exchange requiring explicit close-today must support CLOSE_TODAY")
        if not self.supports_unified_close and not self.supports_close_today:
            raise ValueError("capability must allow at least unified close or explicit close-today")


@dataclass(frozen=True)
class CloseCapabilityTable:
    """按交易所的平仓能力表 (带版本)."""

    version: str
    capabilities: Mapping[Exchange, ExchangeCloseCapability]

    def __post_init__(self) -> None:
        require_text(self.version, "capability_version")
        for exchange, cap in self.capabilities.items():
            if not isinstance(exchange, Exchange) or not isinstance(cap, ExchangeCloseCapability):
                raise TypeError("capability table maps Exchange -> ExchangeCloseCapability")
            if exchange in EXCHANGES_REQUIRING_EXPLICIT_SPLIT and not cap.requires_explicit_close_today:
                raise ValueError(f"{exchange} requires explicit close-today split; capability table disagrees")
        object.__setattr__(self, "capabilities", dict(self.capabilities))

    def for_exchange(self, exchange: Exchange) -> ExchangeCloseCapability:
        cap = self.capabilities.get(exchange)
        if cap is None:
            raise MissingRuleError(f"no verified close capability for {exchange} in table {self.version}")
        return cap


@dataclass(frozen=True, slots=True, kw_only=True)
class ProductIntent:
    """品种级下单意图 (FR-ORD-08)，由路由按当时可见主力映射解析为实际合约."""

    client_order_id: str
    account_id: str
    strategy_id: str
    product: ProductId
    side: Side
    offset: Offset
    quantity: int
    order_type: OrderType
    created_at: datetime
    limit_price_ticks: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.product, ProductId):
            raise TypeError("product intent requires a ProductId")


@dataclass(frozen=True)
class OrderPlan:
    """路由结果：子订单列表 + 今昨归属 (供费用核算) + 所用能力表版本."""

    parent: OrderIntent
    children: tuple[OrderIntent, ...]
    attribution: tuple[tuple[Offset, int], ...]
    capability_version: str
    mapping_version: str | None = None
    priority: ClosePriority | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def today_lots(self) -> int:
        return sum(q for o, q in self.attribution if o == Offset.CLOSE_TODAY)

    @property
    def yesterday_lots(self) -> int:
        return sum(q for o, q in self.attribution if o == Offset.CLOSE_YESTERDAY)


class SmartRouter:
    """开平意图解析与子订单拆单规划器."""

    def __init__(
        self,
        capabilities: CloseCapabilityTable,
        default_priority: ClosePriority = ClosePriority.YESTERDAY_FIRST,
        product_priorities: Mapping[str, ClosePriority] | None = None,
        resolve_contract: Callable[[ProductId], InstrumentId] | None = None,
        mapping_version: str | None = None,
    ) -> None:
        if not isinstance(capabilities, CloseCapabilityTable):
            raise TypeError("SmartRouter requires an explicit CloseCapabilityTable")
        self.capabilities = capabilities
        self.default_priority = default_priority
        self.product_priorities = {k.strip().lower(): v for k, v in (product_priorities or {}).items()}
        self.resolve_contract = resolve_contract
        self.mapping_version = mapping_version

    # ------------------------------------------------------------------ 配置
    def get_priority(self, product_code: str) -> ClosePriority:
        return self.product_priorities.get(product_code.strip().lower(), self.default_priority)

    @staticmethod
    def product_code(instrument: InstrumentId) -> str:
        return "".join(c for c in instrument.symbol if c.isalpha()).lower()

    # ------------------------------------------------------------------ 品种级意图
    def resolve_intent(self, intent: OrderIntent | ProductIntent) -> OrderIntent:
        """OrderIntent 直接透传；ProductIntent 用注入的映射解析为实际合约并盖上 mapping_version."""
        if isinstance(intent, OrderIntent):
            return intent
        if self.resolve_contract is None:
            raise MissingRuleError("product-level intent needs a resolve_contract mapping; none was injected")
        if self.mapping_version is None:
            raise MissingRuleError("product-level intent resolution requires an explicit mapping_version")
        instrument = self.resolve_contract(intent.product)
        if not isinstance(instrument, InstrumentId) or instrument.exchange != intent.product.exchange:
            raise ValueError(f"resolved contract {instrument} does not belong to product {intent.product}")
        return OrderIntent(
            client_order_id=intent.client_order_id,
            account_id=intent.account_id,
            strategy_id=intent.strategy_id,
            instrument=instrument,
            side=intent.side,
            offset=intent.offset,
            quantity=intent.quantity,
            order_type=intent.order_type,
            limit_price_ticks=intent.limit_price_ticks,
            created_at=intent.created_at,
            mapping_version=self.mapping_version,
        )

    # ------------------------------------------------------------------ 规划
    def plan_order(
        self,
        intent: OrderIntent | ProductIntent,
        target_position: PositionDetail | None,
        id_generator: Callable[[], str],
    ) -> OrderPlan:
        """根据持仓与交易所能力，将委托意图分解为可直接发给柜台的子单计划 (纯函数，无副作用)."""
        order = self.resolve_intent(intent)
        version = self.capabilities.version
        mapping_version = order.mapping_version

        if order.offset == Offset.OPEN:
            return OrderPlan(order, (order,), (), version, mapping_version)

        cap = self.capabilities.for_exchange(order.instrument.exchange)

        # 显式平今 / 平昨
        if order.offset in {Offset.CLOSE_TODAY, Offset.CLOSE_YESTERDAY}:
            if not cap.supports_close_today:
                raise UnsupportedOffsetError(
                    f"{order.instrument.exchange} does not support explicit {order.offset} "
                    f"(capability table {version}); use Offset.CLOSE"
                )
            if target_position is not None:
                avail = (
                    target_position.available_td if order.offset == Offset.CLOSE_TODAY else target_position.available_yd
                )
                if avail < order.quantity:
                    bucket = "today" if order.offset == Offset.CLOSE_TODAY else "yesterday"
                    raise ValueError(
                        f"insufficient available {bucket} position: available={avail}, requested={order.quantity}"
                    )
            return OrderPlan(order, (order,), ((order.offset, order.quantity),), version, mapping_version)

        # 统一平仓 CLOSE：需要今昨归属
        if target_position is None:
            raise ValueError(f"cannot plan close order without target position for {order.instrument}")
        if target_position.total_available < order.quantity:
            raise ValueError(
                f"insufficient total available position: total_available={target_position.total_available}, "
                f"requested={order.quantity}"
            )

        priority = self.get_priority(self.product_code(order.instrument))
        qty_yd, qty_td = self._allocate(order.quantity, target_position, priority)
        ordered: list[tuple[Offset, int]] = (
            [(Offset.CLOSE_YESTERDAY, qty_yd), (Offset.CLOSE_TODAY, qty_td)]
            if priority == ClosePriority.YESTERDAY_FIRST
            else [(Offset.CLOSE_TODAY, qty_td), (Offset.CLOSE_YESTERDAY, qty_yd)]
        )
        attribution = tuple((off, q) for off, q in ordered if q > 0)

        if cap.requires_explicit_close_today:
            children = tuple(self._make_child(order, off, q, id_generator) for off, q in attribution)
            return OrderPlan(order, children, attribution, version, mapping_version, priority)

        if not cap.supports_unified_close:
            raise UnsupportedOffsetError(
                f"{order.instrument.exchange} supports neither unified close nor explicit split (table {version})"
            )
        # 统一平仓透传一个子单；今昨归属仍随计划返回供费用核算
        return OrderPlan(
            order,
            (order,),
            attribution,
            version,
            mapping_version,
            priority,
            notes=("unified close passed through; attribution is for fee accounting only",),
        )

    @staticmethod
    def _allocate(req_qty: int, pos: PositionDetail, priority: ClosePriority) -> tuple[int, int]:
        if priority == ClosePriority.YESTERDAY_FIRST:
            qty_yd = min(pos.available_yd, req_qty)
            return qty_yd, req_qty - qty_yd
        qty_td = min(pos.available_td, req_qty)
        return req_qty - qty_td, qty_td

    @staticmethod
    def _make_child(
        parent: OrderIntent,
        sub_offset: Offset,
        sub_qty: int,
        id_generator: Callable[[], str],
    ) -> OrderIntent:
        return OrderIntent(
            client_order_id=id_generator(),
            account_id=parent.account_id,
            strategy_id=parent.strategy_id,
            instrument=parent.instrument,
            side=parent.side,
            offset=sub_offset,
            quantity=sub_qty,
            order_type=parent.order_type,
            limit_price_ticks=parent.limit_price_ticks,
            parent_order_id=parent.client_order_id,
            mapping_version=parent.mapping_version,
            created_at=parent.created_at,
        )
