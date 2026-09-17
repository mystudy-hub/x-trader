"""Unit tests for Order state machine, send_state, trade deduplication and unlinked trades (S2-01)."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

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
from qh_trader.domain.orders import Order, OrderManager, TradeDeduplicator


@pytest.fixture
def sample_inst():
    return InstrumentId(Exchange.SHFE, "rb2410")


@pytest.fixture
def sample_intent(sample_inst):
    return OrderIntent(
        client_order_id="ord-001",
        account_id="acc-test",
        strategy_id="strat-test",
        instrument=sample_inst,
        side=Side.BUY,
        offset=Offset.OPEN,
        quantity=5,
        order_type=OrderType.LIMIT,
        limit_price_ticks=3500,
        created_at=datetime.now(timezone.utc),
    )


def test_order_creation_and_initial_state(sample_intent):
    order = Order(intent=sample_intent)
    assert order.client_order_id == "ord-001"
    assert order.status == OrderStatus.CREATED
    assert order.send_state == SendState.NOT_SENT
    assert order.leaves_qty == 5
    assert order.cum_filled_qty == 0
    assert order.accounted_filled_qty == 0
    assert not order.is_terminal
    assert order.is_active


def test_order_submitting_and_send_result_not_sent(sample_intent):
    order = Order(intent=sample_intent)
    order.mark_submitting()
    assert order.status == OrderStatus.SUBMITTING

    result = LocalSendResult(state=SendState.NOT_SENT, local_code=-1, evidence="validation failure")
    order.apply_send_result(result)
    assert order.send_state == SendState.NOT_SENT
    assert order.status == OrderStatus.REJECTED
    assert order.is_terminal


def test_order_send_result_sent_unknown_requires_reconciliation(sample_intent):
    order = Order(intent=sample_intent)
    order.mark_submitting()

    result = LocalSendResult(state=SendState.SENT_UNKNOWN, local_code=0, evidence="network timeout 30s")
    order.apply_send_result(result)
    assert order.send_state == SendState.SENT_UNKNOWN
    assert order.reconciliation_required is True
    # 状态仍保持 SUBMITTING，等待核对，不盲目 REJECT，也不自作主张 FILLED
    assert order.status == OrderStatus.SUBMITTING


def test_order_state_machine_progress_and_no_rewind(sample_intent, sample_inst):
    order = Order(intent=sample_intent)
    order.mark_submitting()

    ident = OrderIdentity(
        account_id="acc-test",
        exchange=Exchange.SHFE,
        client_order_id="ord-001",
        exchange_order_id="ex-12345",
        front_id=1,
        session_id=101,
        order_ref="ref-001",
    )
    # 1. 柜台接受 ACCEPTED
    u1 = OrderUpdate(
        identity=ident,
        instrument=sample_inst,
        side=Side.BUY,
        offset=Offset.OPEN,
        status=OrderStatus.ACCEPTED,
        quantity=5,
        filled_quantity=0,
        event_time=datetime.now(timezone.utc),
        available_at=datetime.now(timezone.utc),
    )
    order.apply_order_update(u1)
    assert order.status == OrderStatus.ACCEPTED
    assert order.send_state == SendState.CONFIRMED_REMOTE
    assert order.identity.exchange_order_id == "ex-12345"

    # 2. 部分成交 PARTIALLY_FILLED
    u2 = OrderUpdate(
        identity=ident,
        instrument=sample_inst,
        side=Side.BUY,
        offset=Offset.OPEN,
        status=OrderStatus.PARTIALLY_FILLED,
        quantity=5,
        filled_quantity=2,
        event_time=datetime.now(timezone.utc),
        available_at=datetime.now(timezone.utc),
    )
    order.apply_order_update(u2)
    assert order.status == OrderStatus.PARTIALLY_FILLED
    assert order.cum_filled_qty == 2
    assert order.leaves_qty == 3

    # 3. 撤单 CANCELLED 进入终态
    u3 = OrderUpdate(
        identity=ident,
        instrument=sample_inst,
        side=Side.BUY,
        offset=Offset.OPEN,
        status=OrderStatus.CANCELLED,
        quantity=5,
        filled_quantity=2,
        event_time=datetime.now(timezone.utc),
        available_at=datetime.now(timezone.utc),
    )
    order.apply_order_update(u3)
    assert order.status == OrderStatus.CANCELLED
    assert order.is_terminal

    # 4. 迟到的 ACCEPTED 或 PARTIALLY_FILLED 回报到达 -> 防倒退规则生效，保持 CANCELLED
    order.apply_order_update(u1)
    assert order.status == OrderStatus.CANCELLED
    order.apply_order_update(u2)
    assert order.status == OrderStatus.CANCELLED


def make_trade(
    account_id: str,
    instrument: InstrumentId,
    trading_day: date,
    trade_id: str,
    side: Side,
    offset: Offset,
    quantity: int,
    price: Decimal = Decimal("3500"),
) -> Trade:
    key = TradeKey(account_id, instrument.exchange, trading_day, trade_id)
    return Trade(
        account_id=account_id,
        instrument=instrument,
        trading_day=trading_day,
        trade_id=trade_id,
        side=side,
        offset=offset,
        quantity=quantity,
        price=price,
        event_time=datetime.now(timezone.utc),
        available_at=datetime.now(timezone.utc),
        deduplication_key=key,
    )


def test_late_trade_arrival_after_cancellation(sample_intent, sample_inst):
    """A02: 订单已处于 CANCELLED 终态，但收到迟到的真实成交仍须入账."""
    order = Order(intent=sample_intent)
    ident = OrderIdentity(account_id="acc-test", exchange=Exchange.SHFE, client_order_id="ord-001")
    # 撤单完成，回报声明成交 2 手
    order.apply_order_update(
        OrderUpdate(
            identity=ident,
            instrument=sample_inst,
            side=Side.BUY,
            offset=Offset.OPEN,
            status=OrderStatus.CANCELLED,
            quantity=5,
            filled_quantity=2,
            event_time=datetime.now(timezone.utc),
            available_at=datetime.now(timezone.utc),
        )
    )
    assert order.status == OrderStatus.CANCELLED
    assert order.accounted_filled_qty == 0
    assert order.unaccounted_fill_qty == 2

    # 迟到真实成交到达
    trade1 = make_trade(
        account_id="acc-test",
        instrument=sample_inst,
        trading_day=date(2024, 9, 10),
        trade_id="TR-001",
        side=Side.BUY,
        offset=Offset.OPEN,
        quantity=1,
    )
    order.apply_trade(trade1)
    assert order.accounted_filled_qty == 1
    assert order.unaccounted_fill_qty == 1
    # 状态仍然保持 CANCELLED，不倒退
    assert order.status == OrderStatus.CANCELLED


def test_trade_deduplicator(sample_inst):
    dedup = TradeDeduplicator()
    trade = make_trade(
        account_id="acc-test",
        instrument=sample_inst,
        trading_day=date(2024, 9, 10),
        trade_id="TR-UNIQUE-1",
        side=Side.BUY,
        offset=Offset.OPEN,
        quantity=2,
    )
    assert dedup.record(trade) is True
    assert dedup.is_duplicate(trade) is True
    assert dedup.record(trade) is False
    assert dedup.seen_count == 1

    # TradeKey 也支持
    tk = TradeKey("acc-test", Exchange.SHFE, date(2024, 9, 10), "TR-UNIQUE-2")
    assert dedup.record(tk) is True
    assert dedup.record(tk) is False
    assert dedup.seen_count == 2


def test_order_manager_unlinked_trade_resolution(sample_intent, sample_inst):
    """FR-ORD-05: 成交先于报单到达，暂存为待关联，报单到达后自动匹配."""
    mgr = OrderManager()

    # 1. 真实成交先到达，当前没有任何本地订单
    early_trade = make_trade(
        account_id="acc-test",
        instrument=sample_inst,
        trading_day=date(2024, 9, 10),
        trade_id="TR-EARLY-1",
        side=Side.BUY,
        offset=Offset.OPEN,
        quantity=2,
    )
    matched, is_new = mgr.process_trade(early_trade)
    assert matched is None
    assert is_new is True
    assert len(mgr.unlinked_trades) == 1
    assert mgr.unlinked_trades[0].trade.trade_id == "TR-EARLY-1"

    # 2. 本地创建订单
    order = mgr.create_order(sample_intent)
    assert order.accounted_filled_qty == 0

    # 3. 订单回报到达，触发自动补关联
    ident = OrderIdentity(
        account_id="acc-test",
        exchange=Exchange.SHFE,
        client_order_id="ord-001",
        exchange_order_id="ex-999",
    )
    mgr.process_order_update(
        OrderUpdate(
            identity=ident,
            instrument=sample_inst,
            side=Side.BUY,
            offset=Offset.OPEN,
            status=OrderStatus.PARTIALLY_FILLED,
            quantity=5,
            filled_quantity=2,
            event_time=datetime.now(timezone.utc),
            available_at=datetime.now(timezone.utc),
        )
    )
    # 补关联完成，订单的已入账成交数量变为 2
    assert order.accounted_filled_qty == 2
    assert mgr.unlinked_trades[0].resolved is True
    assert mgr.unlinked_trades[0].linked_client_order_id == "ord-001"


def test_unknown_send_timeout_fixture_exact_behavior(sample_intent):
    """验证 unknown_send_timeout 规范:

    本地返回 0 但未收到远端确认(超时/断线/空查询):
    send_state=SENT_UNKNOWN, 保留预占, auto_resubmit=False, pending_reconciliation=True.
    """
    order = Order(intent=sample_intent)
    order.mark_submitting()

    # 模拟断线/超时后适配器返回 SENT_UNKNOWN
    res = LocalSendResult(state=SendState.SENT_UNKNOWN, local_code=0, evidence="timeout 30s after disconnect")
    order.apply_send_result(res)

    assert order.send_state == SendState.SENT_UNKNOWN
    assert order.reconciliation_required is True
    # 状态绝不被当成 REJECTED，也绝不自动重发
    assert order.status == OrderStatus.SUBMITTING


def test_proven_not_sent_fixture_exact_behavior(sample_intent):
    """验证 proven_not_sent 规范:

    明确未送出网络(前置校验失败):
    send_state=NOT_SENT, status=REJECTED, 释放预占, auto_resubmit=False.
    """
    order = Order(intent=sample_intent)
    order.mark_submitting()

    res = LocalSendResult(state=SendState.NOT_SENT, local_code=-1, evidence="rejected by local validation")
    order.apply_send_result(res)

    assert order.send_state == SendState.NOT_SENT
    assert order.status == OrderStatus.REJECTED
    assert order.is_terminal is True


def test_old_epoch_trade_is_fact_behavior(sample_inst):
    """验证 old_epoch_trade_is_fact 规范:

    旧代次的命令被拒，但旧代次发出的真实成交必须作为客观事实入账，绝不可丢弃.
    """
    mgr = OrderManager()
    old_intent = OrderIntent(
        client_order_id="old-ord-1",
        account_id="acc-test",
        strategy_id="strat-1",
        instrument=sample_inst,
        side=Side.BUY,
        offset=Offset.OPEN,
        quantity=1,
        order_type=OrderType.LIMIT,
        limit_price_ticks=3500,
        created_at=datetime.now(timezone.utc),
    )
    mgr.create_order(old_intent)

    # 真实成交来自旧代次
    trade = make_trade(
        account_id="acc-test",
        instrument=sample_inst,
        trading_day=date(2024, 9, 10),
        trade_id="TR-OLD-EPOCH",
        side=Side.BUY,
        offset=Offset.OPEN,
        quantity=1,
    )
    matched, is_new = mgr.process_trade(trade, target_client_order_id="old-ord-1")
    assert is_new is True
    assert matched is not None
    assert matched.accounted_filled_qty == 1
    assert matched.status == OrderStatus.FILLED

