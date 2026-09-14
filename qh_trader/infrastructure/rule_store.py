"""SQLite rule versions with explicit scope, visibility, replacement and migration; no seeded rules."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from qh_trader.core.clock import utc_timestamp
from qh_trader.core.constants import AmbiguousRuleError, Exchange, MarketPhase, MissingRuleError, Offset
from qh_trader.core.objects import (
    Capability,
    CommissionRule,
    ContractSpec,
    InstrumentId,
    MarginRule,
    Permissions,
    ProductId,
    Session,
    VersionedValue,
    require_date,
    require_enum,
    require_text,
)


def _time(value: datetime) -> str:
    return utc_timestamp(value).isoformat(timespec="microseconds")


def _scope(*parts: str) -> str:
    for part in parts:
        require_text(part, "rule scope")
    return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return _time(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError("rule payload must contain normalized Core values")


def _encode(kind: str, value: Any) -> str:
    payload: Any
    if kind == "sessions":
        payload = [asdict(item) for item in value]
    else:
        payload = asdict(value)
        if kind == "capability" and isinstance(value.value, Decimal):
            payload["value"] = {"decimal": str(value.value)}
    return json.dumps(
        payload, default=_json_default, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _instrument(value: dict[str, Any]) -> InstrumentId:
    return InstrumentId(Exchange(value["exchange"]), value["symbol"])


def _decode(kind: str, encoded: str) -> Any:
    payload = json.loads(encoded)
    if kind == "commission":
        return CommissionRule(
            Decimal(payload["per_lot"]),
            Decimal(payload["ad_valorem"]),
            Decimal(payload["currency_unit"]),
            payload["rounding"],
        )
    if kind == "margin":
        return MarginRule(Decimal(payload["ratio"]), Decimal(payload["per_lot"]))
    if kind == "contract":
        product = payload["product"]
        return ContractSpec(
            instrument=_instrument(payload["instrument"]),
            product=ProductId(Exchange(product["exchange"]), product["product"]),
            delivery_year=payload["delivery_year"],
            delivery_month=payload["delivery_month"],
            multiplier=Decimal(payload["multiplier"]),
            price_tick=Decimal(payload["price_tick"]),
            listed_on=date.fromisoformat(payload["listed_on"]),
            last_trading_day=date.fromisoformat(payload["last_trading_day"]),
        )
    if kind == "capability":
        value = payload["value"]
        if isinstance(value, dict):
            value = Decimal(value["decimal"])
        return Capability(value, payload["verified"], payload["evidence_ref"])
    if kind == "sessions":
        return tuple(
            Session(
                instrument=_instrument(item["instrument"]),
                session_id=item["session_id"],
                trading_day=date.fromisoformat(item["trading_day"]),
                start=datetime.fromisoformat(item["start"]),
                end=datetime.fromisoformat(item["end"]),
                phase=MarketPhase(item["phase"]),
                permissions=Permissions(**item["permissions"]),
                rule_version=item["rule_version"],
                source_id=item["source_id"],
                available_at=datetime.fromisoformat(item["available_at"]),
            )
            for item in payload
        )
    raise ValueError("unsupported stored rule kind")


class RuleStore:
    """A RuleStorePort adapter. File-backed schemas are migrated explicitly by the assembly entry point."""

    def __init__(self, database: Path | str = ":memory:") -> None:
        in_memory = str(database) == ":memory:"
        if not in_memory:
            path = Path(database).resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            database = path
        self.connection = sqlite3.connect(database)
        self.connection.row_factory = sqlite3.Row
        mode = self.connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if not in_memory and mode != "wal":
            self.connection.close()
            raise RuntimeError("rule database must support WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        if in_memory:
            self.migrate()

    def __enter__(self) -> RuleStore:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def migrate(self) -> None:
        if self.connection.in_transaction:
            raise RuntimeError("migrate before starting rule transactions")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='journal_schema'"
            ).fetchone():
                raise ValueError("rule metadata and the trading journal must use separate databases")
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS rule_store_schema ("
                "singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL)"
            )
            row = self.connection.execute("SELECT version FROM rule_store_schema WHERE singleton=1").fetchone()
            if row is not None and row[0] != 1:
                raise ValueError("unsupported rule database schema version")
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS rule_versions (
                    rule_id TEXT PRIMARY KEY NOT NULL,
                    kind TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    effective_from TEXT NOT NULL,
                    effective_to TEXT,
                    available_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    replaces_id TEXT REFERENCES rule_versions(rule_id),
                    UNIQUE(kind, scope, source_id, version),
                    CHECK(effective_to IS NULL OR effective_to > effective_from),
                    CHECK(length(source_id)>0 AND length(version)>0)
                )
            """)
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS rule_lookup ON rule_versions(kind, scope, available_at, effective_from)"
            )
            self.connection.execute("INSERT OR IGNORE INTO rule_store_schema VALUES (1, 1)")
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _require_schema(self) -> None:
        exists = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='rule_store_schema'"
        ).fetchone()
        if exists is None:
            raise RuntimeError("rule database needs an explicit migrate() call from the assembly entry point")
        version = self.connection.execute("SELECT version FROM rule_store_schema WHERE singleton=1").fetchone()
        if version is None or version[0] != 1:
            raise ValueError("unsupported rule database schema version")

    @staticmethod
    def _identity(kind: str, scope: str, source_id: str, version: str) -> str:
        return hashlib.sha256(_scope(kind, scope, source_id, version).encode("utf-8")).hexdigest()

    def _register(
        self,
        kind: str,
        scope: str,
        value: Any,
        source_id: str,
        version: str,
        effective_from: datetime,
        available_at: datetime,
        effective_to: datetime | None,
        replaces: tuple[str, str] | None,
    ) -> None:
        self._require_schema()
        wrapped = VersionedValue(
            value=value,
            source_id=source_id,
            version=version,
            effective_from=effective_from,
            available_at=available_at,
            effective_to=effective_to,
        )
        identity = self._identity(kind, scope, source_id, version)
        replacement = self._identity(kind, scope, *replaces) if replaces is not None else None
        if identity == replacement:
            raise ValueError("a rule cannot replace itself")
        record = (
            identity,
            kind,
            scope,
            source_id,
            version,
            _time(wrapped.effective_from),
            _time(wrapped.effective_to) if wrapped.effective_to is not None else None,
            _time(wrapped.available_at),
            _encode(kind, wrapped.value),
            replacement,
        )
        with self.connection:
            previous = self.connection.execute("SELECT * FROM rule_versions WHERE rule_id=?", (identity,)).fetchone()
            if previous is not None:
                if tuple(previous) != record:
                    raise ValueError("a published rule version is immutable; register a new version")
                return
            if replacement is not None:
                parent = self.connection.execute(
                    "SELECT available_at FROM rule_versions WHERE rule_id=?", (replacement,)
                ).fetchone()
                if parent is None:
                    raise MissingRuleError("the replaced rule version has not been registered")
                if parent["available_at"] > record[7]:
                    raise ValueError("a replacement cannot be visible before the version it replaces")
            self.connection.execute("INSERT INTO rule_versions VALUES (?,?,?,?,?,?,?,?,?,?)", record)

    def _find(
        self,
        kind: str,
        scope: str,
        effective_at: datetime | None,
        known_at: datetime,
    ) -> VersionedValue[Any]:
        self._require_schema()
        query = "SELECT * FROM rule_versions WHERE kind=? AND scope=? AND available_at<=?"
        parameters = [kind, scope, _time(known_at)]
        if effective_at is not None:
            effective = _time(effective_at)
            query += " AND effective_from<=? AND (effective_to IS NULL OR effective_to>?)"
            parameters.extend((effective, effective))
        candidates = self.connection.execute(query, parameters).fetchall()
        replaced = set()
        for candidate in candidates:
            parent_id = candidate["replaces_id"]
            visited = set()
            while parent_id is not None:
                if parent_id in visited:
                    raise ValueError("cycle in rule replacement history")
                visited.add(parent_id)
                replaced.add(parent_id)
                parent = self.connection.execute(
                    "SELECT replaces_id FROM rule_versions WHERE rule_id=?", (parent_id,)
                ).fetchone()
                parent_id = parent["replaces_id"] if parent is not None else None
        matches = [row for row in candidates if row["rule_id"] not in replaced]
        if not matches:
            raise MissingRuleError("no rule matches the exact scope, effective time and visibility cutoff")
        if len(matches) != 1:
            raise AmbiguousRuleError("multiple rules match without an explicit replacement relationship")
        row = matches[0]
        return VersionedValue(
            value=_decode(kind, row["payload"]),
            source_id=row["source_id"],
            version=row["version"],
            effective_from=datetime.fromisoformat(row["effective_from"]),
            effective_to=datetime.fromisoformat(row["effective_to"]) if row["effective_to"] else None,
            available_at=datetime.fromisoformat(row["available_at"]),
        )

    def register_contract_rule(
        self,
        instrument: InstrumentId,
        profile: str,
        rule: ContractSpec,
        source_id: str,
        version: str,
        effective_from: datetime,
        available_at: datetime,
        effective_to: datetime | None = None,
        *,
        replaces: tuple[str, str] | None = None,
    ) -> None:
        if not isinstance(rule, ContractSpec) or rule.instrument != instrument:
            raise ValueError("contract rule and scope must refer to the same actual instrument")
        self._register(
            "contract",
            _scope(str(instrument), profile),
            rule,
            source_id,
            version,
            effective_from,
            available_at,
            effective_to,
            replaces,
        )

    def register_commission_rule(
        self,
        instrument: InstrumentId,
        profile: str,
        offset: Offset,
        rule: CommissionRule,
        source_id: str,
        version: str,
        effective_from: datetime,
        available_at: datetime,
        effective_to: datetime | None = None,
        *,
        replaces: tuple[str, str] | None = None,
    ) -> None:
        require_enum(offset, Offset)
        if not isinstance(instrument, InstrumentId) or not isinstance(rule, CommissionRule):
            raise TypeError("commission registration requires a normalized instrument and rule")
        self._register(
            "commission",
            _scope(str(instrument), profile, offset.value),
            rule,
            source_id,
            version,
            effective_from,
            available_at,
            effective_to,
            replaces,
        )

    def register_margin_rule(
        self,
        instrument: InstrumentId,
        profile: str,
        rule: MarginRule,
        source_id: str,
        version: str,
        effective_from: datetime,
        available_at: datetime,
        effective_to: datetime | None = None,
        *,
        replaces: tuple[str, str] | None = None,
    ) -> None:
        if not isinstance(instrument, InstrumentId) or not isinstance(rule, MarginRule):
            raise TypeError("margin registration requires a normalized instrument and rule")
        self._register(
            "margin",
            _scope(str(instrument), profile),
            rule,
            source_id,
            version,
            effective_from,
            available_at,
            effective_to,
            replaces,
        )

    def contract_rule(
        self, instrument: InstrumentId, profile: str, effective_at: datetime, known_at: datetime
    ) -> VersionedValue[ContractSpec]:
        return cast(
            VersionedValue[ContractSpec],
            self._find("contract", _scope(str(instrument), profile), effective_at, known_at),
        )

    def commission_rule(
        self,
        instrument: InstrumentId,
        profile: str,
        offset: Offset,
        effective_at: datetime,
        known_at: datetime,
    ) -> VersionedValue[CommissionRule]:
        require_enum(offset, Offset)
        return cast(
            VersionedValue[CommissionRule],
            self._find("commission", _scope(str(instrument), profile, offset.value), effective_at, known_at),
        )

    def margin_rule(
        self, instrument: InstrumentId, profile: str, effective_at: datetime, known_at: datetime
    ) -> VersionedValue[MarginRule]:
        return cast(
            VersionedValue[MarginRule], self._find("margin", _scope(str(instrument), profile), effective_at, known_at)
        )

    def register_capability(
        self,
        exchange: Exchange,
        product: ProductId,
        broker_profile: str,
        ctp_version: str,
        name: str,
        rule: Capability,
        source_id: str,
        version: str,
        effective_from: datetime,
        available_at: datetime,
        effective_to: datetime | None = None,
        *,
        replaces: tuple[str, str] | None = None,
    ) -> None:
        if product.exchange != exchange or not isinstance(rule, Capability):
            raise ValueError("capability scope and value must be normalized")
        scope = _scope(exchange.value, product.product, broker_profile, ctp_version, name)
        self._register(
            "capability", scope, rule, source_id, version, effective_from, available_at, effective_to, replaces
        )

    def capability(
        self,
        exchange: Exchange,
        product: ProductId,
        broker_profile: str,
        ctp_version: str,
        name: str,
        effective_at: datetime,
        known_at: datetime,
    ) -> VersionedValue[Capability] | None:
        if product.exchange != exchange:
            raise ValueError("product and capability exchange disagree")
        try:
            return cast(
                VersionedValue[Capability],
                self._find(
                    "capability",
                    _scope(exchange.value, product.product, broker_profile, ctp_version, name),
                    effective_at,
                    known_at,
                ),
            )
        except MissingRuleError:
            return None

    def register_sessions(
        self,
        instrument: InstrumentId,
        trading_day: date,
        sessions: Sequence[Session],
        source_id: str,
        version: str,
        effective_from: datetime,
        available_at: datetime,
        effective_to: datetime | None = None,
        *,
        replaces: tuple[str, str] | None = None,
    ) -> None:
        require_date(trading_day, "trading_day")
        rows = tuple(sorted(sessions, key=lambda row: row.start))
        for row in rows:
            if (row.instrument, row.trading_day, row.source_id, row.rule_version) != (
                instrument,
                trading_day,
                source_id,
                version,
            ):
                raise ValueError("session set and its provenance/scope disagree")
            if row.available_at > utc_timestamp(available_at):
                raise ValueError("a session set cannot be published before its contained records")
        if any(left.end > right.start for left, right in zip(rows, rows[1:], strict=False)):
            raise AmbiguousRuleError("overlapping sessions cannot be registered")
        self._register(
            "sessions",
            _scope(str(instrument), trading_day.isoformat()),
            rows,
            source_id,
            version,
            effective_from,
            available_at,
            effective_to,
            replaces,
        )

    def sessions(self, instrument: InstrumentId, trading_day: date, known_at: datetime) -> Sequence[Session]:
        require_date(trading_day, "trading_day")
        return cast(
            tuple[Session, ...],
            self._find("sessions", _scope(str(instrument), trading_day.isoformat()), None, known_at).value,
        )
