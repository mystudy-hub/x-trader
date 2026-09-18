"""Unit tests for ExchangeLimits, RiskManager, self-trade prevention, circuit breaker and control epochs.

Covers: S2-05, S2-06, S2-10, A08, A09, A19, A23; FR-RISK-01~05, FR-RISK-07.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from qh_trader.core.constants import AmbiguousRuleError, Exchange, Offset, OrderStatus, OrderType, PositionSide, Side
from qh_trader.core.objects import ContractSpec, ControlEpoch, InstrumentId, OrderIntent, ProductId
from qh_trader.domain.ledger import AccountFundsState, AccountLedger
from qh_trader.domain.limits import ExchangeLimits, LimitKind, LimitRule, LimitSource, LimitViolationError
from qh_trader.domain.orders import Order
from qh_trader.domain.positions import PositionDetail, PositionManager
from qh_trader.domain.risk import (
    EpochViolationError,
    RiskManager,
    RiskState,
    RiskStateTransitionError,
    RiskViolationError,
)

DAY = date(2024, 6, 3)
NOW = datetime(2024, 6, 3, 1, 0, tzinfo=timezone.utc)


@pytest.fixture
def rb():
    return InstrumentId(Exchange.SHFE, "rb2410")


@pytest.fixture
def funds():
    return AccountFundsState(
        balance=Decimal("100000"),
        total_equity=Decimal("100000"),
        margin_used=Decimal("10000"),
        frozen_margin=Decimal("0"),
        frozen_fee=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        realized_mtm_pnl=Decimal("0"),
        realized_trade_pnl=Decimal("0"),
        total_commission=Decimal("0"),
        broker_available=Decimal("90000"),
        available_for_new_trades=Decimal("90000"),
        margin_coverage_equity=Decimal("100000"),
        risk_ratio=Decimal("0.10"),
    )


def intent(
    inst: InstrumentId,
    cid: str = "ord-1",
    side: Side = Side.BUY,
    offset: Offset = Offset.OPEN,
    qty: int = 1,
    price: int | None = 3500,
    order_type: OrderType = OrderType.LIMIT,
) -> OrderIntent:
    return OrderIntent(
        client_order_id=cid,
        account_id="acc-1",
        strategy_id="strat-1",
        instrument=inst,
        side=side,
        offset=offset,
        quantity=qty,
        order_type=order_type,
        limit_price_ticks=price if order_type == OrderType.LIMIT else None,
        created_at=NOW,
    )


def rule(kind: LimitKind, scope: str, value, effective_from: date, effective_to: date | None = None) -> LimitRule:
    return LimitRule(
        kind=kind,
        scope=scope,
        value=value,
        source=LimitSource.EXCHANGE,
        effective_from=effective_from,
        effective_to=effective_to,
        evidence_ref="docs/limits/test-fixture",
    )


# ============================================================================ limits (A19, FR-RISK-02/03)


def test_no_default_limits_and_missing_limits_reported(rb):
    limits = ExchangeLimits()
    assert limits.get_max_position_lots(rb, DAY) is None
    assert limits.get_max_cancels(rb, DAY) is None
    assert limits.get_max_open_lots(rb, DAY) is None
    assert limits.missing_limits(rb, DAY) == (
        LimitKind.MAX_OPEN_LOTS_PER_DAY,
        LimitKind.MAX_POSITION_LOTS,
        LimitKind.MAX_CANCELS_PER_DAY,
    )
    # unconfigured dimensions are not checked: a huge order and huge cancel count both pass
    limits.check_order(intent(rb, qty=100000), DAY, current_open_lots_today=10**6, current_holding_lots=10**6)
    limits.check_cancel(rb, DAY, current_cancels_today=10**6)
    assert not hasattr(limits, "default_max_position")
    assert not hasattr(limits, "default_max_cancels")


def test_limit_rule_requires_evidence_and_source():
    with pytest.raises(ValueError, match="evidence_ref"):
        LimitRule(LimitKind.MAX_POSITION_LOTS, "rb", 500, LimitSource.EXCHANGE, DAY, "")
    with pytest.raises(TypeError, match="LimitSource"):
        LimitRule(LimitKind.MAX_POSITION_LOTS, "rb", 500, "EXCHANGE", DAY, "ev")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="effective_to"):
        rule(LimitKind.MAX_POSITION_LOTS, "rb", 500, DAY, DAY)


def test_daily_open_and_position_limits(rb):
    limits = ExchangeLimits(
        [
            rule(LimitKind.MAX_OPEN_LOTS_PER_DAY, "SHFE.rb2410", 10, date(2024, 1, 1)),
            rule(LimitKind.MAX_POSITION_LOTS, "SHFE.rb2410", 20, date(2024, 1, 1)),
        ]
    )
    order = intent(rb, qty=6)
    with pytest.raises(LimitViolationError, match="daily open limit exceeded"):
        limits.check_order(order, DAY, current_open_lots_today=5, current_holding_lots=0)
    with pytest.raises(LimitViolationError, match="position limit exceeded"):
        limits.check_order(order, DAY, current_open_lots_today=0, current_holding_lots=16)
    limits.check_order(order, DAY, current_open_lots_today=4, current_holding_lots=14)
    # position limit does not apply to closes
    limits.check_order(intent(rb, side=Side.SELL, offset=Offset.CLOSE, qty=6), DAY, 100, 100)


def test_effective_date_switching_and_instrument_over_product(rb):
    limits = ExchangeLimits(
        [
            rule(LimitKind.MAX_POSITION_LOTS, "RB", 900, date(2024, 1, 1)),
            # contract-stage decreasing limits for rb2410: 500 until Aug 31, then 100 in Sept, then 30 in Oct
            rule(LimitKind.MAX_POSITION_LOTS, "shfe.rb2410", 500, date(2024, 1, 1), date(2024, 9, 1)),
            rule(LimitKind.MAX_POSITION_LOTS, "SHFE.rb2410", 100, date(2024, 9, 1), date(2024, 10, 1)),
            rule(LimitKind.MAX_POSITION_LOTS, "SHFE.rb2410", 30, date(2024, 10, 1)),
        ]
    )
    assert limits.get_max_position_lots(rb, date(2024, 6, 3)) == 500
    assert limits.get_max_position_lots(rb, date(2024, 9, 1)) == 100
    assert limits.get_max_position_lots(rb, date(2024, 9, 30)) == 100
    assert limits.get_max_position_lots(rb, date(2024, 10, 15)) == 30
    # before any contract rule: product-level rule (case-insensitive) applies
    other = InstrumentId(Exchange.SHFE, "rb2501")
    assert limits.get_max_position_lots(other, date(2024, 6, 3)) == 900
    assert limits.get_max_position_lots(other, date(2023, 12, 31)) is None
    order = intent(rb, qty=50)
    limits.check_order(order, date(2024, 8, 30), 0, 400)
    with pytest.raises(LimitViolationError, match="limit=100"):
        limits.check_order(order, date(2024, 9, 2), 0, 60)


def test_ambiguous_same_day_rules_rejected(rb):
    limits = ExchangeLimits(
        [
            rule(LimitKind.MAX_CANCELS_PER_DAY, "rb", 400, DAY),
            rule(LimitKind.MAX_CANCELS_PER_DAY, "rb", 500, DAY),
        ]
    )
    with pytest.raises(AmbiguousRuleError):
        limits.get_max_cancels(rb, DAY)


def test_delivery_month_open_rejected_for_natural_person(rb):
    spec = ContractSpec(
        instrument=rb,
        product=ProductId(Exchange.SHFE, "rb"),
        delivery_year=2024,
        delivery_month=10,
        multiplier=Decimal("10"),
        price_tick=Decimal("1"),
        listed_on=date(2023, 10, 16),
        last_trading_day=date(2024, 10, 15),
    )
    limits = ExchangeLimits()
    open_order = intent(rb)
    close_order = intent(rb, side=Side.SELL, offset=Offset.CLOSE)
    # September: still allowed
    limits.check_order(open_order, date(2024, 9, 30), 0, 0, contract_spec=spec, natural_person=True)
    # October 1st onward: natural person may not open
    with pytest.raises(LimitViolationError, match="delivery month"):
        limits.check_order(open_order, date(2024, 10, 8), 0, 0, contract_spec=spec, natural_person=True)
    # closes remain allowed; non-natural-person not restricted by delivery month alone
    limits.check_order(close_order, date(2024, 10, 8), 0, 0, contract_spec=spec, natural_person=True)
    limits.check_order(open_order, date(2024, 10, 8), 0, 0, contract_spec=spec, natural_person=False)


def test_explicit_no_open_from_rule(rb):
    limits = ExchangeLimits([rule(LimitKind.NO_OPEN_FROM, "SHFE.rb2410", date(2024, 9, 20), date(2024, 1, 1))])
    limits.check_order(intent(rb), date(2024, 9, 19), 0, 0)
    with pytest.raises(LimitViolationError, match="open forbidden"):
        limits.check_order(intent(rb), date(2024, 9, 20), 0, 0)


def test_price_band_applies_to_open_and_close(rb):
    limits = ExchangeLimits()
    band = (3300, 3700)
    limits.check_order(intent(rb, price=3700), DAY, 0, 0, price_band=band)
    with pytest.raises(LimitViolationError, match="out of price band"):
        limits.check_order(intent(rb, price=3701), DAY, 0, 0, price_band=band)
    with pytest.raises(LimitViolationError, match="out of price band"):
        limits.check_order(intent(rb, side=Side.SELL, offset=Offset.CLOSE, price=3299), DAY, 0, 0, price_band=band)
    # market order has no limit price: band is not applicable
    limits.check_order(intent(rb, order_type=OrderType.MARKET), DAY, 0, 0, price_band=band)


def test_check_cancel_limit(rb):
    limits = ExchangeLimits([rule(LimitKind.MAX_CANCELS_PER_DAY, "rb", 3, date(2024, 1, 1))])
    limits.check_cancel(rb, DAY, 2)
    with pytest.raises(LimitViolationError, match="cancel limit reached"):
        limits.check_cancel(rb, DAY, 3)


# ============================================================================ epochs (A23, FR-RISK-07)


def test_stale_epoch_rejects_order_cancel_amend_and_recovery(rb, funds):
    risk = RiskManager("acc-1", control=ControlEpoch("ctl-A", 5), trading_day=DAY)
    pos = PositionDetail(instrument=rb, side=PositionSide.LONG)
    with pytest.raises(EpochViolationError, match="epoch mismatch"):
        risk.check_order(intent(rb), command_epoch=4, funds=funds, current_pos=pos)
    order = Order(intent=intent(rb, cid="o-1"))
    with pytest.raises(EpochViolationError, match="cancel"):
        risk.check_cancel_command(order, 4)
    with pytest.raises(EpochViolationError, match="amend"):
        risk.check_amend_command(order, 4, new_quantity=1)
    with pytest.raises(EpochViolationError, match="recovery"):
        risk.check_recovery_command(4)
    # a future epoch is not a valid current controller either
    with pytest.raises(EpochViolationError):
        risk.check_recovery_command(6)
    # correct epoch passes
    risk.check_recovery_command(5)
    risk.check_cancel_command(order, ControlEpoch("ctl-A", 5))
    # same epoch but different controller_id is rejected
    with pytest.raises(EpochViolationError, match="controller"):
        risk.check_cancel_command(order, ControlEpoch("ctl-B", 5))


def test_takeover_advances_epoch_and_rejects_old_controller(rb):
    risk = RiskManager("acc-1", control=ControlEpoch("ctl-A", 1))
    risk.assume_control(ControlEpoch("ctl-B", 2))
    assert risk.control == ControlEpoch("ctl-B", 2)
    assert risk.controller_id == "ctl-B" and risk.current_epoch == 2
    with pytest.raises(EpochViolationError):
        risk.check_recovery_command(ControlEpoch("ctl-A", 1))
    with pytest.raises(EpochViolationError, match="never reused"):
        risk.advance_epoch(2)
    with pytest.raises(EpochViolationError, match="never reused"):
        risk.advance_epoch(1)


def test_cancel_command_enforces_cancel_limit_but_not_risk_state(rb):
    limits = ExchangeLimits([rule(LimitKind.MAX_CANCELS_PER_DAY, "rb", 1, date(2024, 1, 1))])
    risk = RiskManager("acc-1", limits=limits, trading_day=DAY)
    risk.escalate("halt", RiskState.HALTED)
    risk.enter_open_cooldown(NOW + timedelta(hours=1))
    order = Order(intent=intent(rb, cid="o-1"))
    risk.check_cancel_command(order, 1)  # halted + cooldown never block cancels
    risk.on_order_canceled(order.intent, unfilled_qty=1)
    with pytest.raises(LimitViolationError, match="cancel limit reached"):
        risk.check_cancel_command(order, 1)
    order.status = OrderStatus.FILLED
    with pytest.raises(RiskViolationError, match="terminal"):
        risk.check_cancel_command(order, 1)


def test_amend_that_increases_open_risk_is_blocked_in_reduce_only(rb):
    risk = RiskManager("acc-1", trading_day=DAY)
    risk.escalate("drawdown")
    open_order = Order(intent=intent(rb, cid="o-open", qty=2))
    close_order = Order(intent=intent(rb, cid="o-close", side=Side.SELL, offset=Offset.CLOSE, qty=2))
    risk.check_amend_command(open_order, 1, new_quantity=1)  # reducing quantity does not add risk
    with pytest.raises(RiskViolationError, match="open orders are blocked"):
        risk.check_amend_command(open_order, 1, new_quantity=3)
    risk.check_amend_command(close_order, 1, new_quantity=3, new_limit_price_ticks=3400)
    with pytest.raises(RiskViolationError, match="price band"):
        risk.check_amend_command(close_order, 1, new_limit_price_ticks=9999, price_band=(3300, 3700))


# ============================================================================ circuit breaker (A09, FR-RISK-05)


def test_escalate_only_forward_and_reset_refusal(rb):
    risk = RiskManager("acc-1")
    assert risk.escalate("loss") == RiskState.REDUCE_ONLY
    assert risk.escalate("cancel pending") == RiskState.CANCELING
    assert risk.escalate("cancels confirmed") == RiskState.FLATTENING
    assert risk.escalate("cannot flatten") == RiskState.HALTED
    with pytest.raises(RiskStateTransitionError, match="terminal"):
        risk.escalate("more")
    assert [e.to_state for e in risk.risk_events] == [
        RiskState.REDUCE_ONLY,
        RiskState.CANCELING,
        RiskState.FLATTENING,
        RiskState.HALTED,
    ]
    # reset refused unless both conditions hold
    for cleared, consistent in ((False, False), (True, False), (False, True)):
        with pytest.raises(RiskStateTransitionError, match="reset refused"):
            risk.reset_risk_state(cause_cleared=cleared, account_consistent=consistent)
    assert risk.risk_state == RiskState.HALTED
    risk.reset_risk_state(cause_cleared=True, account_consistent=True)
    assert risk.risk_state == RiskState.NORMAL
    # explicit targets must move forward; skipping ahead is allowed, backwards is not
    risk.escalate("jump", RiskState.FLATTENING)
    with pytest.raises(RiskStateTransitionError, match="only move forward"):
        risk.escalate("back", RiskState.REDUCE_ONLY)
    with pytest.raises(RiskStateTransitionError, match="only move forward"):
        risk.escalate("same", RiskState.FLATTENING)


def test_reduce_only_blocks_open_but_allows_close(rb, funds):
    risk = RiskManager("acc-1", trading_day=DAY)
    risk.escalate("market crash")
    pos = PositionDetail(instrument=rb, side=PositionSide.LONG, pos_td=5)
    with pytest.raises(RiskViolationError, match="open orders are blocked"):
        risk.check_order(intent(rb), 1, funds, pos)
    risk.check_order(intent(rb, side=Side.SELL, offset=Offset.CLOSE_TODAY), 1, funds, pos)
    risk.escalate("halt", RiskState.HALTED)
    with pytest.raises(RiskViolationError, match="HALTED"):
        risk.check_order(intent(rb, side=Side.SELL, offset=Offset.CLOSE_TODAY), 1, funds, pos)


def test_cooldown_blocks_open_not_close(rb, funds):
    risk = RiskManager("acc-1", trading_day=DAY)
    risk.enter_open_cooldown(NOW + timedelta(minutes=5))
    pos = PositionDetail(instrument=rb, side=PositionSide.LONG, pos_yd=3)
    with pytest.raises(RiskViolationError, match="open cooldown"):
        risk.check_order(intent(rb), 1, funds, pos, now=NOW)
    risk.check_order(intent(rb, side=Side.SELL, offset=Offset.CLOSE_YESTERDAY), 1, funds, pos, now=NOW)
    # after cooldown expires, opens pass again; risk state was never touched
    risk.check_order(intent(rb), 1, funds, pos, now=NOW + timedelta(minutes=6))
    assert risk.risk_state == RiskState.NORMAL


def test_flatten_request_is_not_flattened(rb):
    risk = RiskManager("acc-1")
    pm = PositionManager("acc-1", trading_day=DAY)
    pm.get_position(rb, PositionSide.LONG).pos_yd = 4
    risk.request_flatten("breaker")
    assert risk.flatten_requested is True
    assert risk.flattened(pm) is False
    assert risk.unresolved_flatten(pm) is True
    remaining = risk.remaining_risk(pm)
    assert len(remaining) == 1 and remaining[0].lots == 4 and remaining[0].side == PositionSide.LONG
    pm.get_position(rb, PositionSide.LONG).pos_yd = 0
    assert risk.flattened(pm) is True
    assert risk.unresolved_flatten(pm) is False


# ============================================================================ counters (FR-RISK-02)


def test_counters_keyed_by_day_and_restorable(rb):
    risk = RiskManager("acc-1", trading_day=DAY)
    opened = intent(rb, cid="o-1", qty=5)
    risk.on_order_accepted(opened)
    risk.on_order_accepted(intent(rb, cid="o-2", qty=3))
    risk.on_order_canceled(opened, unfilled_qty=2)  # cancel of unfilled part decrements opens
    assert risk.today_open_lots(rb) == 6
    assert risk.today_cancels_count(rb) == 1
    risk.on_order_rejected(intent(rb, cid="o-3", qty=4), was_accepted=False)
    assert risk.today_open_lots(rb) == 6
    risk.on_order_rejected(intent(rb, cid="o-2", qty=3), was_accepted=True)
    assert risk.today_open_lots(rb) == 3
    # close orders never count as opens
    risk.on_order_accepted(intent(rb, cid="o-4", side=Side.SELL, offset=Offset.CLOSE, qty=9))
    assert risk.today_open_lots(rb) == 3

    next_day = DAY + timedelta(days=1)
    assert risk.reset_daily_counters(next_day) is True
    assert risk.reset_daily_counters(next_day) is False  # idempotent
    assert risk.today_open_lots(rb) == 0
    assert risk.today_open_lots(rb, DAY) == 3  # previous day retained for persistence
    risk.on_order_accepted(intent(rb, cid="o-5", qty=1))
    with pytest.raises(ValueError, match="backwards"):
        risk.reset_daily_counters(DAY)

    snap = risk.snapshot_counters()
    restored = RiskManager("acc-1")
    restored.restore_counters(snap)
    assert restored.trading_day == next_day
    assert restored.today_open_lots(rb) == 1
    assert restored.today_open_lots(rb, DAY) == 3
    assert restored.today_cancels_count(rb, DAY) == 1
    assert restored.snapshot_counters() == snap
    with pytest.raises(ValueError, match="snapshot version"):
        restored.restore_counters({"version": 99})


def test_open_limit_uses_counted_lots_for_trading_day(rb, funds):
    limits = ExchangeLimits([rule(LimitKind.MAX_OPEN_LOTS_PER_DAY, "rb", 5, date(2024, 1, 1))])
    risk = RiskManager("acc-1", limits=limits, trading_day=DAY)
    pos = PositionDetail(instrument=rb, side=PositionSide.LONG)
    risk.on_order_accepted(intent(rb, qty=4))
    with pytest.raises(LimitViolationError, match="daily open limit"):
        risk.check_order(intent(rb, qty=2), 1, funds, pos)
    risk.check_order(intent(rb, qty=1), 1, funds, pos)
    with pytest.raises(ValueError, match="trading_day is required"):
        RiskManager("acc-1").check_order(intent(rb), 1, funds, pos)


# ============================================================================ self-trade (A08, FR-RISK-04)


def test_self_trade_prevention_limit_and_market(rb, funds):
    risk = RiskManager("acc-1", trading_day=DAY)
    pos = PositionDetail(instrument=rb, side=PositionSide.LONG)
    active_sell = Order(intent=intent(rb, cid="existing-sell", side=Side.SELL, price=3500))
    with pytest.raises(RiskViolationError, match="self-trade prevented"):
        risk.check_order(intent(rb, cid="buy", price=3500), 1, funds, pos, active_orders=[active_sell])
    risk.check_order(intent(rb, cid="buy", price=3499), 1, funds, pos, active_orders=[active_sell])
    # market order crosses any opposite active order (conservative)
    with pytest.raises(RiskViolationError, match="market order crosses"):
        risk.check_order(intent(rb, cid="mkt", order_type=OrderType.MARKET), 1, funds, pos, active_orders=[active_sell])
    # active market order on the opposite side crosses a new limit order too
    active_mkt_sell = Order(intent=intent(rb, cid="mkt-sell", side=Side.SELL, order_type=OrderType.MARKET))
    with pytest.raises(RiskViolationError, match="market order crosses"):
        risk.check_order(intent(rb, cid="buy", price=1), 1, funds, pos, active_orders=[active_mkt_sell])
    # same side or terminal orders never count
    active_sell.status = OrderStatus.CANCELLED
    risk.check_order(intent(rb, cid="buy", price=3600), 1, funds, pos, active_orders=[active_sell])


# ============================================================================ atomic check-and-reserve (A08)


def _ledger_with_position(rb: InstrumentId, lots_yd: int = 0) -> AccountLedger:
    ledger = AccountLedger("acc-1", initial_capital=Decimal("100000"), trading_day=DAY)
    if lots_yd:
        ledger.position_manager.get_position(rb, PositionSide.LONG).pos_yd = lots_yd
    return ledger


def test_check_and_reserve_success_reserves_both(rb):
    risk = RiskManager("acc-1", trading_day=DAY)
    ledger = _ledger_with_position(rb)
    pm = ledger.position_manager
    pos_res, funds_res = risk.check_and_reserve(
        intent(rb, cid="o-1", qty=2), 1, ledger, pm, [], Decimal("7000"), Decimal("10"), DAY
    )
    assert pos_res.client_order_id == "o-1" and pos_res.offset == Offset.OPEN
    assert funds_res.margin == Decimal("7000")
    assert ledger.frozen_margin == Decimal("7000") and ledger.frozen_fee == Decimal("10")
    assert pm.get_reservation("o-1") is not None
    # pending reservation reduces available funds for the next order
    assert ledger.get_funds_state().available_for_new_trades == Decimal("100000") - Decimal("7010")
    with pytest.raises(RiskViolationError, match="already has a position reservation"):
        risk.check_and_reserve(intent(rb, cid="o-1", qty=2), 1, ledger, pm, [], Decimal("1"), Decimal("0"), DAY)


def test_check_and_reserve_failure_leaves_nothing_reserved(rb):
    risk = RiskManager("acc-1", trading_day=DAY, control=ControlEpoch("ctl", 3))
    ledger = _ledger_with_position(rb, lots_yd=2)
    pm = ledger.position_manager

    def assert_clean():
        assert ledger.frozen_margin == Decimal("0") and ledger.frozen_fee == Decimal("0")
        assert pm.reservations() == ()
        assert pm.get_position(rb, PositionSide.LONG).total_frozen == 0
        pm.verify_frozen_invariant()

    # stale epoch
    with pytest.raises(EpochViolationError):
        risk.check_and_reserve(intent(rb, cid="e"), 2, ledger, pm, [], Decimal("1"), Decimal("0"), DAY)
    assert_clean()
    # insufficient funds
    with pytest.raises(RiskViolationError, match="insufficient available funds"):
        risk.check_and_reserve(intent(rb, cid="f"), 3, ledger, pm, [], Decimal("999999"), Decimal("0"), DAY)
    assert_clean()
    # self-trade
    active_sell = Order(intent=intent(rb, cid="s", side=Side.SELL, price=3500))
    with pytest.raises(RiskViolationError, match="self-trade"):
        risk.check_and_reserve(
            intent(rb, cid="b", price=3500), 3, ledger, pm, [active_sell], Decimal("1"), Decimal("0"), DAY
        )
    assert_clean()
    # price band (applies to close too) -> no position freeze happened
    close = intent(rb, cid="c", side=Side.SELL, offset=Offset.CLOSE, qty=1, price=9999)
    with pytest.raises(LimitViolationError, match="price band"):
        risk.check_and_reserve(close, 3, ledger, pm, [], Decimal("0"), Decimal("1"), DAY, price_band=(3000, 4000))
    assert_clean()
    # closing more than available
    too_many = intent(rb, cid="c2", side=Side.SELL, offset=Offset.CLOSE, qty=3)
    with pytest.raises(RiskViolationError, match="insufficient closable position"):
        risk.check_and_reserve(too_many, 3, ledger, pm, [], Decimal("0"), Decimal("1"), DAY)
    assert_clean()
    # funds reservation failing after position freeze rolls the freeze back
    ledger.reserve_funds("dup", Decimal("1"), Decimal("0"))
    with pytest.raises(RiskViolationError, match="already has a funds reservation"):
        risk.check_and_reserve(intent(rb, cid="dup"), 3, ledger, pm, [], Decimal("1"), Decimal("0"), DAY)
    ledger.release_funds("dup")
    assert_clean()
    with pytest.raises(ValueError, match="negative"):
        risk.check_and_reserve(intent(rb, cid="neg"), 3, ledger, pm, [], Decimal("-1"), Decimal("0"), DAY)
    assert_clean()


def test_check_and_reserve_close_freezes_position(rb):
    risk = RiskManager("acc-1", trading_day=DAY)
    ledger = _ledger_with_position(rb, lots_yd=2)
    pm = ledger.position_manager
    close = intent(rb, cid="c", side=Side.SELL, offset=Offset.CLOSE, qty=2)
    pos_res, _ = risk.check_and_reserve(close, 1, ledger, pm, [], Decimal("0"), Decimal("2"), DAY)
    assert (pos_res.frozen_yd, pos_res.frozen_td) == (2, 0)
    assert pm.get_position(rb, PositionSide.LONG).total_available == 0
    pm.verify_frozen_invariant()


def test_pending_open_reservations_count_against_position_limit(rb):
    limits = ExchangeLimits([rule(LimitKind.MAX_POSITION_LOTS, "rb", 5, date(2024, 1, 1))])
    risk = RiskManager("acc-1", limits=limits, trading_day=DAY)
    ledger = _ledger_with_position(rb, lots_yd=2)
    pm = ledger.position_manager
    risk.check_and_reserve(intent(rb, cid="o-1", qty=2), 1, ledger, pm, [], Decimal("1"), Decimal("0"), DAY)
    # holding 2 + pending 2 + new 2 = 6 > 5
    with pytest.raises(LimitViolationError, match="position limit exceeded"):
        risk.check_and_reserve(intent(rb, cid="o-2", qty=2), 1, ledger, pm, [], Decimal("1"), Decimal("0"), DAY)
    assert pm.get_reservation("o-2") is None and ledger.get_funds_reservation("o-2") is None
    risk.check_and_reserve(intent(rb, cid="o-3", qty=1), 1, ledger, pm, [], Decimal("1"), Decimal("0"), DAY)
