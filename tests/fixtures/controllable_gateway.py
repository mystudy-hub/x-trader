"""可控网关事件夹具 (S2-12, FR-REC-05).

模拟柜台回报流，用于注入乱序、重复、迟到成交、成交先于报单、跨会话 OrderRef 冲突、
查询限流 / 不完整等故障。夹具只生成规范事件与查询结果，不做任何记账；记账由领域内核完成。
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from qh_trader.core.constants import EventKind, Exchange, Offset, OrderStatus, Side
from qh_trader.core.event import CanonicalEvent
from qh_trader.core.objects import (
    InstrumentId,
    OrderIdentity,
    OrderUpdate,
    Position,
    QueryBatch,
    QueryResult,
    Trade,
    TradeKey,
)

ACCOUNT = "test-account"
BASE_TIME = datetime(2024, 9, 10, 1, 0, tzinfo=timezone.utc)


@dataclass
class ControllableGateway:
    """按脚本产生柜台回报的可控网关桩."""

    account_id: str = ACCOUNT
    trading_day: date = date(2024, 9, 10)
    front_id: int = 1
    session_id: int = 100
    source_id: str = "controllable-gateway"
    _seq: int = 0
    _clock: datetime = BASE_TIME
    _events: list[CanonicalEvent] = field(default_factory=list)
    _trade_counter: int = 0
    _sys_counter: int = 0

    # ------------------------------------------------------------------ 基础
    def _next_time(self) -> datetime:
        self._clock += timedelta(milliseconds=1)
        return self._clock

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def new_session(self) -> None:
        """模拟重新登录：session_id 变化，OrderRef 可能重复."""
        self.session_id += 1

    def identity(
        self,
        exchange: Exchange,
        client_order_id: str | None = None,
        order_ref: str | None = None,
        exchange_order_id: str | None = None,
        session_id: int | None = None,
    ) -> OrderIdentity:
        if order_ref is not None:
            return OrderIdentity(
                account_id=self.account_id,
                exchange=exchange,
                client_order_id=client_order_id,
                exchange_order_id=exchange_order_id,
                front_id=self.front_id,
                session_id=self.session_id if session_id is None else session_id,
                order_ref=order_ref,
            )
        return OrderIdentity(
            account_id=self.account_id,
            exchange=exchange,
            client_order_id=client_order_id,
            exchange_order_id=exchange_order_id,
        )

    def next_sys_id(self) -> str:
        self._sys_counter += 1
        return f"SYS-{self._sys_counter}"

    # ------------------------------------------------------------------ 回报生成
    def order_report(
        self,
        instrument: InstrumentId,
        side: Side,
        offset: Offset,
        status: OrderStatus,
        quantity: int,
        filled_quantity: int,
        *,
        identity: OrderIdentity,
        event_time: datetime | None = None,
    ) -> CanonicalEvent:
        when = event_time or self._next_time()
        update = OrderUpdate(
            identity=identity,
            instrument=instrument,
            side=side,
            offset=offset,
            status=status,
            quantity=quantity,
            filled_quantity=filled_quantity,
            event_time=when,
            available_at=when,
        )
        event = CanonicalEvent(
            event_id=f"ord-{self._next_seq()}",
            kind=EventKind.ORDER_REPORT,
            event_time=when,
            available_at=when,
            sequence=self._seq,
            source_id=self.source_id,
            payload=update,
        )
        self._events.append(event)
        return event

    def trade_report(
        self,
        instrument: InstrumentId,
        side: Side,
        offset: Offset,
        quantity: int,
        price: Decimal,
        *,
        identity: OrderIdentity | None,
        trade_id: str | None = None,
        trading_day: date | None = None,
        event_time: datetime | None = None,
        extra_scope: tuple[str, ...] = (),
    ) -> CanonicalEvent:
        when = event_time or self._next_time()
        if trade_id is None:
            self._trade_counter += 1
            trade_id = f"T{self._trade_counter}"
        day = trading_day or self.trading_day
        trade = Trade(
            account_id=self.account_id,
            instrument=instrument,
            trading_day=day,
            trade_id=trade_id,
            side=side,
            offset=offset,
            quantity=quantity,
            price=price,
            event_time=when,
            available_at=when,
            deduplication_key=TradeKey(self.account_id, instrument.exchange, day, trade_id, extra_scope),
            order_identity=identity,
        )
        event = CanonicalEvent(
            event_id=f"trd-{self._next_seq()}",
            kind=EventKind.TRADE_REPORT,
            event_time=when,
            available_at=when,
            sequence=self._seq,
            source_id=self.source_id,
            payload=trade,
        )
        self._events.append(event)
        return event

    def duplicate(self, event: CanonicalEvent) -> CanonicalEvent:
        """同一事实通过另一条回调再次到达 (新序号，同样载荷)."""
        when = self._next_time()
        payload = event.payload
        if isinstance(payload, Trade):
            payload = Trade(
                account_id=payload.account_id,
                instrument=payload.instrument,
                trading_day=payload.trading_day,
                trade_id=payload.trade_id,
                side=payload.side,
                offset=payload.offset,
                quantity=payload.quantity,
                price=payload.price,
                event_time=when,
                available_at=when,
                deduplication_key=payload.deduplication_key,
                order_identity=payload.order_identity,
            )
        else:
            payload = OrderUpdate(
                identity=payload.identity,
                instrument=payload.instrument,
                side=payload.side,
                offset=payload.offset,
                status=payload.status,
                quantity=payload.quantity,
                filled_quantity=payload.filled_quantity,
                event_time=when,
                available_at=when,
            )
        dup = CanonicalEvent(
            event_id=f"dup-{self._next_seq()}",
            kind=event.kind,
            event_time=when,
            available_at=when,
            sequence=self._seq,
            source_id=self.source_id,
            payload=payload,
        )
        self._events.append(dup)
        return dup

    # ------------------------------------------------------------------ 故障注入
    @staticmethod
    def shuffled(events: Iterable[CanonicalEvent], seed: int = 7) -> list[CanonicalEvent]:
        """乱序投递 (保持事件本身不变)."""
        items = list(events)
        random.Random(seed).shuffle(items)
        return items

    @staticmethod
    def reversed_order(events: Iterable[CanonicalEvent]) -> list[CanonicalEvent]:
        return list(reversed(list(events)))

    def delivered(self) -> Iterator[CanonicalEvent]:
        return iter(list(self._events))

    # ------------------------------------------------------------------ 查询
    def query_batch(self, batch_id: str, trading_day: date | None = None) -> QueryBatch:
        return QueryBatch(batch_id, self.account_id, trading_day or self.trading_day, self._next_time())

    def query_result(
        self,
        batch: QueryBatch,
        records: Iterable,
        *,
        complete: bool = True,
        error_code: int | None = None,
    ) -> QueryResult:
        return QueryResult(
            batch=batch,
            records=tuple(records),
            available_at=self._next_time(),
            source_id=self.source_id,
            source_version="stub-1",
            complete=complete,
            error_code=error_code,
        )

    def rate_limited(self, batch: QueryBatch, records: Iterable) -> QueryResult:
        """限流：返回部分记录且 complete=False."""
        items = list(records)
        return self.query_result(batch, items[: max(0, len(items) - 1)], complete=False, error_code=-3)

    @staticmethod
    def position(
        instrument: InstrumentId,
        side,
        pos_yd: int = 0,
        pos_td: int = 0,
        frozen_yd: int = 0,
        frozen_td: int = 0,
        hedge_flag: str = "SPECULATION",
    ) -> Position:
        return Position(
            instrument=instrument,
            side=side,
            hedge_flag=hedge_flag,
            pos_yd=pos_yd,
            pos_td=pos_td,
            frozen_yd=frozen_yd,
            frozen_td=frozen_td,
        )
