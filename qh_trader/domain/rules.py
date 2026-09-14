"""Pure fee/margin calculations over explicit rules; persistence is injected through RuleStorePort."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, localcontext

from qh_trader.core.constants import Offset
from qh_trader.core.objects import (
    ROUNDING_MODES,
    CommissionRule,
    InstrumentId,
    MarginRule,
    require_decimal,
    require_int,
)
from qh_trader.core.ports import RuleStorePort


def _validate_inputs(price: Decimal, volume: int, multiplier: Decimal) -> None:
    require_decimal(price, "price", Decimal(0))
    require_decimal(multiplier, "multiplier", Decimal(0))
    require_int(volume, "volume")
    if price <= 0 or multiplier <= 0:
        raise ValueError("actual contract price and multiplier must be positive")


def _precision(*values: Decimal) -> int:
    return max(28, sum(len(value.as_tuple().digits) + abs(int(value.as_tuple().exponent)) for value in values) + 12)


def _round_amount(amount: Decimal, currency_unit: Decimal, rounding: str) -> Decimal:
    require_decimal(currency_unit, "currency_unit", Decimal(0))
    if currency_unit <= 0 or rounding not in ROUNDING_MODES:
        raise ValueError("money rounding needs a positive unit and an explicit rounding mode")
    # quantize(0.05) only fixes decimal places; division is necessary for arbitrary units.
    return (amount / currency_unit).quantize(Decimal(1), rounding=rounding) * currency_unit


def calculate_commission(rule: CommissionRule, price: Decimal, volume: int, multiplier: Decimal) -> Decimal:
    _validate_inputs(price, volume, multiplier)
    with localcontext() as context:
        context.prec = _precision(price, Decimal(volume), multiplier, rule.per_lot, rule.ad_valorem, rule.currency_unit)
        fee = rule.per_lot * volume + price * volume * multiplier * rule.ad_valorem
        return _round_amount(fee, rule.currency_unit, rule.rounding)


def calculate_margin(
    rule: MarginRule,
    price: Decimal,
    volume: int,
    multiplier: Decimal,
    *,
    currency_unit: Decimal | None = None,
    rounding: str | None = None,
) -> Decimal:
    """Return exact margin unless the account explicitly supplies a rounding policy."""
    _validate_inputs(price, volume, multiplier)
    if (currency_unit is None) != (rounding is None):
        raise ValueError("currency_unit and rounding must be supplied together")
    if currency_unit is not None:
        require_decimal(currency_unit, "currency_unit", Decimal(0))
        if currency_unit <= 0 or rounding not in ROUNDING_MODES:
            raise ValueError("money rounding needs a positive unit and an explicit rounding mode")
    with localcontext() as context:
        context.prec = _precision(
            price,
            Decimal(volume),
            multiplier,
            rule.ratio,
            rule.per_lot,
            currency_unit if currency_unit is not None else Decimal(1),
        )
        margin = price * volume * multiplier * rule.ratio + rule.per_lot * volume
        return (
            _round_amount(margin, currency_unit, rounding)
            if currency_unit is not None and rounding is not None
            else margin
        )


class RuleEngine:
    def __init__(self, rule_store: RuleStorePort) -> None:
        self.rule_store = rule_store

    def evaluate_commission(
        self,
        instrument: InstrumentId,
        profile: str,
        offset: Offset,
        price: Decimal,
        volume: int,
        multiplier: Decimal,
        effective_at: datetime,
        known_at: datetime,
    ) -> Decimal:
        rule = self.rule_store.commission_rule(instrument, profile, offset, effective_at, known_at)
        return calculate_commission(rule.value, price, volume, multiplier)

    def evaluate_margin(
        self,
        instrument: InstrumentId,
        profile: str,
        price: Decimal,
        volume: int,
        multiplier: Decimal,
        effective_at: datetime,
        known_at: datetime,
        *,
        currency_unit: Decimal | None = None,
        rounding: str | None = None,
    ) -> Decimal:
        rule = self.rule_store.margin_rule(instrument, profile, effective_at, known_at)
        return calculate_margin(rule.value, price, volume, multiplier, currency_unit=currency_unit, rounding=rounding)
