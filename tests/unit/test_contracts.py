"""Catalog-backed resolution and historical ambiguity regression cases."""

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from qh_trader.core.constants import AmbiguousRuleError, Exchange, MissingRuleError
from qh_trader.core.objects import ContractSpec, InstrumentId, ProductId
from qh_trader.data.contracts import CatalogEntry, ContractResolver
from qh_trader.data.sources import normalize_instrument_to_symbol


def ta_entry(year):
    return CatalogEntry(
        spec=ContractSpec(
            instrument=InstrumentId(Exchange.CZCE, f"TA{year % 100:02}05"),
            product=ProductId(Exchange.CZCE, "TA"),
            delivery_year=year,
            delivery_month=5,
            multiplier=Decimal(5),
            price_tick=Decimal(2),
            listed_on=date(year - 1, 5, 1),
            last_trading_day=date(year, 5, 15),
        ),
        aliases=("TA405",),
        source_id="synthetic-only",
        available_at=datetime(year - 1, 1, 1, tzinfo=timezone.utc),
    )


def test_resolve_standard_four_digit(sample_catalog, sample_instrument):
    assert sample_catalog.resolve("SHFE.rb2410")[0] == sample_instrument
    assert sample_catalog.resolve("rb2410", as_of=date(2024, 9, 9))[1:] == (2024, 10)
    with pytest.raises(ValueError, match="conflicting exchange"):
        sample_catalog.resolve("SHFE.rb2410", exchange=Exchange.DCE)


def test_czce_short_code_uses_catalog_lifetimes():
    resolver = ContractResolver([ta_entry(2014), ta_entry(2024)], "synthetic-catalog")
    assert resolver.resolve("TA405", as_of=date(2014, 3, 1))[0].symbol == "TA1405"
    assert resolver.resolve("TA405", as_of=date(2024, 3, 1))[0].symbol == "TA2405"
    with pytest.raises(MissingRuleError):
        resolver.resolve("TA405", as_of=date(2018, 3, 1))
    with pytest.raises(ValueError, match="as_of"):
        resolver.resolve("TA405")
    with pytest.raises(ValueError, match="resolved"):
        normalize_instrument_to_symbol("CZCE.TA405")


def test_missing_or_ambiguous_catalog_never_falls_back():
    with pytest.raises(MissingRuleError, match="catalog"):
        ContractResolver().resolve("SHFE.rb2410")
    entry = ta_entry(2024)
    conflict = replace(entry, source_id="conflicting-synthetic-catalog")
    resolver = ContractResolver([entry, conflict], "ambiguous-fixture")
    with pytest.raises(AmbiguousRuleError):
        resolver.resolve("TA405", as_of=date(2024, 3, 1))


def test_get_spec_returns_supplied_dates_not_estimated_fifteenth(sample_catalog):
    assert sample_catalog.get_spec("SHFE.rb2410").listed_on == date(2024, 1, 1)
    with pytest.raises(MissingRuleError):
        sample_catalog.get_spec("SHFE.rb2410", as_of=date(2024, 10, 16))
    with pytest.raises(MissingRuleError):
        sample_catalog.resolve("SHFE.rb2410", known_at=datetime(2023, 1, 1, tzinfo=timezone.utc))


def test_real_catalog_file_if_present():
    from pathlib import Path

    catalog_path = Path("config/contract_catalog_2024v1.json")
    if not catalog_path.exists():
        pytest.skip("real catalog file not generated")
    resolver = ContractResolver.from_file(catalog_path)
    inst, y, m = resolver.resolve("SHFE.rb2410")
    assert inst.symbol == "rb2410"
    assert (y, m) == (2024, 10)
    fg_inst, fg_y, fg_m = resolver.resolve("FG501", as_of=date(2024, 10, 1))
    assert fg_inst.exchange == Exchange.CZCE
    assert (fg_y, fg_m) == (2025, 1)
