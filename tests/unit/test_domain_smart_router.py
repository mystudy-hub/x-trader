"""Unit tests for SmartRouter, capability-driven close routing and product-level intents (S2-04, A01, FR-ORD-01/08)."""

from datetime import datetime, timezone

import pytest

from qh_trader.core.constants import Exchange, MissingRuleError, Offset, OrderType, PositionSide, Side
from qh_trader.core.objects import InstrumentId, OrderIntent, ProductId
from qh_trader.domain.positions import PositionDetail
from qh_trader.domain.smart_router import (
    EXCHANGES_REQUIRING_EXPLICIT_SPLIT,
    CloseCapabilityTable,
    ClosePriority,
    ExchangeCloseCapability,
    OrderPlan,
    ProductIntent,
    SmartRouter,
    UnsupportedOffsetError,
)

NOW = datetime(2024, 6, 3, 1, 0, tzinfo=timezone.utc)
RB = InstrumentId(Exchange.SHFE, "rb2410")
TA = InstrumentId(Exchange.CZCE, "TA409")
M = InstrumentId(Exchange.DCE, "m2409")


def capability_table() -> CloseCapabilityTable:
    return CloseCapabilityTable(
        version="cap-test-v1",
        capabilities={
            Exchange.SHFE: ExchangeCloseCapability(True, True, False, "fixture"),
            Exchange.INE: ExchangeCloseCapability(True, True, False, "fixture"),
            # CZCE fixture: unified close only, explicit split not supported
            Exchange.CZCE: ExchangeCloseCapability(False, False, True, "fixture"),
            # DCE fixture: supports both explicit close-today and unified close, split not required
            Exchange.DCE: ExchangeCloseCapability(True, False, True, "fixture"),
        },
    )


def make_intent(
    inst: InstrumentId,
    offset: Offset,
    qty: int,
    cid: str = "parent-001",
    side: Side = Side.SELL,
    mapping_version: str | None = None,
) -> OrderIntent:
    return OrderIntent(
        client_order_id=cid,
        account_id="test-acc",
        strategy_id="strat-1",
        instrument=inst,
        side=side,
        offset=offset,
        quantity=qty,
        order_type=OrderType.LIMIT,
        limit_price_ticks=3500,
        created_at=NOW,
        mapping_version=mapping_version,
    )


@pytest.fixture
def gen_id():
    counter = 0

    def _gen():
        nonlocal counter
        counter += 1
        return f"child-{counter}"

    return _gen


def test_capability_table_validation():
    assert EXCHANGES_REQUIRING_EXPLICIT_SPLIT == frozenset({Exchange.SHFE, Exchange.INE})
    with pytest.raises(ValueError, match="requires explicit close-today split"):
        CloseCapabilityTable("v", {Exchange.SHFE: ExchangeCloseCapability(True, False, True)})
    with pytest.raises(ValueError, match="must support CLOSE_TODAY"):
        ExchangeCloseCapability(False, True, False)
    with pytest.raises(ValueError, match="at least"):
        ExchangeCloseCapability(False, False, False)
    with pytest.raises(TypeError, match="CloseCapabilityTable"):
        SmartRouter(capabilities=None)  # type: ignore[arg-type]
    with pytest.raises(MissingRuleError, match="no verified close capability"):
        capability_table().for_exchange(Exchange.GFEX)


def test_open_passes_through_without_split(gen_id):
    router = SmartRouter(capability_table())
    plan = router.plan_order(make_intent(RB, Offset.OPEN, 3, side=Side.BUY), None, gen_id)
    assert isinstance(plan, OrderPlan)
    assert plan.children == (plan.parent,)
    assert plan.attribution == ()
    assert plan.capability_version == "cap-test-v1"


def test_shfe_close_split_yesterday_first(gen_id):
    """A01: 昨仓 2 手，今仓 5 手，平 3 手 -> 平昨 2 手 + 平今 1 手."""
    router = SmartRouter(capability_table(), default_priority=ClosePriority.YESTERDAY_FIRST)
    pos = PositionDetail(instrument=RB, side=PositionSide.LONG, pos_yd=2, pos_td=5)
    plan = router.plan_order(make_intent(RB, Offset.CLOSE, 3, cid="parent-auto-close"), pos, gen_id)
    assert len(plan.children) == 2
    assert (plan.children[0].offset, plan.children[0].quantity) == (Offset.CLOSE_YESTERDAY, 2)
    assert (plan.children[1].offset, plan.children[1].quantity) == (Offset.CLOSE_TODAY, 1)
    assert [c.client_order_id for c in plan.children] == ["child-1", "child-2"]
    assert all(c.parent_order_id == "parent-auto-close" for c in plan.children)
    assert plan.attribution == ((Offset.CLOSE_YESTERDAY, 2), (Offset.CLOSE_TODAY, 1))
    assert plan.priority == ClosePriority.YESTERDAY_FIRST
    assert (plan.yesterday_lots, plan.today_lots) == (2, 1)
    # planning is side-effect free
    assert (pos.frozen_yd, pos.frozen_td) == (0, 0)


def test_shfe_close_split_today_first(gen_id):
    """A01: 平今优先，昨仓 2 手，今仓 5 手，平 3 手 -> 平今 3 手；平 6 手 -> 平今 5 + 平昨 1."""
    router = SmartRouter(capability_table(), default_priority=ClosePriority.TODAY_FIRST)
    pos = PositionDetail(instrument=RB, side=PositionSide.LONG, pos_yd=2, pos_td=5)
    plan = router.plan_order(make_intent(RB, Offset.CLOSE, 3), pos, gen_id)
    assert len(plan.children) == 1
    assert (plan.children[0].offset, plan.children[0].quantity) == (Offset.CLOSE_TODAY, 3)
    assert plan.attribution == ((Offset.CLOSE_TODAY, 3),)

    plan6 = router.plan_order(make_intent(RB, Offset.CLOSE, 6), pos, gen_id)
    assert [(c.offset, c.quantity) for c in plan6.children] == [(Offset.CLOSE_TODAY, 5), (Offset.CLOSE_YESTERDAY, 1)]


def test_product_priority_keys_are_case_insensitive():
    router = SmartRouter(capability_table(), product_priorities={" RB ": ClosePriority.TODAY_FIRST})
    assert router.get_priority("rb") == ClosePriority.TODAY_FIRST
    assert router.get_priority("Rb") == ClosePriority.TODAY_FIRST
    assert router.get_priority("cu") == ClosePriority.YESTERDAY_FIRST
    pos = PositionDetail(instrument=RB, side=PositionSide.LONG, pos_yd=2, pos_td=5)
    plan = router.plan_order(make_intent(RB, Offset.CLOSE, 3), pos, lambda: "c")
    assert plan.children[0].offset == Offset.CLOSE_TODAY


def test_explicit_close_today_passes_through_on_shfe(gen_id):
    router = SmartRouter(capability_table())
    pos = PositionDetail(instrument=RB, side=PositionSide.LONG, pos_yd=2, pos_td=5)
    plan = router.plan_order(make_intent(RB, Offset.CLOSE_TODAY, 3, cid="parent-close"), pos, gen_id)
    assert len(plan.children) == 1
    assert plan.children[0] is plan.parent
    assert plan.children[0].offset == Offset.CLOSE_TODAY and plan.children[0].quantity == 3
    assert plan.attribution == ((Offset.CLOSE_TODAY, 3),)


def test_explicit_close_today_rejected_on_czce(gen_id):
    router = SmartRouter(capability_table())
    pos = PositionDetail(instrument=TA, side=PositionSide.LONG, pos_yd=2, pos_td=5)
    with pytest.raises(UnsupportedOffsetError, match="does not support explicit CLOSE_TODAY"):
        router.plan_order(make_intent(TA, Offset.CLOSE_TODAY, 1), pos, gen_id)
    with pytest.raises(UnsupportedOffsetError, match="does not support explicit CLOSE_YESTERDAY"):
        router.plan_order(make_intent(TA, Offset.CLOSE_YESTERDAY, 1), pos, gen_id)


def test_czce_unified_close_single_child_with_attribution(gen_id):
    router = SmartRouter(capability_table())
    pos = PositionDetail(instrument=TA, side=PositionSide.LONG, pos_yd=2, pos_td=5)
    parent = make_intent(TA, Offset.CLOSE, 3)
    plan = router.plan_order(parent, pos, gen_id)
    assert plan.children == (parent,)
    assert plan.children[0].offset == Offset.CLOSE
    assert plan.attribution == ((Offset.CLOSE_YESTERDAY, 2), (Offset.CLOSE_TODAY, 1))
    assert (plan.yesterday_lots, plan.today_lots) == (2, 1)
    assert plan.capability_version == "cap-test-v1"
    assert plan.notes


def test_dce_with_both_capabilities_passes_unified_close_and_allows_explicit(gen_id):
    router = SmartRouter(capability_table())
    pos = PositionDetail(instrument=M, side=PositionSide.LONG, pos_yd=1, pos_td=1)
    unified = router.plan_order(make_intent(M, Offset.CLOSE, 2), pos, gen_id)
    assert len(unified.children) == 1 and unified.children[0].offset == Offset.CLOSE
    assert unified.attribution == ((Offset.CLOSE_YESTERDAY, 1), (Offset.CLOSE_TODAY, 1))
    explicit = router.plan_order(make_intent(M, Offset.CLOSE_TODAY, 1), pos, gen_id)
    assert explicit.children[0].offset == Offset.CLOSE_TODAY


def test_insufficient_position_raises(gen_id):
    router = SmartRouter(capability_table())
    pos = PositionDetail(instrument=RB, side=PositionSide.LONG, pos_yd=1, pos_td=1)
    with pytest.raises(ValueError, match="insufficient available today position"):
        router.plan_order(make_intent(RB, Offset.CLOSE_TODAY, 2), pos, gen_id)
    with pytest.raises(ValueError, match="insufficient available yesterday position"):
        router.plan_order(make_intent(RB, Offset.CLOSE_YESTERDAY, 2), pos, gen_id)
    with pytest.raises(ValueError, match="insufficient total available position"):
        router.plan_order(make_intent(RB, Offset.CLOSE, 3), pos, gen_id)
    with pytest.raises(ValueError, match="without target position"):
        router.plan_order(make_intent(RB, Offset.CLOSE, 1), None, gen_id)
    # frozen lots are not available
    frozen = PositionDetail(instrument=RB, side=PositionSide.LONG, pos_yd=2, frozen_yd=2)
    with pytest.raises(ValueError, match="insufficient total available position"):
        router.plan_order(make_intent(RB, Offset.CLOSE, 1), frozen, gen_id)


def test_product_intent_resolved_with_mapping_version(gen_id):
    product = ProductId(Exchange.SHFE, "rb")
    router = SmartRouter(
        capability_table(),
        resolve_contract=lambda p: InstrumentId(p.exchange, "rb2410") if p.product == "rb" else RB,
        mapping_version="dominant-2024-06-03",
    )
    pintent = ProductIntent(
        client_order_id="p-1",
        account_id="test-acc",
        strategy_id="strat-1",
        product=product,
        side=Side.SELL,
        offset=Offset.CLOSE,
        quantity=2,
        order_type=OrderType.LIMIT,
        limit_price_ticks=3500,
        created_at=NOW,
    )
    pos = PositionDetail(instrument=RB, side=PositionSide.LONG, pos_yd=1, pos_td=1)
    plan = router.plan_order(pintent, pos, gen_id)
    assert plan.parent.instrument == RB
    assert plan.parent.mapping_version == "dominant-2024-06-03"
    assert plan.mapping_version == "dominant-2024-06-03"
    assert all(c.mapping_version == "dominant-2024-06-03" for c in plan.children)
    assert [(c.offset, c.quantity) for c in plan.children] == [(Offset.CLOSE_YESTERDAY, 1), (Offset.CLOSE_TODAY, 1)]

    # explicit instrument intent passes through unchanged (its own mapping_version retained)
    direct = make_intent(RB, Offset.OPEN, 1, side=Side.BUY, mapping_version="explicit")
    assert router.resolve_intent(direct) is direct
    assert router.plan_order(direct, None, gen_id).mapping_version == "explicit"


def test_product_intent_requires_mapping_and_matching_exchange(gen_id):
    product = ProductId(Exchange.SHFE, "rb")
    pintent = ProductIntent(
        client_order_id="p-1",
        account_id="a",
        strategy_id="s",
        product=product,
        side=Side.BUY,
        offset=Offset.OPEN,
        quantity=1,
        order_type=OrderType.MARKET,
        created_at=NOW,
    )
    with pytest.raises(MissingRuleError, match="resolve_contract"):
        SmartRouter(capability_table()).plan_order(pintent, None, gen_id)
    with pytest.raises(MissingRuleError, match="mapping_version"):
        SmartRouter(capability_table(), resolve_contract=lambda p: RB).plan_order(pintent, None, gen_id)
    with pytest.raises(ValueError, match="does not belong to product"):
        SmartRouter(capability_table(), resolve_contract=lambda p: TA, mapping_version="v").plan_order(
            pintent, None, gen_id
        )
    with pytest.raises(TypeError, match="ProductId"):
        ProductIntent(
            client_order_id="p-2",
            account_id="a",
            strategy_id="s",
            product=RB,  # type: ignore[arg-type]
            side=Side.BUY,
            offset=Offset.OPEN,
            quantity=1,
            order_type=OrderType.MARKET,
            created_at=NOW,
        )
