"""S2-12 / A02 / A03 / A04 / A23: 可控网关事件驱动的领域内核组合测试.

一笔去重后的成交只更新一次订单、持仓与账本；乱序、重复、迟到成交、成交先于报单、
跨会话 OrderRef 冲突、终态后迟到成交等注入均以每步持仓、冻结与入账次数断言。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from qh_trader.core.constants import EventKind, Exchange, Offset, OrderStatus, OrderType, PositionSide, SendState, Side
from qh_trader.core.event import CanonicalEvent
from qh_trader.core.objects import InstrumentId, LocalSendResult, OrderIntent
from qh_trader.domain.ledger import AccountLedger
from qh_trader.domain.orders import OrderManager
from qh_trader.domain.positions import PositionAccountingError
from tests.fixtures.controllable_gateway import ACCOUNT, ControllableGateway

ROOT = Path(__file__).resolve().parents[2]
RB = InstrumentId(Exchange.SHFE, "rb2410")
DAY = date(2024, 9, 10)
MULT = Decimal("10")


class Kernel:
    """把 OrderManager + AccountLedger(PositionManager) 串成一个账户执行序列."""

    def __init__(self) -> None:
        self.orders = OrderManager()
        self.ledger = AccountLedger(account_id=ACCOUNT, initial_capital=Decimal("100000"), trading_day=DAY)
        self.postings = 0

    @property
    def positions(self):
        return self.ledger.position_manager

    def submit(self, intent: OrderIntent, order_ref: str, gw: ControllableGateway) -> None:
        self.orders.create_order(intent)
        self.positions.reserve_for_order(
            intent.client_order_id, intent.instrument, intent.side, intent.offset, intent.quantity
        )
        self.orders.bind_session_identity(intent.client_order_id, gw.front_id, gw.session_id, order_ref)
        self.orders.get_order(intent.client_order_id).mark_submitting()
        self.orders.record_send_result(
            intent.client_order_id, LocalSendResult(SendState.SENT_UNKNOWN, 0, "ReqOrderInsert=0")
        )

    def handle(self, event: CanonicalEvent) -> None:
        if event.kind == EventKind.ORDER_REPORT:
            order = self.orders.process_order_update(event.payload)
            if order is not None and order.is_terminal:
                self.positions.on_order_canceled_or_rejected(order.client_order_id, order.cum_filled_qty)
        elif event.kind == EventKind.TRADE_REPORT:
            order, is_new = self.orders.process_trade(event.payload)
            if is_new and order is not None:
                self.ledger.on_trade(event.payload, multiplier=MULT, client_order_id=order.client_order_id)
                self.postings += 1

    def link_pending(self) -> None:
        """订单回报到达后，把刚刚被补关联的成交入账 (一次)."""
        for item in self.orders.unlinked_trades:
            if item.resolved and not getattr(item, "_posted", False):
                self.ledger.on_trade(item.trade, multiplier=MULT, client_order_id=item.linked_client_order_id)
                item._posted = True  # type: ignore[attr-defined]
                self.postings += 1


def intent(cid: str, side: Side, offset: Offset, qty: int, price: int = 3500) -> OrderIntent:
    return OrderIntent(
        client_order_id=cid,
        account_id=ACCOUNT,
        strategy_id="s1",
        instrument=RB,
        side=side,
        offset=offset,
        quantity=qty,
        order_type=OrderType.LIMIT,
        limit_price_ticks=price,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def gw() -> ControllableGateway:
    return ControllableGateway()


@pytest.fixture
def kernel() -> Kernel:
    return Kernel()


def seed_long(kernel: Kernel, pos_td: int) -> None:
    kernel.positions.get_position(RB, PositionSide.LONG).pos_td = pos_td
    for i in range(pos_td):
        gw = ControllableGateway()
        ev = gw.trade_report(RB, Side.BUY, Offset.OPEN, 1, Decimal("3400"), identity=None, trade_id=f"seed-{i}")
        kernel.ledger.get_instrument_ledger(RB, MULT).add_open_lot(ev.payload, DAY, Decimal("0"))


# ---------------------------------------------------------------------- A02: terminal_before_fills 全链路
def test_terminal_before_fills_with_duplicate_and_shuffled_delivery(gw, kernel):
    case = next(
        c
        for c in json.loads((ROOT / "tests/fixtures/order_event_examples.json").read_text(encoding="utf-8"))["cases"]
        if c["id"] == "terminal_before_fills"
    )
    seed_long(kernel, case["inputs"]["initial"]["pos_td"])
    pos = kernel.positions.get_position(RB, PositionSide.LONG)
    kernel.submit(intent("close-1", Side.SELL, Offset.CLOSE_TODAY, 3), "1", gw)
    assert (pos.pos_td, pos.frozen_td) == (5, 3)

    ident = gw.identity(Exchange.SHFE, order_ref="1", exchange_order_id=gw.next_sys_id())
    cancel = gw.order_report(RB, Side.SELL, Offset.CLOSE_TODAY, OrderStatus.CANCELLED, 3, 2, identity=ident)
    t1 = gw.trade_report(RB, Side.SELL, Offset.CLOSE_TODAY, 1, Decimal("3500"), identity=ident, trade_id="T1")
    t2 = gw.trade_report(RB, Side.SELL, Offset.CLOSE_TODAY, 1, Decimal("3500"), identity=ident, trade_id="T2")
    dup = gw.duplicate(t1)

    expected = case["expected"]["states_after_events"]
    kernel.handle(cancel)
    assert (pos.pos_td, pos.frozen_td) == (expected[0]["pos_td"], expected[0]["frozen_td"])
    kernel.handle(t1)
    assert (pos.pos_td, pos.frozen_td) == (expected[1]["pos_td"], expected[1]["frozen_td"])
    kernel.handle(t2)
    assert (pos.pos_td, pos.frozen_td) == (expected[2]["pos_td"], expected[2]["frozen_td"])
    kernel.handle(dup)
    assert (pos.pos_td, pos.frozen_td) == (expected[3]["pos_td"], expected[3]["frozen_td"])
    order = kernel.orders.get_order("close-1")
    assert order.status == OrderStatus.CANCELLED
    assert order.accounted_filled_qty == 2
    assert kernel.postings == case["expected"]["unique_trade_postings"]
    assert kernel.orders.deduplicator.seen_count == 2
    kernel.positions.verify_frozen_invariant()

    # 同一脚本乱序投递 (成交先于撤单终态) 得到相同最终状态
    gw2, k2 = ControllableGateway(), Kernel()
    seed_long(k2, 5)
    k2.submit(intent("close-1", Side.SELL, Offset.CLOSE_TODAY, 3), "1", gw2)
    ident2 = gw2.identity(Exchange.SHFE, order_ref="1", exchange_order_id=gw2.next_sys_id())
    events = [
        gw2.order_report(RB, Side.SELL, Offset.CLOSE_TODAY, OrderStatus.CANCELLED, 3, 2, identity=ident2),
        gw2.trade_report(RB, Side.SELL, Offset.CLOSE_TODAY, 1, Decimal("3500"), identity=ident2, trade_id="T1"),
        gw2.trade_report(RB, Side.SELL, Offset.CLOSE_TODAY, 1, Decimal("3500"), identity=ident2, trade_id="T2"),
    ]
    events.append(gw2.duplicate(events[1]))
    for ev in ControllableGateway.reversed_order(events):
        k2.handle(ev)
    p2 = k2.positions.get_position(RB, PositionSide.LONG)
    assert (p2.pos_td, p2.frozen_td) == (3, 0)
    assert k2.postings == 2
    assert k2.orders.get_order("close-1").status == OrderStatus.CANCELLED
    k2.positions.verify_frozen_invariant()


# ---------------------------------------------------------------------- A02: 成交先于报单，占位关联
def test_trade_before_order_report_links_only_by_remote_identity(gw, kernel):
    kernel.submit(intent("open-1", Side.BUY, Offset.OPEN, 2), "7", gw)
    ident = gw.identity(Exchange.SHFE, order_ref="7", exchange_order_id="SYS-A")
    # 成交只带 ExchangeID+OrderSysID，没有原会话三元组，本地还没建立 SYS-A 映射
    trade_ident = gw.identity(Exchange.SHFE, exchange_order_id="SYS-A")
    early = gw.trade_report(RB, Side.BUY, Offset.OPEN, 2, Decimal("3500"), identity=trade_ident)
    kernel.handle(early)
    assert kernel.orders.pending_unlinked_trades()
    assert kernel.orders.get_order("open-1").accounted_filled_qty == 0
    assert kernel.postings == 0
    assert kernel.orders.has_open_reconciliation

    kernel.handle(gw.order_report(RB, Side.BUY, Offset.OPEN, OrderStatus.FILLED, 2, 2, identity=ident))
    kernel.link_pending()
    order = kernel.orders.get_order("open-1")
    assert order.accounted_filled_qty == 2
    assert order.status == OrderStatus.FILLED
    assert kernel.positions.get_position(RB, PositionSide.LONG).pos_td == 2
    assert kernel.postings == 1
    assert not kernel.orders.pending_unlinked_trades()
    # 迟到的重复成交与重复报单不再次入账
    kernel.handle(gw.duplicate(early))
    kernel.handle(gw.order_report(RB, Side.BUY, Offset.OPEN, OrderStatus.PARTIALLY_FILLED, 2, 1, identity=ident))
    assert kernel.postings == 1
    assert order.status == OrderStatus.FILLED


def test_trade_without_identity_is_never_guessed_onto_an_order(gw, kernel):
    kernel.submit(intent("open-1", Side.BUY, Offset.OPEN, 2), "7", gw)
    orphan = gw.trade_report(RB, Side.BUY, Offset.OPEN, 2, Decimal("3500"), identity=None)
    kernel.handle(orphan)
    assert kernel.orders.get_order("open-1").accounted_filled_qty == 0
    assert kernel.postings == 0
    assert len(kernel.orders.pending_unlinked_trades()) == 1
    # 人工 / 恢复协调器确认归属后一次性入账
    kernel.orders.resolve_unlinked_trade(orphan.payload.trade_id, "open-1", "confirmed by broker statement")
    assert kernel.orders.get_order("open-1").accounted_filled_qty == 2


# ---------------------------------------------------------------------- A02: 跨会话 OrderRef 冲突
def test_cross_session_order_ref_conflict_does_not_misbind(gw, kernel):
    kernel.submit(intent("old-1", Side.BUY, Offset.OPEN, 1), "1", gw)
    old_session = gw.session_id
    gw.new_session()
    kernel.submit(intent("new-1", Side.BUY, Offset.OPEN, 1), "1", gw)

    new_ident = gw.identity(Exchange.SHFE, order_ref="1")
    old_ident = gw.identity(Exchange.SHFE, order_ref="1", session_id=old_session)
    kernel.handle(gw.trade_report(RB, Side.BUY, Offset.OPEN, 1, Decimal("3500"), identity=new_ident))
    assert kernel.orders.get_order("new-1").accounted_filled_qty == 1
    assert kernel.orders.get_order("old-1").accounted_filled_qty == 0
    kernel.handle(gw.trade_report(RB, Side.BUY, Offset.OPEN, 1, Decimal("3500"), identity=old_ident))
    assert kernel.orders.get_order("old-1").accounted_filled_qty == 1

    # 只带 OrderRef 的回报无法构造合法标识；带未知会话号的回报进入外部占位而不是被丢弃或猜配
    gw.new_session()
    foreign = gw.identity(Exchange.SHFE, order_ref="1")
    assert (
        kernel.orders.process_order_update(
            gw.order_report(RB, Side.BUY, Offset.OPEN, OrderStatus.ACCEPTED, 1, 0, identity=foreign).payload
        )
        is None
    )
    assert len(kernel.orders.external_orders) == 1
    assert kernel.orders.has_open_reconciliation


# ---------------------------------------------------------------------- FR-ORD-03: 未预占成交不侵占他人冻结
def test_unreserved_fill_cannot_consume_other_orders_freeze(gw, kernel):
    seed_long(kernel, 5)
    kernel.submit(intent("x", Side.SELL, Offset.CLOSE_TODAY, 3), "1", gw)
    pos = kernel.positions.get_position(RB, PositionSide.LONG)
    assert pos.frozen_td == 3
    stray = gw.trade_report(RB, Side.SELL, Offset.CLOSE_TODAY, 2, Decimal("3500"), identity=None)
    # 可用今仓只有 2，未预占成交 2 手可以落在可用份额上，冻结不变
    kernel.positions.apply_trade(stray.payload)
    assert (pos.pos_td, pos.frozen_td) == (3, 3)
    assert kernel.positions.get_reservation("x").frozen_td == 3
    with pytest.raises(PositionAccountingError, match="other orders' reservations"):
        kernel.positions.apply_trade(
            gw.trade_report(RB, Side.SELL, Offset.CLOSE_TODAY, 1, Decimal("3500"), identity=None, trade_id="S2").payload
        )
    kernel.positions.verify_frozen_invariant()


# ---------------------------------------------------------------------- A03: 发送三态与预占
def test_send_state_semantics_and_reservation_release(gw, kernel):
    seed_long(kernel, 2)
    pos = kernel.positions.get_position(RB, PositionSide.LONG)
    # 明确未发送：释放预占
    kernel.orders.create_order(intent("ns", Side.SELL, Offset.CLOSE_TODAY, 1))
    kernel.positions.reserve_for_order("ns", RB, Side.SELL, Offset.CLOSE_TODAY, 1)
    kernel.ledger.reserve_funds("ns", Decimal("100"), Decimal("1"))
    kernel.orders.get_order("ns").mark_submitting()
    kernel.orders.record_send_result("ns", LocalSendResult(SendState.NOT_SENT, -1, "adapter proved no network call"))
    order = kernel.orders.get_order("ns")
    assert order.status == OrderStatus.REJECTED and order.send_state == SendState.NOT_SENT
    kernel.positions.release_reservation("ns")
    kernel.ledger.release_funds("ns")
    assert pos.frozen_td == 0 and kernel.ledger.frozen_margin == Decimal("0")

    # 未知发送：30 秒超时与空查询均不释放、不重发
    kernel.submit(intent("uk", Side.SELL, Offset.CLOSE_TODAY, 1), "9", gw)
    kernel.ledger.reserve_funds("uk", Decimal("100"), Decimal("1"))
    uk = kernel.orders.get_order("uk")
    assert uk.send_state == SendState.SENT_UNKNOWN and uk.reconciliation_required
    assert pos.frozen_td == 1 and kernel.ledger.frozen_margin == Decimal("100")
    # 迟到的本地 NOT_SENT 结果不能把已远端确认的订单拉回
    kernel.handle(
        gw.order_report(
            RB,
            Side.SELL,
            Offset.CLOSE_TODAY,
            OrderStatus.ACCEPTED,
            1,
            0,
            identity=gw.identity(Exchange.SHFE, order_ref="9"),
        )
    )
    assert uk.send_state == SendState.CONFIRMED_REMOTE and not uk.reconciliation_required
    kernel.orders.record_send_result("uk", LocalSendResult(SendState.NOT_SENT, -1, "late local failure"))
    assert uk.send_state == SendState.CONFIRMED_REMOTE
    assert uk.reconciliation_required


def test_reconciliation_flag_survives_later_reports(gw, kernel):
    kernel.submit(intent("o", Side.BUY, Offset.OPEN, 1), "3", gw)
    ident = gw.identity(Exchange.SHFE, order_ref="3")
    kernel.handle(gw.trade_report(RB, Side.BUY, Offset.OPEN, 1, Decimal("3500"), identity=ident, trade_id="a"))
    kernel.handle(gw.trade_report(RB, Side.BUY, Offset.OPEN, 1, Decimal("3500"), identity=ident, trade_id="b"))
    order = kernel.orders.get_order("o")
    assert order.reconciliation_required and "exceeds order quantity" in order.reconciliation_reason
    kernel.handle(gw.order_report(RB, Side.BUY, Offset.OPEN, OrderStatus.FILLED, 1, 1, identity=ident))
    assert order.reconciliation_required


# ---------------------------------------------------------------------- FR-CAL-06: 跨日后迟到的上一交易日成交
def test_late_previous_day_close_today_fill_after_rollover(gw, kernel):
    seed_long(kernel, 2)
    kernel.submit(intent("c", Side.SELL, Offset.CLOSE_TODAY, 2), "5", gw)
    kernel.ledger.settle_day({RB: Decimal("3450")}, date(2024, 9, 11))
    pos = kernel.positions.get_position(RB, PositionSide.LONG)
    assert (pos.pos_yd, pos.pos_td, pos.frozen_yd, pos.frozen_td) == (2, 0, 2, 0)
    late = gw.trade_report(
        RB,
        Side.SELL,
        Offset.CLOSE_TODAY,
        1,
        Decimal("3500"),
        identity=gw.identity(Exchange.SHFE, order_ref="5"),
        trading_day=DAY,
    )
    kernel.handle(late)
    assert (pos.pos_yd, pos.pos_td, pos.frozen_yd, pos.frozen_td) == (1, 0, 1, 0)
    record = kernel.ledger.get_instrument_ledger(RB).closed_records[-1]
    # 已按 3450 结算过的批次，盯市基准是结算价
    assert record.mtm_close_pnl == (Decimal("3500") - Decimal("3450")) * MULT
    assert record.trade_close_pnl == (Decimal("3500") - Decimal("3400")) * MULT
    kernel.positions.verify_frozen_invariant()
    # 同一交易日重复跨日任务不再转换
    assert kernel.positions.advance_trading_day(date(2024, 9, 11)) is False


# ---------------------------------------------------------------------- A23: 旧代次成交是事实
def test_old_epoch_trade_is_fact_but_command_rejected(gw, kernel):
    from qh_trader.domain.risk import EpochViolationError, RiskManager, RiskState

    case = next(
        c
        for c in json.loads((ROOT / "tests/fixtures/order_event_examples.json").read_text(encoding="utf-8"))["cases"]
        if c["id"] == "old_epoch_trade_is_fact"
    )
    risk = RiskManager(account_id=ACCOUNT, initial_epoch=case["inputs"]["order_epoch"])
    kernel.submit(intent("old", Side.BUY, Offset.OPEN, 1), "1", gw)
    kernel.orders.get_order("old").command_epoch = case["inputs"]["order_epoch"]
    risk.advance_epoch(case["inputs"]["current_epoch"])
    risk.escalate("fixture", target=RiskState.HALTED)

    ident = gw.identity(Exchange.SHFE, order_ref="1")
    t = gw.trade_report(RB, Side.BUY, Offset.OPEN, 1, Decimal("3500"), identity=ident, trade_id="T3")
    kernel.handle(t)
    kernel.handle(gw.duplicate(t))
    assert kernel.positions.get_position(RB, PositionSide.LONG).total_position == case["expected"]["final_position"]
    assert kernel.postings == case["expected"]["unique_trade_postings"]
    with pytest.raises(EpochViolationError):
        risk.check_command_epoch(case["inputs"]["events"][2]["command_epoch"])
    assert risk.risk_state == RiskState.HALTED
