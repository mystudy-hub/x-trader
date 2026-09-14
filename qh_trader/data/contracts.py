"""Historical contract lookup from an explicitly versioned catalog; never infer a delivery year."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from qh_trader.core.clock import utc_timestamp
from qh_trader.core.constants import AmbiguousRuleError, Exchange, MissingRuleError
from qh_trader.core.objects import ContractSpec, InstrumentId, ProductId, require_date, require_text


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    spec: ContractSpec
    aliases: tuple[str, ...]
    source_id: str
    available_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.spec, ContractSpec):
            raise TypeError("catalog entry requires ContractSpec")
        require_text(self.source_id, "catalog source")
        aliases = tuple(self.aliases)
        for alias in aliases:
            require_text(alias, "catalog alias")
        object.__setattr__(self, "aliases", aliases)
        object.__setattr__(self, "available_at", utc_timestamp(self.available_at))


class ContractResolver:
    """Resolve within one supplied catalog version, listing interval and optional visibility cutoff."""

    def __init__(self, entries: Sequence[CatalogEntry] = (), catalog_version: str | None = None) -> None:
        self.entries = tuple(entries)
        self.catalog_version = catalog_version
        if self.entries:
            if catalog_version is None:
                raise ValueError("catalog entries require an explicit catalog_version")
            require_text(catalog_version, "catalog_version")
        if any(not isinstance(entry, CatalogEntry) for entry in self.entries):
            raise TypeError("catalog entries must be normalized")

    @classmethod
    def from_file(cls, path: Path | str) -> ContractResolver:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if data.get("schema_version") != 1:
            raise ValueError("unsupported contract catalog schema")
        entries = []
        for row in data["entries"]:
            exchange = Exchange(row["exchange"])
            spec = ContractSpec(
                instrument=InstrumentId(exchange, row["symbol"]),
                product=ProductId(exchange, row["product"]),
                delivery_year=row["delivery_year"],
                delivery_month=row["delivery_month"],
                multiplier=Decimal(row["multiplier"]),
                price_tick=Decimal(row["price_tick"]),
                listed_on=date.fromisoformat(row["listed_on"]),
                last_trading_day=date.fromisoformat(row["last_trading_day"]),
            )
            entries.append(
                CatalogEntry(
                    spec=spec,
                    aliases=tuple(row["aliases"]),
                    source_id=row["source_id"],
                    available_at=datetime.fromisoformat(row["available_at"]),
                )
            )
        return cls(entries, catalog_version=data["catalog_version"])

    def _entry(
        self,
        raw_symbol: str,
        as_of: date | None,
        exchange: Exchange | None,
        known_at: datetime | None,
    ) -> CatalogEntry:
        if not self.entries:
            raise MissingRuleError("a versioned historical contract catalog is required")
        require_text(raw_symbol, "raw_symbol")
        symbol = raw_symbol.strip()
        if "." in symbol:
            prefix, symbol = symbol.split(".", 1)
            declared = Exchange(prefix.upper())
            if exchange is not None and exchange != declared:
                raise ValueError("conflicting exchange identifiers")
            exchange = declared
        if as_of is not None:
            require_date(as_of, "as_of")
        if re.fullmatch(r"[A-Za-z]+\d{3}", symbol) and as_of is None:
            raise ValueError("short contract codes require as_of trading day")
        visible_until = utc_timestamp(known_at) if known_at is not None else None
        matches = []
        for entry in self.entries:
            spec = entry.spec
            if exchange is not None and spec.instrument.exchange != exchange:
                continue
            aliases = (spec.instrument.symbol, *entry.aliases)
            if symbol.casefold() not in {alias.casefold() for alias in aliases}:
                continue
            if as_of is not None and not spec.listed_on <= as_of <= spec.last_trading_day:
                continue
            if visible_until is not None and entry.available_at > visible_until:
                continue
            matches.append(entry)
        if not matches:
            raise MissingRuleError("no contract exists in the catalog for this code, date and visibility cutoff")
        if len(matches) != 1:
            raise AmbiguousRuleError("contract catalog contains multiple applicable candidates")
        return matches[0]

    def resolve(
        self,
        raw_symbol: str,
        as_of: date | None = None,
        exchange: Exchange | None = None,
        *,
        known_at: datetime | None = None,
    ) -> tuple[InstrumentId, int, int]:
        spec = self._entry(raw_symbol, as_of, exchange, known_at).spec
        return spec.instrument, spec.delivery_year, spec.delivery_month

    def get_spec(
        self,
        raw_symbol: str,
        as_of: date | None = None,
        exchange: Exchange | None = None,
        *,
        known_at: datetime | None = None,
    ) -> ContractSpec:
        return self._entry(raw_symbol, as_of, exchange, known_at).spec
