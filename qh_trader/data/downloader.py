"""Archive source observations, then publish only explicitly validated canonical datasets."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

from qh_trader.core.constants import Exchange, MissingRuleError, PriceType
from qh_trader.core.objects import Bar, InstrumentId, freeze_payload
from qh_trader.data.calendar import TradingCalendar
from qh_trader.data.contracts import ContractResolver
from qh_trader.data.execution_reference import derive_execution_references
from qh_trader.data.schemas import (
    BarTiming,
    DataQualityReport,
    DataValidationError,
    SettlementPublication,
    convert_daily_records_to_bars,
    convert_daily_records_to_settlements,
    convert_minute_records_to_bars,
    parse_day,
    parse_time,
    record_key,
    validate_ohlc_records,
)
from qh_trader.data.sources import BaseDataSource, create_data_source, normalize_instrument_to_symbol
from qh_trader.data.storage import ParquetDataStorage, normalize_interval


def parse_instrument(
    ident: InstrumentId | str,
    *,
    resolver: ContractResolver | None = None,
    as_of: date | None = None,
) -> InstrumentId:
    if resolver is not None:
        return resolver.resolve(str(ident), as_of=as_of)[0]
    if isinstance(ident, InstrumentId):
        normalize_instrument_to_symbol(ident)
        return ident
    if "." not in ident:
        raise ValueError("declare the exchange or supply a historical catalog; no exchange fallback is allowed")
    exchange, symbol = ident.split(".", 1)
    normalize_instrument_to_symbol(ident)
    return InstrumentId(Exchange(exchange.upper()), symbol)


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"unsupported raw archive value: {type(value).__name__}")


@dataclass(frozen=True)
class RawDownload:
    path: Path
    source_version: str
    records: tuple[Mapping[str, Any], ...]
    ingested_at: datetime
    quality: DataQualityReport

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", tuple(freeze_payload(row) for row in self.records))


class _BarImportOptions(TypedDict):
    timings: Mapping[str, BarTiming]
    calendar: TradingCalendar
    source_id: str
    source_version: str
    ingested_at: datetime


def load_import_metadata(path: Path | str) -> tuple[dict[str, BarTiming], dict[str, SettlementPublication]]:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if data.get("schema_version") != 1:
        raise ValueError("unsupported source timing metadata")
    timings = {}
    for key, record in data["bar_timings"].items():
        values = dict(record)
        values["trading_day"] = parse_day(values["trading_day"])
        for name in ("bar_start", "bar_end", "open_time", "available_at"):
            values[name] = parse_time(values[name])
        if values.get("open_available_at") is not None:
            values["open_available_at"] = parse_time(values["open_available_at"])
        values["price_types"] = tuple(PriceType(value) for value in values.get("price_types", []))
        timings[key] = BarTiming(**values)
    publications = {}
    for key, record in data.get("settlement_publications", {}).items():
        values = dict(record)
        for name in ("published_at", "available_at"):
            values[name] = parse_time(values[name])
        publications[key] = SettlementPublication(**values)
    return timings, publications


class FuturesDataDownloader:
    def __init__(
        self,
        data_source: BaseDataSource | str = "sina",
        storage: ParquetDataStorage | Path | str | None = None,
        *,
        resolver: ContractResolver | None = None,
        calendar: TradingCalendar | None = None,
        timings: Mapping[str, BarTiming] | None = None,
        publications: Mapping[str, SettlementPublication] | None = None,
        metadata_refs: Mapping[str, Any] | None = None,
    ) -> None:
        self.data_source = create_data_source(data_source) if isinstance(data_source, str) else data_source
        self.storage = (
            storage if isinstance(storage, ParquetDataStorage) else ParquetDataStorage(storage or "data_storage")
        )
        self.resolver = resolver
        self.calendar = calendar
        self.timings = dict(timings or {})
        self.publications = dict(publications or {})
        self.metadata_refs = dict(metadata_refs or {})

    def _archive(self, payload: dict) -> tuple[Path, str]:
        data = json.dumps(
            payload, default=_json_default, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        directory = self.storage.root_dir / "raw"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{digest}.json"
        if path.exists():
            if path.read_bytes() != data:
                raise ValueError("raw archive hash collision or corruption")
            return path, digest
        temporary = directory / f".tmp-{uuid4().hex}.json"
        try:
            with temporary.open("wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return path, digest

    def download_raw(
        self,
        instrument: InstrumentId | str,
        interval: str = "1d",
        start_date: date | str | None = None,
        end_date: date | str | None = None,
    ) -> RawDownload:
        interval = normalize_interval(interval)
        first = parse_day(start_date) if start_date is not None else None
        last = parse_day(end_date) if end_date is not None else None
        if first is not None and last is not None and first > last:
            raise ValueError("requested dates are reversed")
        inst = parse_instrument(instrument, resolver=self.resolver, as_of=first)
        capture_start = len(getattr(self.data_source, "captures", ()))
        imported = datetime.now(timezone.utc)
        payload = {
            "schema_version": 1,
            "source_id": self.data_source.source_id,
            "source_name": self.data_source.source_name,
            "instrument": str(inst),
            "interval": interval,
            "requested_start": first,
            "requested_end": last,
            "source_timezone": self.data_source.source_timezone,
            "ingested_at": imported,
            "status": "raw_observation",
            "records": [],
        }
        try:
            if interval == "1d":
                records = self.data_source.fetch_daily_bars(inst, start_date=first, end_date=last)
            else:
                period = "60" if interval == "1h" else interval.removesuffix("m")
                records = self.data_source.fetch_minute_bars(inst, period=period, start_time=first, end_time=last)
        except Exception as exc:
            payload["status"] = "parse_or_fetch_failed"
            payload["error_type"] = type(exc).__name__
            payload["captures"] = getattr(self.data_source, "captures", [])[capture_start:]
            self._archive(payload)
            raise
        payload["records"] = records
        payload["captures"] = getattr(self.data_source, "captures", [])[capture_start:]
        path, digest = self._archive(payload)
        time_key = "date" if interval == "1d" else "datetime"
        quality = validate_ohlc_records(
            records,
            time_key,
            strict=False,
            instrument=inst,
            source_timezone=self.data_source.source_timezone,
        )
        return RawDownload(path, digest, tuple(records), imported, quality)

    def download_bars(
        self,
        instrument: InstrumentId | str,
        interval: str = "1d",
        start_date: date | str | None = None,
        end_date: date | str | None = None,
        strict_quality: bool = True,
    ) -> tuple[Path, DataQualityReport, list[Bar]]:
        if self.resolver is None or self.calendar is None or not self.timings:
            raise MissingRuleError("canonical import needs a contract catalog, calendar and source timing evidence")
        interval = normalize_interval(interval)
        raw = self.download_raw(instrument, interval, start_date, end_date)
        if not raw.records:
            raise ValueError("source returned no observations")
        # Even diagnostic/lenient validation cannot label invalid records as canonical OK data.
        if not raw.quality.is_clean:
            raise DataValidationError(raw.quality)
        first = parse_day(start_date) if start_date is not None else None
        inst = parse_instrument(instrument, resolver=self.resolver, as_of=first)
        time_key = "date" if interval == "1d" else "datetime"
        selected_timings = {}
        for record in raw.records:
            key = record_key(record, time_key, self.data_source.source_timezone)
            if key not in self.timings:
                raise MissingRuleError(f"source timing evidence missing for {key}")
            timing = self.timings[key]
            resolved = self.resolver.resolve(str(inst), as_of=timing.trading_day)[0]
            if resolved != inst:
                raise ValueError("source record falls outside the actual contract lifetime")
            selected_timings[key] = timing
        kwargs = _BarImportOptions(
            timings=selected_timings,
            calendar=self.calendar,
            source_id=self.data_source.source_id,
            source_version=raw.source_version,
            ingested_at=raw.ingested_at,
        )
        if interval == "1d":
            bars = convert_daily_records_to_bars(raw.records, inst, **kwargs)
            settlements = convert_daily_records_to_settlements(
                raw.records,
                inst,
                publications=self.publications,
                source_id=self.data_source.source_id,
                source_version=raw.source_version,
                ingested_at=raw.ingested_at,
            )
        else:
            bars = convert_minute_records_to_bars(
                raw.records,
                inst,
                interval,
                source_timezone=self.data_source.source_timezone,
                **kwargs,
            )
            settlements = []
        references = derive_execution_references(bars, {item.bar_start: item for item in selected_timings.values()})
        provenance = {
            "raw": {"path": raw.path.relative_to(self.storage.root_dir).as_posix(), "sha256": raw.source_version},
            "catalog_version": self.resolver.catalog_version,
            "calendar_version": self.calendar.version,
            "timing_evidence": sorted({item.evidence_ref for item in selected_timings.values()}),
            "time_assumptions": sorted(
                {item.time_assumption for item in selected_timings.values() if item.time_assumption}
            ),
            "metadata_inputs": self.metadata_refs,
            "settlement_publication_evidence": sorted({item.evidence_ref for item in self.publications.values()}),
        }
        committed = self.storage.publish_batch(
            inst, interval, bars=bars, settlements=settlements, execution_references=references, provenance=provenance
        )
        return self.storage.get_bar_file_path(inst, interval, snapshot=committed), raw.quality, bars

    def download_batch(
        self,
        instruments: Sequence[InstrumentId | str],
        intervals: Sequence[str] = ("1d", "1h"),
        start_date=None,
        end_date=None,
    ) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for instrument in instruments:
            result = results.setdefault(str(instrument), {})
            for interval in intervals:
                try:
                    path, report, bars = self.download_bars(instrument, interval, start_date, end_date)
                    result[interval] = {
                        "status": "published",
                        "path": str(path),
                        "count": len(bars),
                        "valid_count": report.valid_count,
                    }
                except (ValueError, OSError, MissingRuleError) as exc:
                    result[interval] = {"status": "failed", "error": str(exc)}
        return results
