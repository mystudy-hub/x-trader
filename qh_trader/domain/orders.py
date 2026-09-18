"""[Domain 层] 订单实体、状态机、成交去重与发送状态 (S2-01, S2-07, FR-ORD-04, FR-ORD-05, FR-ORD-06, FR-REC-02).

主要职责:
1. 订单生命周期与严格单向状态机 (不倒退规则，终态后仍可处理迟到成交)
2. 独立发送状态 (SendState: NOT_SENT, SENT_UNKNOWN, CONFIRMED_REMOTE)，只允许向更强证据推进
3. 独立撤单状态 (cancel_pending)，撤单拒绝保留原因
4. 严格成交去重 (TradeKey 含适配器定义的 extra_scope)
5. 成交与订单的关联只按可唯一归属的远端标识 (ExchangeID + OrderSysID 或原会话三元组) 进行，
   绝不按品种、方向猜配 (FR-ORD-05)；无法归属的真实成交与无法归属的订单回报分别进入待关联队列
   与外部占位记录，等待恢复协调器处置。
6. 对账标记只能由明确的对账处置清除，不因后续任意回报被覆盖。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from qh_trader.core.constants import (
    Exchange,
    Offset,
    OrderStatus,
    OrderType,
    SendState,
    Side,
)
from qh_trader.core.objects import (
    InstrumentId,
    LocalSendResult,
    OrderIdentity,
    OrderIntent,
    OrderUpdate,
    Trade,
    TradeKey,
)

TERMINAL_STATUSES = frozenset({OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED})

_SEND_STATE_RANK = {SendState.NOT_SENT: 0, SendState.SENT_UNKNOWN: 1, SendState.CONFIRMED_REMOTE: 2}


def _format_trade_key(trade_or_key: Trade | TradeKey | str) -> str:
    if isinstance(trade_or_key, str):
        return trade_or_key
    if isinstance(trade_or_key, Trade):
        trade_or_key = trade_or_key.deduplication_key
    if isinstance(trade_or_key, TradeKey):
        ex = trade_or_key.exchange
        parts = [
            trade_or_key.account_id,
            str(ex.value if hasattr(ex, "value") else ex),
            str(trade_or_key.trading_day),
            trade_or_key.trade_id,
            *trade_or_key.extra_scope,
        ]
        return "|".join(parts)
    raise TypeError(f"unsupported trade key type: {type(trade_or_key).__name__}")


class TradeDeduplicator:
    """真实成交去重器 (FR-ORD-05, FR-REC-02)：同一成交多次到达时业务结果绝不重复计算."""

    def __init__(self, seen_keys: Sequence[str] | None = None) -> None:
        self._seen: set[str] = set(seen_keys or ())

    def is_duplicate(self, trade_or_key: Trade | TradeKey | str) -> bool:
        return _format_trade_key(trade_or_key) in self._seen

    def record(self, trade_or_key: Trade | TradeKey | str) -> bool:
        """记录成交. 若是新成交返回 True，若是重复成交返回 False."""
        key = _format_trade_key(trade_or_key)
        if key in self._seen:
            return False
        self._seen.add(key)
        return True

    @property
    def seen_count(self) -> int:
        return len(self._seen)

    def snapshot(self) -> list[str]:
        return sorted(self._seen)


@dataclass
class SendAttempt:
    attempted_at: datetime
    state: SendState
    evidence: str
    local_code: int | None


@dataclass
class Order:
    """订单领域实体与状态机."""

    intent: OrderIntent
    status: OrderStatus = OrderStatus.CREATED
    send_state: SendState = SendState.NOT_SENT
    send_evidence: str | None = None
    send_attempts: list[SendAttempt] = field(default_factory=list)
    identity: OrderIdentity | None = None
    # 发出命令时的控制代次；旧代次的成交仍是事实
    command_epoch: int | None = None

    cum_filled_qty: int = 0
    accounted_filled_qty: int = 0
    cancel_pending: bool = False
    cancel_reject_reason: str | None = None

    reconciliation_required: bool = False
    reconciliation_reason: str | None = None

    child_order_ids: list[str] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)

    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def client_order_id(self) -> str:
        return self.intent.client_order_id

    @property
    def account_id(self) -> str:
        return self.intent.account_id

    @property
    def strategy_id(self) -> str:
        return self.intent.strategy_id

    @property
    def instrument(self) -> InstrumentId:
        return self.intent.instrument

    @property
    def side(self) -> Side:
        return self.intent.side

    @property
    def offset(self) -> Offset:
        return self.intent.offset

    @property
    def quantity(self) -> int:
        return self.intent.quantity

    @property
    def order_type(self) -> OrderType:
        return self.intent.order_type

    @property
    def limit_price_ticks(self) -> int | None:
        return self.intent.limit_price_ticks

    @property
    def parent_order_id(self) -> str | None:
        return self.intent.parent_order_id

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_active(self) -> bool:
        return not self.is_terminal

    @property
    def leaves_qty(self) -> int:
        """未成交数量 (委托量 - 柜台累计成交量)."""
        return max(0, self.quantity - self.cum_filled_qty)

    @property
    def unaccounted_fill_qty(self) -> int:
        """柜台回报确认成交但本系统账本尚未入账的差额 (A02 / terminal_before_fills)."""
        return max(0, self.cum_filled_qty - self.accounted_filled_qty)

    @property
    def is_pending_remote(self) -> bool:
        """发送结果未知且尚无远端证据，需要恢复协调器核对 (A03)."""
        return self.send_state == SendState.SENT_UNKNOWN and self.is_active

    def _touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc)

    def flag_reconciliation(self, reason: str) -> None:
        self.reconciliation_required = True
        self.reconciliation_reason = reason
        self._touch()

    def clear_reconciliation(self, resolution: str) -> None:
        """只有明确的对账处置才能清除标记."""
        self.reconciliation_required = False
        self.reconciliation_reason = f"resolved: {resolution}"
        self._touch()

    def mark_submitting(self, command_epoch: int | None = None) -> None:
        if self.status != OrderStatus.CREATED:
            raise ValueError(f"cannot submit order from status: {self.status}")
        self.status = OrderStatus.SUBMITTING
        if command_epoch is not None:
            self.command_epoch = command_epoch
        self._touch()

    def apply_send_result(self, result: LocalSendResult) -> None:
        """处理底层网络发送调用结果 (FR-ORD-06, A03).

        send_state 只能向更强证据推进：远端已确认后，本地的 NOT_SENT / SENT_UNKNOWN 结果不再改变状态。
        """
        self.send_attempts.append(
            SendAttempt(datetime.now(timezone.utc), result.state, result.evidence, result.local_code)
        )
        self._touch()
        if _SEND_STATE_RANK[result.state] < _SEND_STATE_RANK[self.send_state]:
            self.flag_reconciliation(
                f"send result {result.state} arrived after send_state={self.send_state}: {result.evidence}"
            )
            return

        self.send_state = result.state
        self.send_evidence = result.evidence
        if result.state == SendState.NOT_SENT:
            if self.status in {OrderStatus.CREATED, OrderStatus.SUBMITTING}:
                self.status = OrderStatus.REJECTED
        elif result.state == SendState.SENT_UNKNOWN:
            self.flag_reconciliation(f"SendState unknown: {result.evidence}")

    def apply_order_update(self, update: OrderUpdate) -> None:
        """处理柜台订单回报 (OnRtnOrder / OnRspOrderInsert 等)."""
        if update.instrument != self.instrument:
            raise ValueError(f"instrument mismatch: {update.instrument} != {self.instrument}")
        if update.side != self.side:
            raise ValueError(f"side mismatch: {update.side} != {self.side}")
        self._touch()

        if self.identity is None:
            self.identity = update.identity
        else:
            merged_dict: dict[str, Any] = {
                "account_id": self.account_id,
                "exchange": self.instrument.exchange,
                "client_order_id": self.client_order_id,
                "exchange_order_id": update.identity.exchange_order_id or self.identity.exchange_order_id,
                "front_id": (
                    update.identity.front_id if update.identity.front_id is not None else self.identity.front_id
                ),
                "session_id": (
                    update.identity.session_id if update.identity.session_id is not None else self.identity.session_id
                ),
                "order_ref": update.identity.order_ref or self.identity.order_ref,
            }
            self.identity = OrderIdentity(**merged_dict)

        # 远端已确认存在该订单；未知发送状态由此关闭，但既有对账标记不因回报自动清除
        if (
            self.send_state == SendState.SENT_UNKNOWN
            and self.reconciliation_reason
            and self.reconciliation_reason.startswith("SendState unknown")
        ):
            self.clear_reconciliation("remote order report received")
        self.send_state = SendState.CONFIRMED_REMOTE

        if update.filled_quantity > self.quantity:
            self.flag_reconciliation(
                f"remote filled_quantity ({update.filled_quantity}) exceeds quantity ({self.quantity})"
            )
            self.cum_filled_qty = self.quantity
        elif update.filled_quantity > self.cum_filled_qty:
            self.cum_filled_qty = update.filled_quantity

        if self.is_terminal:
            # 迟到的 ACCEPTED / PARTIALLY_FILLED 不能使终态倒退
            return
        self.status = update.status
        if self.is_terminal:
            self.cancel_pending = False

    def apply_trade(self, trade: Trade) -> bool:
        """将真实成交事件入账到本订单 (FR-ORD-04 / A02: 终态后仍可补记迟到成交)."""
        if trade.instrument != self.instrument:
            raise ValueError(f"trade instrument mismatch: {trade.instrument} != {self.instrument}")
        if trade.side != self.side:
            raise ValueError(f"trade side mismatch: {trade.side} != {self.side}")

        self.trades.append(trade)
        if self.accounted_filled_qty + trade.quantity > self.quantity:
            self.flag_reconciliation(
                f"trade fill exceeds order quantity: current={self.accounted_filled_qty}, "
                f"trade={trade.quantity}, total={self.quantity}"
            )
        self.accounted_filled_qty += trade.quantity
        if self.accounted_filled_qty > self.cum_filled_qty:
            self.cum_filled_qty = self.accounted_filled_qty
        self.send_state = SendState.CONFIRMED_REMOTE
        self._touch()

        if not self.is_terminal and self.cum_filled_qty >= self.quantity:
            self.status = OrderStatus.FILLED
            self.cancel_pending = False
        return True

    def request_cancel(self) -> None:
        if self.is_terminal:
            raise ValueError(f"cannot cancel terminal order: {self.status}")
        self.cancel_pending = True
        self.cancel_reject_reason = None
        self._touch()

    def cancel_rejected(self, reason: str) -> None:
        """撤单被柜台拒绝 (原单仍然可能有效，预占继续保留)."""
        self.cancel_pending = False
        self.cancel_reject_reason = reason
        self._touch()


@dataclass
class UnlinkedTrade:
    """待关联的真实成交记录 (FR-ORD-05)."""

    trade: Trade
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    resolved: bool = False
    linked_client_order_id: str | None = None


@dataclass
class ExternalOrderRecord:
    """无法归属本地意图的订单回报占位记录 (FR-ORD-05: 建立带远端标识的占位记录)."""

    identity: OrderIdentity
    updates: list[OrderUpdate] = field(default_factory=list)
    first_seen_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class OrderManager:
    """订单聚合根与管理中心."""

    def __init__(self) -> None:
        self._orders_by_client_id: dict[str, Order] = {}
        self._client_id_by_exchange_order: dict[tuple[Exchange, str], str] = {}
        self._client_id_by_session: dict[tuple[int, int, str], str] = {}
        self.deduplicator: TradeDeduplicator = TradeDeduplicator()
        self.unlinked_trades: list[UnlinkedTrade] = []
        self.external_orders: dict[tuple[Exchange, str] | tuple[int, int, str], ExternalOrderRecord] = {}

    # ------------------------------------------------------------------ 创建与查询
    def create_order(self, intent: OrderIntent) -> Order:
        if intent.client_order_id in self._orders_by_client_id:
            raise ValueError(f"duplicate client_order_id: {intent.client_order_id}")
        order = Order(intent=intent)
        self._orders_by_client_id[intent.client_order_id] = order
        if intent.parent_order_id:
            parent = self._orders_by_client_id.get(intent.parent_order_id)
            if parent is None:
                raise ValueError(
                    f"parent order {intent.parent_order_id} must exist before child {intent.client_order_id}"
                )
            parent.child_order_ids.append(intent.client_order_id)
        return order

    def bind_session_identity(self, client_order_id: str, front_id: int, session_id: int, order_ref: str) -> None:
        """报单前登记原会话三元组映射 (FR-ORD-05: 本地意图及原会话订单映射必须在报单前存在)."""
        order = self._orders_by_client_id.get(client_order_id)
        if order is None:
            raise KeyError(f"unknown client_order_id: {client_order_id}")
        key = (front_id, session_id, order_ref)
        existing = self._client_id_by_session.get(key)
        if existing is not None and existing != client_order_id:
            raise ValueError(f"session identity {key} already bound to {existing}")
        self._client_id_by_session[key] = client_order_id
        order.identity = OrderIdentity(
            account_id=order.account_id,
            exchange=order.instrument.exchange,
            client_order_id=client_order_id,
            exchange_order_id=order.identity.exchange_order_id if order.identity else None,
            front_id=front_id,
            session_id=session_id,
            order_ref=order_ref,
        )

    def get_order(self, client_order_id: str) -> Order | None:
        return self._orders_by_client_id.get(client_order_id)

    def orders(self) -> tuple[Order, ...]:
        return tuple(self._orders_by_client_id.values())

    def active_orders(self, instrument: InstrumentId | None = None) -> list[Order]:
        return [
            o
            for o in self._orders_by_client_id.values()
            if o.is_active and (instrument is None or o.instrument == instrument)
        ]

    def get_order_by_exchange_id(self, exchange: Exchange, exchange_order_id: str) -> Order | None:
        cid = self._client_id_by_exchange_order.get((exchange, exchange_order_id))
        return self._orders_by_client_id.get(cid) if cid else None

    def get_order_by_session(self, front_id: int, session_id: int, order_ref: str) -> Order | None:
        cid = self._client_id_by_session.get((front_id, session_id, order_ref))
        return self._orders_by_client_id.get(cid) if cid else None

    def find_matching_order(
        self,
        *,
        client_order_id: str | None = None,
        exchange: Exchange | None = None,
        exchange_order_id: str | None = None,
        front_id: int | None = None,
        session_id: int | None = None,
        order_ref: str | None = None,
    ) -> Order | None:
        """只按可唯一归属的标识查找：本地单号、交易所单号、完整原会话三元组。OrderRef 单独不作键。"""
        if client_order_id and client_order_id in self._orders_by_client_id:
            return self._orders_by_client_id[client_order_id]
        if exchange and exchange_order_id:
            order = self.get_order_by_exchange_id(exchange, exchange_order_id)
            if order:
                return order
        if front_id is not None and session_id is not None and order_ref:
            order = self.get_order_by_session(front_id, session_id, order_ref)
            if order:
                return order
        return None

    def _find_by_identity(self, ident: OrderIdentity | None) -> Order | None:
        if ident is None:
            return None
        return self.find_matching_order(
            client_order_id=ident.client_order_id,
            exchange=ident.exchange,
            exchange_order_id=ident.exchange_order_id,
            front_id=ident.front_id,
            session_id=ident.session_id,
            order_ref=ident.order_ref,
        )

    def _index_identity(self, order: Order, ident: OrderIdentity) -> None:
        if ident.exchange_order_id:
            key_ex = (ident.exchange, ident.exchange_order_id)
            existing = self._client_id_by_exchange_order.get(key_ex)
            if existing is not None and existing != order.client_order_id:
                order.flag_reconciliation(f"exchange_order_id {ident.exchange_order_id} already bound to {existing}")
                return
            self._client_id_by_exchange_order[key_ex] = order.client_order_id
        if ident.front_id is not None and ident.session_id is not None and ident.order_ref:
            key_s = (ident.front_id, ident.session_id, ident.order_ref)
            existing = self._client_id_by_session.get(key_s)
            if existing is not None and existing != order.client_order_id:
                order.flag_reconciliation(f"session identity {key_s} already bound to {existing}")
                return
            self._client_id_by_session[key_s] = order.client_order_id

    # ------------------------------------------------------------------ 发送
    def record_send_result(self, client_order_id: str, result: LocalSendResult) -> Order:
        order = self._orders_by_client_id.get(client_order_id)
        if not order:
            raise KeyError(f"unknown client_order_id: {client_order_id}")
        order.apply_send_result(result)
        return order

    # ------------------------------------------------------------------ 回报
    def process_order_update(self, update: OrderUpdate) -> Order | None:
        """处理订单回报。无法归属的回报记为外部占位，返回 None，不丢弃也不猜配."""
        ident = update.identity
        order = self._find_by_identity(ident)
        if order is None:
            self._record_external_order(update)
            return None
        self._index_identity(order, ident)
        order.apply_order_update(update)
        self._check_unlinked_trades(order)
        return order

    def _external_key(self, ident: OrderIdentity) -> tuple[Exchange, str] | tuple[int, int, str]:
        if ident.exchange_order_id:
            return (ident.exchange, ident.exchange_order_id)
        assert ident.front_id is not None and ident.session_id is not None and ident.order_ref
        return (ident.front_id, ident.session_id, ident.order_ref)

    def _record_external_order(self, update: OrderUpdate) -> ExternalOrderRecord:
        key = self._external_key(update.identity)
        record = self.external_orders.get(key)
        if record is None:
            record = ExternalOrderRecord(identity=update.identity)
            self.external_orders[key] = record
        record.updates.append(update)
        return record

    # ------------------------------------------------------------------ 成交
    def process_trade(self, trade: Trade, target_client_order_id: str | None = None) -> tuple[Order | None, bool]:
        """处理成交回报.

        返回 (Order | None, is_new_trade):
        - 重复事件: (order, False)，不做任何入账
        - 新事件且可唯一归属 (显式本地单号或成交携带的远端标识): (order, True)
        - 新事件但无法归属: 存入待关联队列，返回 (None, True)。绝不按品种、方向猜配。
        """
        if self.deduplicator.is_duplicate(trade):
            order = self._orders_by_client_id.get(target_client_order_id) if target_client_order_id else None
            if order is None:
                order = self._find_by_identity(trade.order_identity)
            return order, False
        self.deduplicator.record(trade)

        order: Order | None = None
        if target_client_order_id:
            order = self._orders_by_client_id.get(target_client_order_id)
            if order is None:
                raise KeyError(f"unknown target client_order_id: {target_client_order_id}")
        else:
            order = self._find_by_identity(trade.order_identity)

        if order is not None:
            if trade.order_identity is not None:
                self._index_identity(order, trade.order_identity)
            order.apply_trade(trade)
            return order, True

        self.unlinked_trades.append(UnlinkedTrade(trade=trade))
        return None, True

    def _check_unlinked_trades(self, order: Order) -> None:
        """订单回报到达后，只把远端标识能唯一归属到该订单的待关联成交补关联."""
        for item in self.unlinked_trades:
            if item.resolved or item.trade.order_identity is None:
                continue
            if self._find_by_identity(item.trade.order_identity) is order:
                order.apply_trade(item.trade)
                item.resolved = True
                item.linked_client_order_id = order.client_order_id

    def resolve_unlinked_trade(self, trade_id: str, client_order_id: str, resolution: str) -> Order:
        """恢复协调器 / 人工确认归属后一次性入账 (FR-ORD-05)."""
        order = self._orders_by_client_id.get(client_order_id)
        if order is None:
            raise KeyError(f"unknown client_order_id: {client_order_id}")
        for item in self.unlinked_trades:
            if not item.resolved and item.trade.trade_id == trade_id:
                order.apply_trade(item.trade)
                item.resolved = True
                item.linked_client_order_id = client_order_id
                order.clear_reconciliation(resolution) if order.reconciliation_required else None
                return order
        raise KeyError(f"no pending unlinked trade with id {trade_id}")

    def pending_unlinked_trades(self) -> list[UnlinkedTrade]:
        return [item for item in self.unlinked_trades if not item.resolved]

    @property
    def has_open_reconciliation(self) -> bool:
        """存在待关联成交、外部委托或对账标记时，不得发布可交易快照."""
        return (
            bool(self.pending_unlinked_trades())
            or bool(self.external_orders)
            or any(o.reconciliation_required for o in self._orders_by_client_id.values())
        )
