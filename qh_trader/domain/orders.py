"""[Domain 层] 订单实体、状态机、成交去重与发送状态 (S2-01, FR-ORD-04, FR-ORD-06, FR-REC-02).

主要职责:
1. 订单生命周期与严格单向状态机 (不倒退规则，终态后仍可处理迟到成交)
2. 独立发送状态 (SendState: NOT_SENT, SENT_UNKNOWN, CONFIRMED_REMOTE)
3. 独立撤单状态 (cancel_pending)
4. 严格成交去重 (TradeKey / account_id|exchange|trading_day|trade_id)
5. 待关联真实成交暂存与事后绑定 (UnlinkedTrade)
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


def _format_trade_key(trade_or_key: Trade | TradeKey | str) -> str:
    if isinstance(trade_or_key, str):
        return trade_or_key
    if isinstance(trade_or_key, TradeKey):
        parts = [
            trade_or_key.account_id,
            str(trade_or_key.exchange.value if hasattr(trade_or_key.exchange, "value") else trade_or_key.exchange),
            str(trade_or_key.trading_day),
            trade_or_key.trade_id,
        ]
        if trade_or_key.extra_scope:
            parts.extend(trade_or_key.extra_scope)
        return "|".join(parts)
    if isinstance(trade_or_key, Trade):
        ex = trade_or_key.instrument.exchange
        ex_str = str(ex.value if hasattr(ex, "value") else ex)
        return f"{trade_or_key.account_id}|{ex_str}|{trade_or_key.trading_day}|{trade_or_key.trade_id}"
    raise TypeError(f"unsupported trade key type: {type(trade_or_key).__name__}")


class TradeDeduplicator:
    """真实成交去重器.

    硬约束 (FR-ORD-05, FR-REC-02):
    同一成交通过多种回调重复到达时，业务结果绝不重复计算.
    """

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
class Order:
    """订单领域实体与状态机."""

    intent: OrderIntent
    status: OrderStatus = OrderStatus.CREATED
    send_state: SendState = SendState.NOT_SENT
    send_evidence: str | None = None
    identity: OrderIdentity | None = None

    cum_filled_qty: int = 0
    accounted_filled_qty: int = 0
    cancel_pending: bool = False

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
        return self.status in {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}

    @property
    def is_active(self) -> bool:
        return not self.is_terminal

    @property
    def leaves_qty(self) -> int:
        """未成交数量 (委托量 - 柜台累计成交量)."""
        return max(0, self.quantity - self.cum_filled_qty)

    @property
    def unaccounted_fill_qty(self) -> int:
        """已由柜台回报确认成交但本系统账本尚未入账的成交差额.

        A02 / terminal_before_fills:
        若终态订单回报中的累计成交量大于已入账成交量，保留差额对应的冻结，等待迟到成交入账.
        """
        return max(0, self.cum_filled_qty - self.accounted_filled_qty)

    def mark_submitting(self) -> None:
        if self.status != OrderStatus.CREATED:
            raise ValueError(f"cannot submit order from status: {self.status}")
        self.status = OrderStatus.SUBMITTING
        self.updated_at = datetime.now(timezone.utc)

    def apply_send_result(self, result: LocalSendResult) -> None:
        """处理底层网络发送调用结果 (FR-ORD-06, A03)."""
        self.send_state = result.state
        self.send_evidence = result.evidence
        self.updated_at = datetime.now(timezone.utc)

        if result.state == SendState.NOT_SENT:
            # 明确未送出网络 (如本地校验失败或前置阻断)
            if self.status in {OrderStatus.CREATED, OrderStatus.SUBMITTING}:
                self.status = OrderStatus.REJECTED
        elif result.state == SendState.SENT_UNKNOWN:
            # 本地返回 0 但未确认送达柜台，或发送异常/超时/断线: 保留预占，升级对账，严禁重发
            self.reconciliation_required = True
            self.reconciliation_reason = f"SendState unknown: {result.evidence}"

    def apply_order_update(self, update: OrderUpdate) -> None:
        """处理柜台订单回报 (OnRtnOrder / OnRspOrderInsert 等)."""
        if update.instrument != self.instrument:
            raise ValueError(f"instrument mismatch: {update.instrument} != {self.instrument}")
        if update.side != self.side:
            raise ValueError(f"side mismatch: {update.side} != {self.side}")

        self.updated_at = datetime.now(timezone.utc)

        # 合并柜台标识信息
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

        # 远端已确认存在该订单
        self.send_state = SendState.CONFIRMED_REMOTE
        self.reconciliation_required = False

        # 累计成交量只增不减 (防御性上界校验, D2)
        if update.filled_quantity > self.quantity:
            self.reconciliation_required = True
            self.reconciliation_reason = (
                f"remote filled_quantity ({update.filled_quantity}) exceeds quantity ({self.quantity})"
            )
            self.cum_filled_qty = self.quantity
        elif update.filled_quantity > self.cum_filled_qty:
            self.cum_filled_qty = update.filled_quantity

        # 状态机流转 (防倒退规则: 终态不能倒退回活动状态)
        target = update.status
        if self.is_terminal:
            # 已在终态，如迟到的 ACCEPTED 或 PARTIALLY_FILLED，直接忽略状态变更，保持当前终态
            return

        # 正常流转
        self.status = target
        if self.is_terminal:
            self.cancel_pending = False

    def apply_trade(self, trade: Trade) -> bool:
        """将真实成交事件入账到本订单.

        FR-ORD-04 / A02:
        订单终态与累计成交事实分开维护；CANCELED 后仍可补记迟到成交.
        """
        if trade.instrument != self.instrument:
            raise ValueError(f"trade instrument mismatch: {trade.instrument} != {self.instrument}")
        if trade.side != self.side:
            raise ValueError(f"trade side mismatch: {trade.side} != {self.side}")

        self.trades.append(trade)
        if self.accounted_filled_qty + trade.quantity > self.quantity:
            self.reconciliation_required = True
            self.reconciliation_reason = (
                f"trade fill exceeds order quantity: current={self.accounted_filled_qty}, "
                f"trade={trade.quantity}, total={self.quantity}"
            )
        self.accounted_filled_qty += trade.quantity
        if self.accounted_filled_qty > self.cum_filled_qty:
            self.cum_filled_qty = self.accounted_filled_qty

        self.updated_at = datetime.now(timezone.utc)

        # 若未处于终态且已全部成交，转为 FILLED
        if not self.is_terminal and self.cum_filled_qty >= self.quantity:
            self.status = OrderStatus.FILLED
            self.cancel_pending = False

        return True

    def request_cancel(self) -> None:
        """发起撤单请求."""
        if self.is_terminal:
            raise ValueError(f"cannot cancel terminal order: {self.status}")
        self.cancel_pending = True
        self.updated_at = datetime.now(timezone.utc)

    def cancel_rejected(self, reason: str) -> None:
        """撤单被柜台拒绝 (原单仍然可能有效，预占继续保留)."""
        self.cancel_pending = False
        self.updated_at = datetime.now(timezone.utc)


@dataclass
class UnlinkedTrade:
    """待关联的真实成交记录 (FR-ORD-05)."""

    trade: Trade
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    resolved: bool = False
    linked_client_order_id: str | None = None


class OrderManager:
    """订单聚合根与管理中心."""

    def __init__(self) -> None:
        self._orders_by_client_id: dict[str, Order] = {}
        self._client_id_by_exchange_order: dict[tuple[Exchange, str], str] = {}
        self._client_id_by_session: dict[tuple[int, int, str], str] = {}
        self.deduplicator: TradeDeduplicator = TradeDeduplicator()
        self.unlinked_trades: list[UnlinkedTrade] = []

    def create_order(self, intent: OrderIntent) -> Order:
        if intent.client_order_id in self._orders_by_client_id:
            raise ValueError(f"duplicate client_order_id: {intent.client_order_id}")
        order = Order(intent=intent)
        self._orders_by_client_id[intent.client_order_id] = order

        # 建立父子单双向索引
        if intent.parent_order_id and intent.parent_order_id in self._orders_by_client_id:
            parent = self._orders_by_client_id[intent.parent_order_id]
            parent.child_order_ids.append(intent.client_order_id)

        return order

    def get_order(self, client_order_id: str) -> Order | None:
        return self._orders_by_client_id.get(client_order_id)

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

    def record_send_result(self, client_order_id: str, result: LocalSendResult) -> Order:
        order = self._orders_by_client_id.get(client_order_id)
        if not order:
            raise KeyError(f"unknown client_order_id: {client_order_id}")
        order.apply_send_result(result)
        return order

    def process_order_update(self, update: OrderUpdate) -> Order:
        ident = update.identity
        order = self.find_matching_order(
            client_order_id=ident.client_order_id,
            exchange=ident.exchange,
            exchange_order_id=ident.exchange_order_id,
            front_id=ident.front_id,
            session_id=ident.session_id,
            order_ref=ident.order_ref,
        )
        if not order:
            raise KeyError(f"no local order matched update identity: {ident}")

        # 维护反向查找索引
        if ident.exchange_order_id:
            self._client_id_by_exchange_order[(ident.exchange, ident.exchange_order_id)] = order.client_order_id
        if ident.front_id is not None and ident.session_id is not None and ident.order_ref:
            self._client_id_by_session[(ident.front_id, ident.session_id, ident.order_ref)] = order.client_order_id

        order.apply_order_update(update)

        # 尝试将此前暂存的未关联成交与当前订单进行匹配
        self._check_unlinked_trades(order)
        return order

    def process_trade(self, trade: Trade, target_client_order_id: str | None = None) -> tuple[Order | None, bool]:
        """处理成交回报.

        返回 (Order | None, is_new_trade):
        - 若成交为重复事件: 返回 (order, False)
        - 若成交为新事件且成功关联到订单: 返回 (order, True)
        - 若成交为新事件但无法关联到订单: 存入待关联队列，返回 (None, True)
        """
        if self.deduplicator.is_duplicate(trade):
            # 重复事实，忽略
            order = self._orders_by_client_id.get(target_client_order_id) if target_client_order_id else None
            return order, False

        # 记录成交去重键
        self.deduplicator.record(trade)

        order: Order | None = None
        if target_client_order_id:
            order = self._orders_by_client_id.get(target_client_order_id)
        else:
            # 根据品种、方向和未平数量尝试匹配活动或已终态的未结单
            for cand in self._orders_by_client_id.values():
                if (
                    cand.instrument == trade.instrument
                    and cand.side == trade.side
                    and cand.offset == trade.offset
                    and cand.accounted_filled_qty < cand.quantity
                ):
                    order = cand
                    break

        if order is not None:
            order.apply_trade(trade)
            return order, True

        # 无法立即关联，存入待关联队列 (FR-ORD-05)
        self.unlinked_trades.append(UnlinkedTrade(trade=trade))
        return None, True

    def _check_unlinked_trades(self, order: Order) -> None:
        """检查是否有待关联成交可以补关联到该订单."""
        for item in self.unlinked_trades:
            if item.resolved:
                continue
            tr = item.trade
            if (
                tr.instrument == order.instrument
                and tr.side == order.side
                and tr.offset == order.offset
                and order.accounted_filled_qty < order.quantity
            ):
                order.apply_trade(tr)
                item.resolved = True
                item.linked_client_order_id = order.client_order_id
