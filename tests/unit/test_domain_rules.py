"""Independent fee/margin arithmetic, explicit rounding and injected rule queries."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from qh_trader.core.constants import Offset
from qh_trader.core.objects import CommissionRule, MarginRule
from qh_trader.domain.rules import RuleEngine, calculate_commission, calculate_margin
from qh_trader.infrastructure.rule_store import RuleStore

D = Decimal


def fee_rule(per_lot="0", rate="0", unit="0.01", rounding="ROUND_HALF_UP"):
    return CommissionRule(D(per_lot), D(rate), D(unit), rounding)


def test_fixed_and_ad_valorem_components_match_independent_hand_calculation():
    assert calculate_commission(fee_rule("3"), D(1500), 5, D(20)) == D("15.00")
    assert calculate_commission(fee_rule(rate="0.0001"), D(3400), 10, D(10)) == D("34.00")
    # Original mixed-fee hand example: fixed 1.5 * 2 = 3; proportional 1000 * 2 * 10 * 0.0001 = 2.
    assert calculate_commission(fee_rule("1.5", "0.0001"), D(1000), 2, D(10)) == D("5.00")


@pytest.mark.parametrize(
    "rounding,expected",
    [
        ("ROUND_HALF_UP", "1.05"),
        ("ROUND_HALF_EVEN", "1.00"),
        ("ROUND_DOWN", "1.00"),
        ("ROUND_UP", "1.05"),
    ],
)
def test_fee_uses_configured_rounding_and_non_cent_currency_unit(rounding, expected):
    assert calculate_commission(fee_rule("1.025", unit="0.05", rounding=rounding), D(100), 1, D(1)) == D(expected)


def test_round_only_after_adding_components():
    # 0.014 + 0.014 = 0.028, rounded once to 0.03 (not 0.01 + 0.01).
    assert calculate_commission(fee_rule("0.014", "0.00014"), D(100), 1, D(1)) == D("0.03")
    with pytest.raises(ValueError, match="rounding mode"):
        fee_rule(rounding="0.01")


def test_margin_is_exact_unless_account_rounding_is_explicit():
    rule = MarginRule(D("0.123456789"), D(0))
    assert calculate_margin(rule, D(1), 1, D(1)) == D("0.123456789")
    assert calculate_margin(rule, D(1), 1, D(1), currency_unit=D("0.05"), rounding="ROUND_CEILING") == D("0.15")
    with pytest.raises(ValueError, match="together"):
        calculate_margin(rule, D(1), 1, D(1), currency_unit=D("0.01"))


@pytest.mark.parametrize("unit", [0.01, D("NaN"), D(0)])
def test_invalid_margin_rounding_units_fail_explicitly(unit):
    with pytest.raises((TypeError, ValueError)):
        calculate_margin(MarginRule(D("0.1"), D(0)), D(100), 1, D(10), currency_unit=unit, rounding="ROUND_UP")


@pytest.mark.parametrize("volume", [True, -1, 1.5, D(2)])
def test_invalid_quantities_are_rejected_instead_of_returning_zero(volume):
    with pytest.raises((TypeError, ValueError)):
        calculate_commission(fee_rule("1"), D(100), volume, D(10))
    with pytest.raises((TypeError, ValueError)):
        calculate_margin(MarginRule(D("0.1"), D(0)), D(100), volume, D(10))


@pytest.mark.parametrize("price", [100.0, D("NaN"), D("Infinity"), D(0)])
def test_financial_inputs_require_finite_decimal(price):
    with pytest.raises((TypeError, ValueError)):
        calculate_commission(fee_rule("1"), price, 1, D(10))


def test_rule_engine_uses_registered_scope_and_offset(sample_instrument):
    at = datetime(2024, 9, 9, tzinfo=timezone.utc)
    with RuleStore() as store:
        store.register_commission_rule(
            sample_instrument, "test-account", Offset.CLOSE_TODAY, fee_rule("10"), "synthetic", "today-v1", at, at
        )
        store.register_commission_rule(
            sample_instrument,
            "test-account",
            Offset.CLOSE_YESTERDAY,
            fee_rule("2"),
            "synthetic",
            "yesterday-v1",
            at,
            at,
        )
        store.register_margin_rule(
            sample_instrument, "test-account", MarginRule(D("0.1"), D(0)), "synthetic", "margin-v1", at, at
        )
        engine = RuleEngine(store)
        assert engine.evaluate_commission(
            sample_instrument, "test-account", Offset.CLOSE_TODAY, D(100), 2, D(10), at, at
        ) == D(20)
        assert engine.evaluate_commission(
            sample_instrument, "test-account", Offset.CLOSE_YESTERDAY, D(100), 2, D(10), at, at
        ) == D(4)
        assert engine.evaluate_margin(sample_instrument, "test-account", D(100), 2, D(10), at, at) == D(200)
