"""Unit tests for RollManager and two-leg state machine (S4-03, FR-CON-05~07, A10)."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from qh_trader.core.constants import Exchange, Offset, PositionSide, Side
from qh_trader.core.objects import InstrumentId, ProductId, Trade, TradeKey
from qh_trader.domain.rollover import (
    LegOrderPolicy,
    RollManager,
    RollState,
)

PROD_RB = ProductId(Exchange.SHFE, "rb")
RB2410 = InstrumentId(Exchange.SHFE, "rb2410")
RB2501 = InstrumentId(Exchange.SHFE, "rb2501")
NOW = datetime(2024, 8, 15, 1, 0, tzinfo=timezone.utc)
DAY = date(2024, 8, 15)


def make_trade(inst: InstrumentId, side: Side, offset: Offset, qty: int, price: str, tid: str) -> Trade:
    return Trade(
        account_id="acc1",
        instrument=inst,
        trading_day=DAY,
        trade_id=tid,
        side=side,
        offset=offset,
        quantity=qty,
        price=Decimal(price),
        event_time=NOW,
        available_at=NOW,
        deduplication_key=TradeKey("acc1", inst.exchange, DAY, tid),
    )


def test_roll_manager_close_first_pipeline() -> None:
    mgr = RollManager("acc1")
    # 多头持仓 2 手从 rb2410 移至 rb2501，先平后开 (CLOSE_FIRST)
    task = mgr.create_roll_task(
        product=PROD_RB,
        from_instrument=RB2410,
        to_instrument=RB2501,
        position_side=PositionSide.LONG,
        quantity=2,
        batch_size=2,
        policy=LegOrderPolicy.CLOSE_FIRST,
    )
    assert task.state == RollState.PLANNED

    # 1. 规划第一腿：应先平旧多仓 (SELL CLOSE)
    intent1 = mgr.plan_next_order(task, NOW)
    assert intent1 is not None
    assert intent1.instrument == RB2410
    assert intent1.side == Side.SELL
    assert intent1.offset == Offset.CLOSE
    assert intent1.quantity == 2
    assert task.state == RollState.LEG_1_SUBMITTED

    # 2. 第一腿全部成交
    t1 = make_trade(RB2410, Side.SELL, Offset.CLOSE, 2, "3100", "t1")
    mgr.on_trade(t1, intent1.client_order_id)
    assert task.state == RollState.LEG_1_FILLED
    assert task.leg1_filled_qty == 2

    # 3. 规划第二腿：开新多仓 (BUY OPEN)
    intent2 = mgr.plan_next_order(task, NOW)
    assert intent2 is not None
    assert intent2.instrument == RB2501
    assert intent2.side == Side.BUY
    assert intent2.offset == Offset.OPEN
    assert intent2.quantity == 2
    assert task.state == RollState.LEG_2_SUBMITTED

    # 4. 第二腿全部成交
    t2 = make_trade(RB2501, Side.BUY, Offset.OPEN, 2, "3250", "t2")
    mgr.on_trade(t2, intent2.client_order_id)
    assert task.state == RollState.COMPLETED
    assert task.leg2_filled_qty == 2
    assert task.is_done
    assert task.remaining_exposure_qty == 0


def test_roll_manager_failure_paused() -> None:
    mgr = RollManager("acc1")
    task = mgr.create_roll_task(
        product=PROD_RB,
        from_instrument=RB2410,
        to_instrument=RB2501,
        position_side=PositionSide.LONG,
        quantity=2,
        policy=LegOrderPolicy.CLOSE_FIRST,
    )
    intent = mgr.plan_next_order(task, NOW)
    assert intent is not None

    # 第一腿被柜台拒绝
    mgr.on_order_rejected_or_cancelled(intent.client_order_id, "CTP: Insufficient funds")
    assert task.state == RollState.PAUSED
    assert task.failure_reason == "CTP: Insufficient funds"
    # PAUSED 状态下不再产生下一腿订单
    next_intent = mgr.plan_next_order(task, NOW)
    assert next_intent is None
