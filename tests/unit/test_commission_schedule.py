"""金额/每手混合手续费在回测、资金预占、实盘重放和检查点之间保持一致。"""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from qh_trader.core.constants import EventKind, Exchange, MissingRuleError, Offset, OrderType, Side
from qh_trader.core.event import CanonicalEvent, JournalSnapshot
from qh_trader.core.execution import CommandKind, ExecutionCommand
from qh_trader.core.objects import (
    ControlEpoch,
    ControlRecord,
    InstrumentId,
    OrderIdentity,
    OrderIntent,
    Trade,
    TradeKey,
)
from qh_trader.engine.base_engine import BaseEngine, CommissionSchedule, InstrumentEconomics
from qh_trader.engine.live_account_model import (
    AccountModelCorruptionError,
    AccountOpening,
    LiveAccountModel,
    fact_key,
    referenced_instruments,
    stored_economics,
)
from scripts.live_assembly import open_model_read_only
from tests.unit import test_live_account_model as account_fixture

D = Decimal
NOW = datetime(2026, 9, 28, 13, tzinfo=timezone.utc)
DAY = NOW.date()
INST = InstrumentId(Exchange.SHFE, "rb2701")
CONTROL = ControlEpoch("test", 1)


def schedule():
    return CommissionSchedule(
        multiplier=D("10"),
        open_money_ratio=D("0.0001"),
        open_per_lot=D("0.1"),
        close_yesterday_money_ratio=D("0.0002"),
        close_yesterday_per_lot=D("0.2"),
        close_today_money_ratio=D("0.0003"),
        close_today_per_lot=D("0.3"),
        source="fixture:explicit-counter-rates",
    )


def economics():
    return InstrumentEconomics(D("10"), D("1"), D("5"), D("0.16"), "fixture:contract", schedule())


def model(eco=None):
    return LiveAccountModel("account", AccountOpening(D("1000000"), DAY), {INST: eco or economics()})


def snapshot(state, sequence=1):
    return JournalSnapshot("account", sequence, 0, state, frozenset(), ControlRecord(CONTROL, NOW, 1))


def apply_updates(live, state, updates):
    state.update(updates)
    for key in [key for key, value in state.items() if value is None]:
        state.pop(key)
    live.publish(snapshot(state))


def trade(name, price, offset, side):
    fill = Trade(
        account_id="account",
        instrument=INST,
        trading_day=DAY,
        trade_id=name,
        side=side,
        offset=offset,
        quantity=2,
        price=D(price),
        event_time=NOW,
        available_at=NOW,
        deduplication_key=TradeKey("account", Exchange.SHFE, DAY, name),
        order_identity=OrderIdentity(account_id="account", exchange=Exchange.SHFE, client_order_id=name),
    )
    return CanonicalEvent(
        event_id=name,
        kind=EventKind.TRADE_REPORT,
        event_time=NOW,
        available_at=NOW,
        sequence=0,
        source_id="fixture",
        payload=fill,
    )


@pytest.mark.parametrize(
    "offset,expected", [(Offset.OPEN, "6.426"), (Offset.CLOSE_YESTERDAY, "12.852"), (Offset.CLOSE_TODAY, "19.278")]
)
def test_money_and_per_lot_are_added_before_account_rounding(offset, expected):
    assert schedule().commission(D("3113"), 2, offset) == D(expected)


@pytest.mark.parametrize("value", [0.0001, "0.0001", D("NaN"), D("Infinity"), D("-0.01")])
def test_rates_require_finite_nonnegative_decimal(value):
    with pytest.raises((TypeError, ValueError)):
        replace(schedule(), open_money_ratio=value)


def test_multiplier_quantity_price_source_and_unknown_close_are_not_guessed():
    with pytest.raises(ValueError, match="multiplier"):
        replace(economics(), commission_schedule=replace(schedule(), multiplier=D("5")))
    with pytest.raises(ValueError, match="source"):
        replace(schedule(), source="")
    for price, quantity, error in [(D("0"), 1, ValueError), (D("1"), True, TypeError), (D("1"), 0, ValueError)]:
        with pytest.raises(error):
            schedule().commission(price, quantity, Offset.OPEN)
    with pytest.raises(MissingRuleError, match="today/yesterday"):
        schedule().commission(D("3113"), 1, Offset.CLOSE)
    equal = replace(schedule(), close_today_money_ratio=D("0.0002"), close_today_per_lot=D("0.2"))
    assert equal.commission(D("3113"), 2, Offset.CLOSE) == D("12.852")


def test_original_five_argument_economics_keeps_constant_fee():
    eco = InstrumentEconomics(D("10"), D("1"), D("5"), D("0.1"), "legacy")
    assert eco.commission(D("3113"), 2, Offset.OPEN) == D("10")


def test_live_and_backtest_reserve_and_charge_the_same_exact_schedule():
    eco = economics()
    base = BaseEngine("account", start_time=NOW, trading_day=DAY)
    base.register_instrument(INST, eco)
    live = model(eco)
    state = dict(live.opening_updates())
    live.publish(snapshot(state))
    intent = OrderIntent(
        client_order_id="open",
        account_id="account",
        strategy_id="test",
        instrument=INST,
        side=Side.BUY,
        offset=Offset.OPEN,
        quantity=2,
        order_type=OrderType.LIMIT,
        limit_price_ticks=3113,
        created_at=NOW,
    )
    command = ExecutionCommand(
        command_id="open",
        account_id="account",
        producer_id="test",
        control=CONTROL,
        kind=CommandKind.SUBMIT,
        submitted_at=NOW,
        payload=intent,
    )
    plan = live.stage_command(command)
    assert plan.approved, plan.reason
    apply_updates(live, state, plan.state_updates)
    reservation = live.ledger.get_funds_reservation("open")
    assert (reservation.margin, reservation.fee) == (D("9961.6"), D("6.426"))
    assert base._estimate_reservation(intent) == (reservation.margin, reservation.fee)
    for event in (trade("open", "3113", Offset.OPEN, Side.BUY), trade("close", "3200", Offset.CLOSE_TODAY, Side.SELL)):
        base.process_trade_event(event)
        apply_updates(live, state, live.stage_fact(event))
    assert base.ledger.total_commission == live.ledger.total_commission == D("26.226")
    assert base.ledger.balance == live.ledger.balance == D("1001713.77")
    again = model(eco)
    again.publish(snapshot(state))
    assert again.ledger.balance == live.ledger.balance
    assert again.ledger.total_commission == live.ledger.total_commission


@pytest.mark.parametrize("changed", ["rate", "source", "margin"])
def test_opening_declaration_rejects_changed_economics_on_replay(changed):
    original = model()
    state = original.opening_updates()
    eco = economics()
    if changed == "rate":
        eco = replace(eco, commission_schedule=replace(eco.commission_schedule, open_money_ratio=D("0.0002")))
    elif changed == "source":
        eco = replace(eco, commission_schedule=replace(eco.commission_schedule, source="different-counter-evidence"))
    else:
        eco = replace(eco, margin_ratio=D("0.2"))
    with pytest.raises(AccountModelCorruptionError, match="economics"):
        model(eco).publish(snapshot(state))
    assert referenced_instruments(state) == {str(INST)}


def test_checkpoint_preserves_schedule_and_checks_its_source():
    live = model()
    live.publish(snapshot(live.opening_updates()))
    payload = live._dump_kernel(live.replica())
    assert payload["economics"][str(INST)]["commission_schedule"]["source"] == schedule().source
    restored = model()._restore_kernel(payload)
    assert model()._dump_kernel(restored) == payload
    changed = replace(economics(), commission_schedule=replace(schedule(), source="changed"))
    with pytest.raises(AccountModelCorruptionError, match="economics"):
        model(changed)._restore_kernel(payload)


def test_legacy_account_without_declaration_cannot_gain_a_new_schedule_on_replay():
    old = {fact_key(1): {"kind": "opened", "initial_capital": D("1000000"), "trading_day": DAY}}
    with pytest.raises(AccountModelCorruptionError, match="migration"):
        model().publish(snapshot(old))
    constant = replace(economics(), commission_schedule=None)
    legacy = model(constant)
    legacy.publish(snapshot(old))
    assert legacy.ledger.balance == D("1000000")


@pytest.mark.parametrize("compacted", [False, True])
def test_read_only_account_uses_persisted_counter_fees_without_current_catalog(tmp_path, compacted):
    fixture = account_fixture
    eco = economics()
    live = LiveAccountModel(fixture.ACCOUNT, fixture.OPENING, {fixture.RB: eco}, now=lambda: fixture.NOW)
    path = tmp_path / "account.db"
    with fixture.Harness(path, model=live) as account:
        account.make_ready()
        account.submit(fixture.command("open", fixture.intent("open")))
        account.fact(fixture.trade_event("fill", order="open"))
        if compacted:
            account.fact(fixture.settlement_event(fixture.D1, "105"))
            account.fact(fixture.advance_event(fixture.D1, fixture.D2))
            assert account.model.checkpoint_through > 0
        before = account.journal.load_checkpoint()
        journal, restored = open_model_read_only(path, fixture.ACCOUNT, tmp_path / "absent-catalog.json")
        try:
            assert restored.ledger.balance == account.model.ledger.balance
            assert restored.ledger.total_commission == D("0.2")
            assert restored._economics_for(fixture.RB) == eco
            closing = fixture.intent(
                "close", side=Side.SELL, offset=Offset.CLOSE_YESTERDAY if compacted else Offset.CLOSE_TODAY
            )
            plan = restored.stage_command(fixture.command("close", closing))
            assert plan.approved, plan.reason
            restored.publish(
                replace(before, journal_seq=before.journal_seq + 1, state=dict(before.state) | dict(plan.state_updates))
            )
            assert restored.ledger.get_funds_reservation("close").fee == (D("0.4") if compacted else D("0.6"))
        finally:
            journal.close()
        assert account.journal.load_checkpoint() == before


@pytest.mark.parametrize("declaration", [None, [], {str(INST): {"multiplier": "10"}}])
def test_malformed_persisted_economics_never_falls_back_to_catalog(declaration):
    state = {fact_key(1): {"kind": "opened", "economics": declaration}}
    with pytest.raises(AccountModelCorruptionError, match="economics"):
        stored_economics(state)


def test_only_legacy_missing_declaration_allows_catalog_fallback():
    assert stored_economics({fact_key(1): {"kind": "opened"}}) is None
    assert stored_economics({fact_key(1): {"kind": "opened", "economics": {}}}) == {}
