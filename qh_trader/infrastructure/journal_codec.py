"""Deterministic, tagged JSON for normalized Core values; never import types named by stored data."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from qh_trader.core import constants, objects
from qh_trader.core.clock import utc_timestamp
from qh_trader.core.event import CanonicalEvent, JournalSnapshot, JournalTransaction, TimerEvent
from qh_trader.infrastructure.private_fields import contains_private_assignment, is_private_field

_VALUES = {
    name: getattr(objects, name)
    for name in (
        "InstrumentId",
        "ProductId",
        "SeriesId",
        "RecordMeta",
        "VersionedValue",
        "Bar",
        "ExecutionReference",
        "Tick",
        "Settlement",
        "Permissions",
        "Session",
        "ControlEpoch",
        "OrderIdentity",
        "OrderIntent",
        "LocalSendResult",
        "OrderUpdate",
        "TradeKey",
        "Trade",
        "Position",
        "ContractSpec",
        "CommissionRule",
        "MarginRule",
        "Capability",
        "CapabilityProfile",
        "ControlRecord",
        "QueryBatch",
        "QueryResult",
        "AccountFunds",
        "QueryRateLimit",
    )
}
_VALUES.update(
    CanonicalEvent=CanonicalEvent,
    JournalTransaction=JournalTransaction,
    JournalSnapshot=JournalSnapshot,
    TimerEvent=TimerEvent,
)
_ENUMS = {
    name: getattr(constants, name)
    for name in (
        "Exchange",
        "Side",
        "PositionSide",
        "Offset",
        "OrderType",
        "OrderStatus",
        "SendState",
        "MarketPhase",
        "ExecutionPolicy",
        "PriceType",
        "SeriesKind",
        "QualityFlag",
        "EventKind",
        "ReplayOrder",
    )
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _encode(value: Any, depth: int = 0) -> Any:
    if depth > 64:
        raise ValueError("journal payload nesting is too deep")
    if isinstance(value, Enum):
        name = type(value).__name__
        if _ENUMS.get(name) is not type(value):
            raise TypeError("journal enum type is not registered")
        return {"t": "enum", "name": name, "v": value.value}
    if value is None or type(value) in (str, bool, int):
        if isinstance(value, str) and contains_private_assignment(value):
            raise ValueError("credential assignments cannot be persisted in free text")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("journal decimals must be finite")
        return {"t": "decimal", "v": str(value)}
    if isinstance(value, datetime):
        return {"t": "datetime", "v": utc_timestamp(value).isoformat(timespec="microseconds")}
    if isinstance(value, date):
        return {"t": "date", "v": value.isoformat()}
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("journal floats must be finite")
        return {"t": "float", "v": value.hex()}
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("journal mapping keys must be strings")
        if any(is_private_field(key) for key in value):
            raise ValueError("credential or raw terminal fields cannot be persisted")
        return {"t": "map", "v": [[key, _encode(value[key], depth + 1)] for key in sorted(value)]}
    if isinstance(value, (list, tuple)):
        return {"t": "tuple", "v": [_encode(item, depth + 1) for item in value]}
    if isinstance(value, (set, frozenset)):
        encoded = [_encode(item, depth + 1) for item in value]
        return {"t": "set", "v": sorted(encoded, key=_json)}
    if is_dataclass(value) and not isinstance(value, type):
        name = type(value).__name__
        if _VALUES.get(name) is not type(value):
            raise TypeError("journal value type is not a registered Core contract")
        return {
            "t": "object",
            "name": name,
            "v": {field.name: _encode(getattr(value, field.name), depth + 1) for field in fields(value)},
        }
    raise TypeError("journal payloads must be normalized values; raw binary and gateway objects are forbidden")


def _decode(value: Any, depth: int = 0) -> Any:
    if depth > 64:
        raise ValueError("journal payload nesting is too deep")
    if value is None or type(value) in (str, bool, int):
        return value
    if not isinstance(value, dict):
        raise ValueError("invalid tagged journal value")
    tag = value.get("t")
    if tag == "decimal":
        number = Decimal(value["v"])
        if not number.is_finite():
            raise ValueError("non-finite journal decimal")
        return number
    if tag == "float":
        floating = float.fromhex(value["v"])
        if not math.isfinite(floating):
            raise ValueError("non-finite journal float")
        return floating
    if tag == "datetime":
        return utc_timestamp(datetime.fromisoformat(value["v"]))
    if tag == "date":
        return date.fromisoformat(value["v"])
    if tag == "enum":
        name = value.get("name")
        enum = _ENUMS.get(name) if isinstance(name, str) else None
        if enum is None:
            raise ValueError("unknown journal enum type")
        return enum(value["v"])
    if tag == "map":
        pairs = value["v"]
        if not isinstance(pairs, list) or any(not isinstance(pair, list) or len(pair) != 2 for pair in pairs):
            raise ValueError("invalid journal mapping")
        keys = [pair[0] for pair in pairs]
        if any(not isinstance(key, str) or is_private_field(key) for key in keys) or len(set(keys)) != len(keys):
            raise ValueError("invalid or private journal mapping keys")
        return objects.freeze_payload({key: _decode(item, depth + 1) for key, item in pairs})
    if tag in {"tuple", "set"}:
        items = [_decode(item, depth + 1) for item in value["v"]]
        return tuple(items) if tag == "tuple" else frozenset(items)
    if tag == "object":
        name = value.get("name")
        cls = _VALUES.get(name) if isinstance(name, str) else None
        if cls is None or not isinstance(value.get("v"), dict):
            raise ValueError("unknown journal contract")
        members = value["v"]
        if set(members) != {field.name for field in fields(cls)}:
            raise ValueError("journal contract fields differ from its schema")
        return cls(**{key: _decode(item, depth + 1) for key, item in members.items()})
    raise ValueError("unknown journal value tag")


def dumps(value: Any) -> str:
    return _json({"schema_version": 1, "value": _encode(value)})


def loads(payload: str) -> Any:
    document = json.loads(payload)
    if (
        not isinstance(document, dict)
        or type(document.get("schema_version")) is not int
        or document["schema_version"] != 1
    ):
        raise ValueError("unsupported journal serialization schema")
    result = _decode(document["value"])
    # Re-encoding also applies all persistence restrictions to decoded contracts.
    _encode(result)
    return result
