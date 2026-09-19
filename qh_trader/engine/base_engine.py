"""[Engine 层] BaseEngine 交易引擎抽象与策略上下文基类 (S3-02, FR-MATCH-01, FR-EXEC-01).

严格遵守架构隔离原则：
- Engine 仅依赖 Core 协议与 Domain 业务内核；
- 绝不直接依赖 Strategy、Gateway 适配器或外部 Infrastructure；
- 策略以 StrategyPort 协议接入，执行网关以 ExecutionPort 接入，日志以 JournalPort 接入。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable

from qh_trader.core.clock import VirtualClock, utc_timestamp
from qh_trader.core.constants import (
    EventKind,
    Exchange,
    Offset,
    OrderStatus,
    OrderType,
    PositionSide,
    Side,
)
from qh_trader.core.event import CanonicalEvent
from qh_trader.core.objects import (
    Bar,
    ControlEpoch,
    InstrumentId,
    OrderIdentity,
    OrderIntent,
    OrderUpdate,
    Trade,
    require_text,
)
from qh_trader.core.ports import (
    ExecutionPort,
    JournalPort,
    RuleStorePort,
    StrategyContextPort,
    StrategyPort,
)
from qh_trader.domain.ledger import AccountLedger
from qh_trader.domain.orders import OrderManager
from qh_trader.domain.positions import PositionDetail, PositionManager
from qh_trader.domain.smart_router import (
    CloseCapabilityTable,
    ExchangeCloseCapability,
    SmartRouter,
)


def default_close_capability_table() -> CloseCapabilityTable:
    """提供国内期货各大交易所标准平今/平昨能力表."""
    caps = {
        Exchange.SHFE: ExchangeCloseCapability(
            supports_close_today=True,
            requires_explicit_close_today=True,
            supports_unified_close=False,
            evidence_ref="standard-shfe-rules",
        ),
        Exchange.INE: ExchangeCloseCapability(
            supports_close_today=True,
            requires_explicit_close_today=True,
            supports_unified_close=False,
            evidence_ref="standard-ine-rules",
        ),
        Exchange.DCE: ExchangeCloseCapability(
            supports_close_today=True,
            requires_explicit_close_today=False,
            supports_unified_close=True,
            evidence_ref="standard-dce-rules",
        ),
        Exchange.CZCE: ExchangeCloseCapability(
            supports_close_today=False,
            requires_explicit_close_today=False,
            supports_unified_close=True,
            evidence_ref="standard-czce-rules",
        ),
        Exchange.CFFEX: ExchangeCloseCapability(
            supports_close_today=True,
            requires_explicit_close_today=False,
            supports_unified_close=True,
            evidence_ref="standard-cffex-rules",
        ),
        Exchange.GFEX: ExchangeCloseCapability(
            supports_close_today=True,
            requires_explicit_close_today=False,
            supports_unified_close=True,
            evidence_ref="standard-gfex-rules",
        ),
    }
    return CloseCapabilityTable(version="v1.0-default", capabilities=caps)


class BaseEngine(StrategyContextPort):
    """引擎基类，实现 StrategyContextPort 为策略提供运行时环境."""

    def __init__(
        self,
        account_id: str,
        *,
        start_time: datetime,
        initial_capital: Decimal = Decimal("1000000.00"),
        trading_day: date | None = None,
        controller_id: str = "engine-controller",
        gateway: ExecutionPort | None = None,
        journal: JournalPort | None = None,
        rule_store: RuleStorePort | None = None,
        smart_router: SmartRouter | None = None,
        contract_multiplier: Decimal = Decimal("10"),
        commission_per_lot: Decimal = Decimal("5.0"),
        margin_ratio: Decimal = Decimal("0.1"),
    ) -> None:
        require_text(account_id, "account_id")
        self.account_id = account_id
        self.clock = VirtualClock(start_time)
        self.epoch = ControlEpoch(controller_id, 1)

        self.contract_multiplier = contract_multiplier
        self.commission_per_lot = commission_per_lot
        self.margin_ratio = margin_ratio

        # 初始化 S2 交易领域内核
        self.ledger = AccountLedger(
            account_id=account_id,
            initial_capital=initial_capital,
            trading_day=trading_day,
        )
        self.position_manager = self.ledger.position_manager
        self.order_manager = OrderManager()
        self.smart_router = smart_router or SmartRouter(default_close_capability_table())

        self.gateway = gateway
        self.journal = journal
        self.rule_store = rule_store

        self.strategies: dict[str, StrategyPort] = {}
        self._order_id_counter = 0

    # ------------------------------------------------------------------ 策略管理
    def add_strategy(self, strategy: StrategyPort) -> None:
        self.strategies[strategy.strategy_id] = strategy
        strategy.on_init()

    # ------------------------------------------------------------------ StrategyContextPort 接口
    def now(self) -> datetime:
        return self.clock.now()

    def get_position(self, instrument: InstrumentId) -> int:
        """获取当前净持仓 (多头 - 空头)."""
        pos_l = self.position_manager.get_position(instrument, PositionSide.LONG)
        pos_s = self.position_manager.get_position(instrument, PositionSide.SHORT)
        return pos_l.total_position - pos_s.total_position

    def send_order(
        self,
        instrument: InstrumentId,
        side: Side,
        offset: Offset,
        quantity: int,
        order_type: OrderType = OrderType.LIMIT,
        limit_price_ticks: int | None = None,
    ) -> str:
        """提交委托，经过 SmartRouter 拆单并进行持仓与资金预占，最后发往 Gateway."""
        if quantity <= 0:
            raise ValueError(f"order quantity must be positive: {quantity}")

        if limit_price_ticks is None and order_type == OrderType.LIMIT:
            order_type = OrderType.MARKET

        self._order_id_counter += 1
        parent_cid = f"ord-{self._order_id_counter}"
        now = self.now()

        # 1. 构造父委托意图
        parent_intent = OrderIntent(
            client_order_id=parent_cid,
            account_id=self.account_id,
            strategy_id="strategy",
            instrument=instrument,
            side=side,
            offset=offset,
            quantity=quantity,
            order_type=order_type,
            limit_price_ticks=limit_price_ticks,
            created_at=now,
        )
        self.order_manager.create_order(parent_intent)

        # 2. 路由拆单
        target_pos_side = PositionSide.LONG if (
            (side == Side.SELL and offset != Offset.OPEN) or (side == Side.BUY and offset == Offset.OPEN)
        ) else PositionSide.SHORT
        if offset != Offset.OPEN:
            target_pos = self.position_manager.get_position(instrument, target_pos_side)
        else:
            target_pos = None

        def id_gen() -> str:
            self._order_id_counter += 1
            return f"ord-{self._order_id_counter}"

        plan = self.smart_router.plan_order(parent_intent, target_pos, id_gen)

        # 3. 提交子单并预占
        for child_intent in plan.children:
            if child_intent.client_order_id != parent_cid:
                self.order_manager.create_order(child_intent)

            # 持仓预占 (仅平仓冻结)
            if child_intent.offset != Offset.OPEN:
                self.position_manager.reserve_for_order(
                    child_intent.client_order_id,
                    child_intent.instrument,
                    child_intent.side,
                    child_intent.offset,
                    child_intent.quantity,
                )

            # 资金预占 (粗略估算保证金与手续费)
            est_price = Decimal("3000")
            if child_intent.limit_price_ticks is not None:
                est_price = Decimal(child_intent.limit_price_ticks)
            est_margin = Decimal(0)
            if child_intent.offset == Offset.OPEN:
                est_margin = est_price * self.contract_multiplier * Decimal(child_intent.quantity) * self.margin_ratio
            est_fee = self.commission_per_lot * Decimal(child_intent.quantity)
            self.ledger.reserve_funds(child_intent.client_order_id, est_margin, est_fee)

            # 提交给网关
            if self.gateway is not None:
                res = self.gateway.submit(child_intent, self.epoch)
                self.order_manager.record_send_result(child_intent.client_order_id, res)

        return parent_cid

    def buy(
        self,
        instrument: InstrumentId,
        quantity: int,
        offset: Offset = Offset.OPEN,
        limit_price_ticks: int | None = None,
    ) -> str:
        order_type = OrderType.LIMIT if limit_price_ticks is not None else OrderType.MARKET
        return self.send_order(instrument, Side.BUY, offset, quantity, order_type, limit_price_ticks)

    def sell(
        self,
        instrument: InstrumentId,
        quantity: int,
        offset: Offset = Offset.CLOSE,
        limit_price_ticks: int | None = None,
    ) -> str:
        order_type = OrderType.LIMIT if limit_price_ticks is not None else OrderType.MARKET
        return self.send_order(instrument, Side.SELL, offset, quantity, order_type, limit_price_ticks)

    def cancel_order(self, client_order_id: str) -> None:
        order = self.order_manager.get_order(client_order_id)
        if order is None:
            return
        targets = [order]
        if order.child_order_ids:
            targets = [self.order_manager.get_order(cid) for cid in order.child_order_ids]

        for o in targets:
            if o and o.identity and self.gateway:
                self.gateway.cancel(o.identity, self.epoch)

    # ------------------------------------------------------------------ 事件分发
    def process_order_event(self, event: CanonicalEvent[OrderUpdate]) -> None:
        update: OrderUpdate = event.payload
        self.order_manager.process_order_update(update)
        for strat in self.strategies.values():
            strat.on_order(update)

    def process_trade_event(self, event: CanonicalEvent[Trade]) -> None:
        trade: Trade = event.payload
        order, is_new = self.order_manager.process_trade(trade)
        if not is_new:
            return

        cid = order.client_order_id if order else None
        commission = Decimal("0")
        if self.rule_store is not None:
            try:
                rule_val = self.rule_store.commission_rule(
                    instrument=trade.instrument,
                    profile="default",
                    offset=trade.offset,
                    effective_at=trade.event_time,
                    known_at=trade.available_at,
                )
                rule = rule_val.value
                fixed_part = rule.fixed_per_lot * Decimal(trade.quantity)
                ratio_part = trade.price * Decimal(trade.quantity) * self.contract_multiplier * rule.ad_valorem_rate
                commission = fixed_part + ratio_part
            except Exception:
                commission = self.commission_per_lot * Decimal(trade.quantity)
        else:
            commission = self.commission_per_lot * Decimal(trade.quantity)

        self.ledger.on_trade(
            trade,
            commission=commission,
            multiplier=self.contract_multiplier,
            client_order_id=cid,
        )

        for strat in self.strategies.values():
            strat.on_trade(trade)
