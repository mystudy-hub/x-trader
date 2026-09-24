"""[Gateway 适配器] 纸面模式账户查询：由模拟网关回报流投影的“柜台视图” (S5-04 装配, FR-REC-04).

模拟网关没有独立账户系统。本适配器订阅同一条回报流，维护订单 / 成交 / 持仓视图作为“远端”答复；
资金余额没有独立来源，只能镜像本地账本并在 ``source_id`` 里声明为 ``paper-mirror``。
它满足 ``AccountQueryPort`` 的批次与完成标志契约，但**不构成独立证据**：纸面对账只能证明
回报入账与查询合并的机制，不能替代柜台联调 (A04 / A23 柜台部分)。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

from qh_trader.core.constants import EventKind, Offset, OrderStatus, PositionSide, Side
from qh_trader.core.event import CanonicalEvent
from qh_trader.core.objects import (
    AccountFunds,
    InstrumentId,
    OrderUpdate,
    Position,
    QueryBatch,
    QueryRateLimit,
    QueryResult,
    Trade,
    require_text,
)
from qh_trader.core.ports import AccountQueryPort

PAPER_SOURCE = "paper-mirror"


def _target_position_side(side: Side, offset: Offset) -> PositionSide:
    """开仓同向；平仓反向 (与 domain.positions.target_position_side 同义，网关层不依赖领域层)."""
    if offset == Offset.OPEN:
        return PositionSide.LONG if side == Side.BUY else PositionSide.SHORT
    return PositionSide.LONG if side == Side.SELL else PositionSide.SHORT


class PaperQueryAdapter(AccountQueryPort):
    """按回报流投影的纸面查询；``observe`` 必须收到网关发出的每一条回报."""

    def __init__(
        self,
        account_id: str,
        *,
        trading_day: date,
        balance: Callable[[], Decimal | None],
        now: Callable[[], datetime],
    ) -> None:
        require_text(account_id, "account_id")
        self.account_id = account_id
        self.trading_day = trading_day
        self._balance = balance
        self._now = now
        self._orders: dict[str, OrderUpdate] = {}
        self._trades: dict[str, Trade] = {}
        self._positions: dict[tuple[InstrumentId, PositionSide], list[int]] = {}

    def observe(self, events: Iterable[CanonicalEvent]) -> None:
        for event in events:
            payload = event.payload
            if event.kind == EventKind.ORDER_REPORT and isinstance(payload, OrderUpdate):
                key = payload.identity.client_order_id or payload.identity.order_ref or event.event_id
                self._orders[key] = payload
            elif event.kind == EventKind.TRADE_REPORT and isinstance(payload, Trade):
                if payload.trade_id in self._trades:
                    continue
                self._trades[payload.trade_id] = payload
                side = _target_position_side(payload.side, payload.offset)
                bucket = self._positions.setdefault((payload.instrument, side), [0, 0])
                if payload.offset == Offset.OPEN:
                    bucket[1] += payload.quantity
                elif payload.offset == Offset.CLOSE_YESTERDAY:
                    bucket[0] -= payload.quantity
                elif payload.offset == Offset.CLOSE_TODAY:
                    bucket[1] -= payload.quantity
                else:
                    taken = min(bucket[0], payload.quantity)
                    bucket[0] -= taken
                    bucket[1] -= payload.quantity - taken
            elif event.kind == EventKind.CONTROL and isinstance(payload, Mapping):
                # 重放 Journal 历史时按日终推进把今仓转为昨仓 (与账户模型的 advance_trading_day 事实同源)
                if payload.get("action") == "advance_trading_day":
                    self.advance_trading_day(payload["new_trading_day"])

    def advance_trading_day(self, new_trading_day: date) -> None:
        for bucket in self._positions.values():
            bucket[0] += bucket[1]
            bucket[1] = 0
        self.trading_day = new_trading_day

    def _batch(self, kind: str) -> QueryBatch:
        return QueryBatch(f"{kind}-{uuid4().hex}", self.account_id, self.trading_day, self._now())

    def _result(self, kind: str, records: tuple) -> QueryResult:
        return QueryResult(
            batch=self._batch(kind),
            records=records,
            available_at=self._now(),
            source_id=PAPER_SOURCE,
            source_version="paper-v1",
            complete=True,
        )

    def query_account(self, batch: QueryBatch) -> QueryResult[AccountFunds]:
        balance = self._balance()
        funds = AccountFunds(balance, balance, None, balance)
        return self._result("funds", (funds,))

    def query_positions(self, batch: QueryBatch) -> QueryResult[Position]:
        records = tuple(
            Position(
                instrument=instrument,
                side=side,
                hedge_flag="SPECULATION",
                pos_yd=max(0, yd),
                pos_td=max(0, td),
                frozen_yd=0,
                frozen_td=0,
            )
            for (instrument, side), (yd, td) in sorted(self._positions.items(), key=lambda item: str(item[0]))
            if yd or td
        )
        return self._result("positions", records)

    def query_orders(self, batch: QueryBatch) -> QueryResult[OrderUpdate]:
        active = tuple(
            update
            for update in self._orders.values()
            if update.status in (OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED)
        )
        return self._result("orders", active)

    def query_trades(self, batch: QueryBatch) -> QueryResult[Trade]:
        return self._result("trades", tuple(self._trades.values()))

    def rate_limit(self) -> QueryRateLimit:
        return QueryRateLimit(interval_ms=0, max_in_flight=1)
