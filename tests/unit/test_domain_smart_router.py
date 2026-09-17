"""Unit tests for SmartRouter and order planning (S2-04, A01)."""

from datetime import datetime, timezone

import pytest

from qh_trader.core.constants import Exchange, Offset, OrderType, PositionSide, Side
from qh_trader.core.objects import InstrumentId, OrderIntent
from qh_trader.domain.positions import PositionDetail
from qh_trader.domain.smart_router import ClosePriority, SmartRouter


@pytest.fixture
def sample_inst():
    return InstrumentId(Exchange.SHFE, "rb2410")


def make_close_intent(inst: InstrumentId, qty: int) -> OrderIntent:
    return OrderIntent(
        client_order_id="parent-001",
        account_id="test-acc",
        strategy_id="strat-1",
        instrument=inst,
        side=Side.SELL,
        offset=Offset.CLOSE_TODAY,  # 初始意图
        quantity=qty,
        order_type=OrderType.LIMIT,
        limit_price_ticks=3500,
        created_at=datetime.now(timezone.utc),
    )


def test_smart_router_auto_close_yesterday_first_split(sample_inst):
    """A01: 测试 Offset.CLOSE 在平昨优先下的智能拆单 (昨仓 2 手，今仓 5 手，平 3 手 -> 平昨 2 手 + 平今 1 手)."""
    router = SmartRouter(default_priority=ClosePriority.YESTERDAY_FIRST)
    pos = PositionDetail(instrument=sample_inst, side=PositionSide.LONG, pos_yd=2, pos_td=5)

    counter = 0

    def gen_id():
        nonlocal counter
        counter += 1
        return f"child-{counter}"

    intent = OrderIntent(
        client_order_id="parent-auto-close",
        account_id="test-acc",
        strategy_id="strat-1",
        instrument=sample_inst,
        side=Side.SELL,
        offset=Offset.CLOSE,
        quantity=3,
        order_type=OrderType.LIMIT,
        limit_price_ticks=3500,
        created_at=datetime.now(timezone.utc),
    )
    plans = router.plan_order(intent, pos, gen_id)
    assert len(plans) == 2
    # 子单 1: 平昨 2 手
    assert plans[0].offset == Offset.CLOSE_YESTERDAY
    assert plans[0].quantity == 2
    assert plans[0].parent_order_id == "parent-auto-close"
    assert plans[0].client_order_id == "child-1"

    # 子单 2: 平今 1 手
    assert plans[1].offset == Offset.CLOSE_TODAY
    assert plans[1].quantity == 1
    assert plans[1].parent_order_id == "parent-auto-close"
    assert plans[1].client_order_id == "child-2"


def test_smart_router_auto_close_today_first_split(sample_inst):
    """A01: 测试 Offset.CLOSE 在平今优先下的智能拆单 (昨仓 2 手，今仓 5 手，平 3 手 -> 平今 3 手)."""
    router = SmartRouter(default_priority=ClosePriority.TODAY_FIRST)
    pos = PositionDetail(instrument=sample_inst, side=PositionSide.LONG, pos_yd=2, pos_td=5)

    counter = 0

    def gen_id():
        nonlocal counter
        counter += 1
        return f"child-{counter}"

    intent = OrderIntent(
        client_order_id="parent-auto-close",
        account_id="test-acc",
        strategy_id="strat-1",
        instrument=sample_inst,
        side=Side.SELL,
        offset=Offset.CLOSE,
        quantity=3,
        order_type=OrderType.LIMIT,
        limit_price_ticks=3500,
        created_at=datetime.now(timezone.utc),
    )
    plans = router.plan_order(intent, pos, gen_id)
    assert len(plans) == 1
    # 子单 1: 平今 3 手
    assert plans[0].offset == Offset.CLOSE_TODAY
    assert plans[0].quantity == 3
    assert plans[0].parent_order_id == "parent-auto-close"


def test_smart_router_yesterday_first_split(sample_inst):
    router = SmartRouter(default_priority=ClosePriority.YESTERDAY_FIRST)
    # 持仓: 昨仓 2 手, 今仓 5 手
    pos = PositionDetail(instrument=sample_inst, side=PositionSide.LONG, pos_yd=2, pos_td=5)

    counter = 0

    def gen_id():
        nonlocal counter
        counter += 1
        return f"child-{counter}"

    # 意图: 平多仓 3 手
    intent = OrderIntent(
        client_order_id="parent-close",
        account_id="test-acc",
        strategy_id="strat-1",
        instrument=sample_inst,
        side=Side.SELL,
        offset=Offset.CLOSE_TODAY,  # 外部通用平仓意图
        quantity=3,
        order_type=OrderType.LIMIT,
        limit_price_ticks=3500,
        created_at=datetime.now(timezone.utc),
    )
    # 若直接指定了 CLOSE_TODAY，且可用今仓足够(5>=3)，直接透传
    plans = router.plan_order(intent, pos, gen_id)
    assert len(plans) == 1
    assert plans[0].offset == Offset.CLOSE_TODAY
    assert plans[0].quantity == 3


def test_smart_router_insufficient_position_raises(sample_inst):
    router = SmartRouter()
    pos = PositionDetail(instrument=sample_inst, side=PositionSide.LONG, pos_yd=1, pos_td=1)

    def gen_id():
        return "c-1"

    # 试图平今 2 手，但今仓只有 1 手
    intent = OrderIntent(
        client_order_id="parent-1",
        account_id="test-acc",
        strategy_id="strat-1",
        instrument=sample_inst,
        side=Side.SELL,
        offset=Offset.CLOSE_TODAY,
        quantity=2,
        order_type=OrderType.LIMIT,
        limit_price_ticks=3500,
        created_at=datetime.now(timezone.utc),
    )
    with pytest.raises(ValueError, match="insufficient available today position"):
        router.plan_order(intent, pos, gen_id)
