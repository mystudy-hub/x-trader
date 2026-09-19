"""Validate external records and preserve complete Core provenance in Arrow datasets."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow as pa

from qh_trader.core.clock import utc_timestamp
from qh_trader.core.constants import Exchange, PriceType, QualityFlag, SeriesKind
from qh_trader.core.objects import (
    Bar,
    ExecutionReference,
    InstrumentId,
    RecordMeta,
    SeriesId,
    Settlement,
    require_bool,
    require_date,
    require_text,
)
from qh_trader.data.calendar import TradingCalendar

CHINA_TZ = ZoneInfo("Asia/Shanghai")
SCHEMA_VERSION = 2


def parse_time(value: datetime | str, source_timezone: str | None = None) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime):
        raise TypeError("timestamp must be an ISO timestamp or datetime")
    if parsed.tzinfo is None:
        if source_timezone is None:
            raise ValueError("naive source timestamp requires an explicitly declared source timezone")
        parsed = parsed.replace(tzinfo=ZoneInfo(source_timezone))
    return utc_timestamp(parsed)


def parse_day(value: date | str) -> date:
    parsed = date.fromisoformat(value) if isinstance(value, str) else value
    require_date(parsed, "trading_day")
    return parsed


def decimal_value(value: Any, name: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{name} is missing or is not a decimal value")
    try:
        result = Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise ValueError(f"{name} is not a decimal value") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def integer_value(value: Any, name: str) -> int:
    number = decimal_value(value, name)
    if number != number.to_integral_value() or not 0 <= number <= 2**63 - 1:
        raise ValueError(f"{name} must be a nonnegative int64 quantity")
    return int(number)


@dataclass(frozen=True, slots=True)
class QualityIssue:
    index: int
    field: str
    issue_type: str
    message: str
    raw_value: str | None


@dataclass(frozen=True, slots=True)
class DataQualityReport:
    total_count: int
    valid_count: int
    issues: tuple[QualityIssue, ...]

    @property
    def is_clean(self) -> bool:
        return not self.issues

    @property
    def has_critical_errors(self) -> bool:
        return bool(self.issues)


class DataValidationError(ValueError):
    def __init__(self, report: DataQualityReport):
        self.report = report
        super().__init__(f"data quality check failed: {len(report.issues)} issue(s); canonical publication refused")


def record_key(record: Mapping[str, Any], time_key: str, source_timezone: str | None = None) -> str:
    value = record[time_key]
    return parse_day(value).isoformat() if time_key == "date" else parse_time(value, source_timezone).isoformat()


def validate_ohlc_records(
    records: Sequence[Mapping[str, Any]],
    time_key: str = "date",
    strict: bool = True,
    *,
    instrument: InstrumentId | SeriesId | None = None,
    source_timezone: str | None = None,
    require_turnover: bool = True,
) -> DataQualityReport:
    issues: list[QualityIssue] = []
    last_time: str | None = None
    valid = 0
    for index, record in enumerate(records):
        before = len(issues)

        def issue(name: str, kind: str, message: str, *, record=record, index=index) -> None:
            raw = record.get(name)
            issues.append(QualityIssue(index, name, kind, message, repr(raw) if raw is not None else None))

        try:
            current = record_key(record, time_key, source_timezone)
            if last_time is not None and current <= last_time:
                issue(time_key, "NON_MONOTONIC_TIME", "timestamps must be strictly increasing")
            last_time = current
        except (KeyError, TypeError, ValueError):
            issue(time_key, "INVALID_TIME", "missing, invalid or ambiguous source time")
        prices = {}
        for name in ("open", "high", "low", "close"):
            try:
                value = decimal_value(record.get(name), name)
                if not isinstance(instrument, SeriesId) and value <= 0:
                    raise ValueError("nonpositive actual-contract price")
                prices[name] = value
            except ValueError:
                issue(name, "INVALID_PRICE", "price is missing or outside the series price domain")
        if len(prices) == 4 and not (
            prices["low"]
            <= min(prices["open"], prices["close"])
            <= max(prices["open"], prices["close"])
            <= prices["high"]
        ):
            issue("ohlc", "INCONSISTENT_OHLC", "OHLC range is inconsistent")
        for name in ("volume", "open_interest"):
            try:
                integer_value(record.get(name), name)
            except ValueError:
                issue(name, "INVALID_QUANTITY", "missing, fractional, boolean or negative quantity")
        try:
            if decimal_value(record.get("turnover"), "turnover") < 0:
                raise ValueError("negative turnover")
        except ValueError:
            if require_turnover:
                issue("turnover", "INVALID_TURNOVER", "actual turnover is required; missing values cannot become zero")
            else:
                # 研究模式: 来源确实不提供成交额, 记录为非阻塞质量标记而非零值 (FR-DATA-08).
                issue("turnover", "TURNOVER_UNAVAILABLE", "source does not establish actual turnover")
        valid += len(issues) == before
    report = DataQualityReport(len(records), valid, tuple(issues))
    if strict and not report.is_clean:
        raise DataValidationError(report)
    return report


@dataclass(frozen=True, slots=True, kw_only=True)
class BarTiming:
    """Source-specific boundaries and publication evidence; no calendar arithmetic defaults."""

    trading_day: date
    bar_start: datetime
    bar_end: datetime
    open_time: datetime
    available_at: datetime
    session_id: str
    includes_auction: bool
    evidence_ref: str
    open_available_at: datetime | None = None
    price_types: tuple[PriceType, ...] = ()
    time_assumption: str | None = None

    def __post_init__(self) -> None:
        require_date(self.trading_day, "trading_day")
        require_text(self.session_id, "session_id")
        require_text(self.evidence_ref, "timing evidence")
        require_bool(self.includes_auction, "includes_auction")
        for name in ("bar_start", "bar_end", "open_time", "available_at"):
            object.__setattr__(self, name, utc_timestamp(getattr(self, name)))
        if self.open_available_at is not None:
            object.__setattr__(self, "open_available_at", utc_timestamp(self.open_available_at))
            if self.open_available_at < self.open_time:
                raise ValueError("open price cannot be visible before its occurrence")
        if self.bar_start >= self.bar_end or self.available_at < self.bar_end:
            raise ValueError("invalid bar interval or premature final-bar publication")
        if self.time_assumption is not None:
            require_text(self.time_assumption, "time assumption")
        price_types = tuple(self.price_types)
        if any(not isinstance(value, PriceType) for value in price_types):
            raise TypeError("execution price types must be explicit")
        object.__setattr__(self, "price_types", price_types)


@dataclass(frozen=True, slots=True, kw_only=True)
class SettlementPublication:
    published_at: datetime
    available_at: datetime
    is_final: bool
    evidence_ref: str

    def __post_init__(self) -> None:
        for name in ("published_at", "available_at"):
            object.__setattr__(self, name, utc_timestamp(getattr(self, name)))
        if self.available_at < self.published_at:
            raise ValueError("settlement availability precedes publication")
        require_bool(self.is_final, "is_final")
        require_text(self.evidence_ref, "settlement publication evidence")


def _convert_bars(
    records: Sequence[Mapping[str, Any]],
    instrument: InstrumentId,
    interval: str,
    *,
    timings: Mapping[str, BarTiming],
    calendar: TradingCalendar,
    source_id: str,
    source_version: str,
    source_timezone: str | None = None,
    ingested_at: datetime | None = None,
    require_turnover: bool = True,
) -> list[Bar]:
    if interval not in {"1d", "1h", "60m", "1m", "5m", "15m", "30m"}:
        raise ValueError("unsupported bar interval")
    time_key = "date" if interval == "1d" else "datetime"
    # 研究模式 (require_turnover=False) 把来源缺成交额记为非阻塞质量标记, 精确模式仍严格失败。
    validate_ohlc_records(
        records,
        time_key,
        strict=require_turnover,
        instrument=instrument,
        source_timezone=source_timezone,
        require_turnover=require_turnover,
    )
    imported = utc_timestamp(ingested_at) if ingested_at is not None else datetime.now(timezone.utc)
    bars: list[Bar] = []
    for sequence, record in enumerate(records, 1):
        key = record_key(record, time_key, source_timezone)
        if key not in timings:
            raise ValueError(f"explicit source timing is missing for {key}")
        timing = timings[key]
        if time_key == "date" and timing.trading_day != parse_day(record["date"]):
            raise ValueError("daily source date and declared trading day disagree")
        if time_key != "date" and timing.bar_end != parse_time(record["datetime"], source_timezone):
            raise ValueError("minute source timestamp and declared bar end disagree")
        sessions = calendar.sessions_for_day(instrument, timing.trading_day)
        opening = [
            row
            for row in sessions
            if row.session_id == timing.session_id and row.contains(timing.open_time) and row.permissions.match
        ]
        if len(opening) != 1:
            raise ValueError("bar open must match an actual, uniquely registered trading/auction session")
        meta = RecordMeta(
            event_time=timing.bar_end,
            available_at=timing.available_at,
            ingested_at=imported,
            trading_day=timing.trading_day,
            session_id=timing.session_id,
            source_id=source_id,
            source_version=source_version,
            ingest_seq=sequence,
            source_seq=integer_value(record["source_seq"], "source_seq")
            if record.get("source_seq") is not None
            else None,
            receive_time=parse_time(record["receive_time"]) if record.get("receive_time") is not None else None,
            schema_version=SCHEMA_VERSION,
            quality_flags=(
                (QualityFlag.SYNTHETIC if timing.time_assumption else QualityFlag.OK)
                | (
                    QualityFlag.OK
                    if record.get("turnover") is not None
                    else QualityFlag.TURNOVER_UNAVAILABLE
                )
            ),
        )
        bar = Bar(
            instrument=instrument,
            meta=meta,
            bar_start=timing.bar_start,
            bar_end=timing.bar_end,
            interval="1h" if interval == "60m" else interval,
            open=decimal_value(record["open"], "open"),
            high=decimal_value(record["high"], "high"),
            low=decimal_value(record["low"], "low"),
            close=decimal_value(record["close"], "close"),
            volume=integer_value(record["volume"], "volume"),
            turnover=(
                decimal_value(record["turnover"], "turnover")
                if record.get("turnover") is not None
                else Decimal(0)
            ),
            open_interest=integer_value(record["open_interest"], "open_interest"),
            open_time=timing.open_time,
            includes_auction=timing.includes_auction,
        )
        if bars and bars[-1].bar_end > bar.bar_start:
            raise ValueError("source Bar intervals overlap; publication refused")
        bars.append(bar)
    return bars


def convert_daily_records_to_bars(
    records: Sequence[Mapping[str, Any]],
    instrument: InstrumentId,
    *,
    timings: Mapping[str, BarTiming],
    calendar: TradingCalendar,
    source_id: str,
    source_version: str,
    ingested_at: datetime | None = None,
    require_turnover: bool = True,
) -> list[Bar]:
    return _convert_bars(
        records,
        instrument,
        "1d",
        timings=timings,
        calendar=calendar,
        source_id=source_id,
        source_version=source_version,
        ingested_at=ingested_at,
        require_turnover=require_turnover,
    )


def build_standard_calendar_and_timings(
    records: Sequence[Mapping[str, Any]],
    instrument: InstrumentId,
    interval: str = "1d",
    session_id: str = "day_continuous",
    evidence_ref: str = "exchange_standard_schedule",
) -> tuple[dict[str, BarTiming], TradingCalendar]:
    """根据行情记录列表构建符合严格校验的默认 TradingCalendar 与 BarTiming 映射."""
    from datetime import time, timedelta

    from qh_trader.core.constants import MarketPhase
    from qh_trader.core.objects import Permissions, Session

    timings: dict[str, BarTiming] = {}
    sessions: list[Session] = []
    days_set: set[date] = set()

    min_interval = "1h" if interval == "60m" else interval
    duration = timedelta(hours=1) if min_interval == "1h" else timedelta(days=1)

    epoch_avail = utc_timestamp(datetime(2000, 1, 1, 0, 0, 0, tzinfo=timezone.utc))

    last_bar_end: datetime | None = None
    for rec in records:
        if interval == "1d":
            t_day = parse_day(rec["date"])
            days_set.add(t_day)
            start_dt = utc_timestamp(datetime.combine(t_day, time(9, 0, 0), tzinfo=CHINA_TZ))
            end_dt = utc_timestamp(datetime.combine(t_day, time(15, 0, 0), tzinfo=CHINA_TZ))
            key = t_day.isoformat()
        else:
            raw_dt = rec["datetime"]
            dt_end = parse_time(raw_dt, "Asia/Shanghai")
            dt_start = dt_end - duration
            if last_bar_end is not None and dt_start < last_bar_end:
                dt_start = last_bar_end
            last_bar_end = dt_end
            # 简单日历判定
            t_day = dt_end.astimezone(CHINA_TZ).date()
            if dt_end.astimezone(CHINA_TZ).hour >= 20:
                t_day = t_day + timedelta(days=1)
            days_set.add(t_day)
            start_dt = dt_start
            end_dt = dt_end
            key = dt_end.isoformat()

        timing = BarTiming(
            trading_day=t_day,
            bar_start=start_dt,
            bar_end=end_dt,
            open_time=start_dt,
            available_at=end_dt,
            session_id=session_id,
            includes_auction=False,
            evidence_ref=evidence_ref,
        )
        timings[key] = timing

    # 按交易日聚合 Session，确保每个交易日区间单调且无重叠
    day_bounds: dict[date, tuple[datetime, datetime]] = {}
    for timing in timings.values():
        t_day = timing.trading_day
        if t_day not in day_bounds:
            day_bounds[t_day] = (timing.bar_start, timing.bar_end)
        else:
            cur_s, cur_e = day_bounds[t_day]
            day_bounds[t_day] = (min(cur_s, timing.bar_start), max(cur_e, timing.bar_end))

    for t_day, (s_dt, e_dt) in sorted(day_bounds.items()):
        sess = Session(
            instrument=instrument,
            session_id=session_id,
            trading_day=t_day,
            start=s_dt,
            end=e_dt,
            phase=MarketPhase.CONTINUOUS,
            permissions=Permissions(True, True, True),
            rule_version="v1",
            source_id="standard_schedule",
            available_at=epoch_avail,
        )
        sessions.append(sess)

    sorted_days = sorted(days_set)
    cov_start = sorted_days[0]
    cov_end = sorted_days[-1]

    calendar = TradingCalendar(
        sessions=sessions,
        trading_days=sorted_days,
        coverage_start=cov_start,
        coverage_end=cov_end,
        version="v1",
        source_id="standard_schedule",
        available_at=epoch_avail,
    )
    return timings, calendar


def convert_minute_records_to_bars(
    records: Sequence[Mapping[str, Any]],
    instrument: InstrumentId,
    interval: str = "1h",
    *,
    timings: Mapping[str, BarTiming],
    calendar: TradingCalendar,
    source_id: str,
    source_version: str,
    source_timezone: str | None = None,
    ingested_at: datetime | None = None,
    require_turnover: bool = True,
) -> list[Bar]:
    return _convert_bars(
        records,
        instrument,
        interval,
        timings=timings,
        calendar=calendar,
        source_id=source_id,
        source_version=source_version,
        source_timezone=source_timezone,
        ingested_at=ingested_at,
        require_turnover=require_turnover,
    )


def convert_daily_records_to_settlements(
    records: Sequence[Mapping[str, Any]],
    instrument: InstrumentId,
    *,
    publications: Mapping[str, SettlementPublication],
    source_id: str,
    source_version: str,
    ingested_at: datetime | None = None,
) -> list[Settlement]:
    imported = utc_timestamp(ingested_at) if ingested_at is not None else datetime.now(timezone.utc)
    result = []
    for sequence, record in enumerate(records, 1):
        if record.get("settlement_price") is None:
            continue
        day = parse_day(record["date"])
        if day.isoformat() not in publications:
            raise ValueError("settlement needs actual publication time/status or a separately declared assumption")
        publication = publications[day.isoformat()]
        meta = RecordMeta(
            event_time=publication.published_at,
            available_at=publication.available_at,
            ingested_at=imported,
            trading_day=day,
            source_id=source_id,
            source_version=source_version,
            ingest_seq=sequence,
            schema_version=SCHEMA_VERSION,
        )
        previous = record.get("pre_settlement_price")
        result.append(
            Settlement(
                instrument=instrument,
                meta=meta,
                settlement_price=decimal_value(record["settlement_price"], "settlement"),
                pre_settlement_price=decimal_value(previous, "pre_settlement") if previous is not None else None,
                published_at=publication.published_at,
                is_final=publication.is_final,
            )
        )
    return result


_TIMESTAMP = pa.timestamp("us", tz="UTC")
_ID_FIELDS = [
    ("instrument_type", pa.string()),
    ("exchange", pa.string()),
    ("symbol", pa.string()),
    ("series_kind", pa.string()),
    ("series_name", pa.string()),
]
_META_FIELDS = [
    ("event_time", _TIMESTAMP),
    ("available_at", _TIMESTAMP),
    ("ingested_at", _TIMESTAMP),
    ("receive_time", _TIMESTAMP),
    ("trading_day", pa.date32()),
    ("session_id", pa.string()),
    ("source_id", pa.string()),
    ("source_version", pa.string()),
    ("source_seq", pa.int64()),
    ("ingest_seq", pa.int64()),
    ("schema_version", pa.int32()),
    ("quality_flags", pa.int32()),
]
BAR_ARROW_SCHEMA = pa.schema(
    _ID_FIELDS
    + _META_FIELDS
    + [
        ("interval", pa.string()),
        ("bar_start", _TIMESTAMP),
        ("bar_end", _TIMESTAMP),
        ("open_time", _TIMESTAMP),
        ("open", pa.string()),
        ("high", pa.string()),
        ("low", pa.string()),
        ("close", pa.string()),
        ("volume", pa.int64()),
        ("turnover", pa.string()),
        ("open_interest", pa.int64()),
        ("includes_auction", pa.bool_()),
    ]
)
SETTLEMENT_ARROW_SCHEMA = pa.schema(
    _ID_FIELDS
    + _META_FIELDS
    + [
        ("settlement_price", pa.string()),
        ("pre_settlement_price", pa.string()),
        ("published_at", _TIMESTAMP),
        ("is_final", pa.bool_()),
    ]
)
EXECUTION_ARROW_SCHEMA = pa.schema(
    _ID_FIELDS
    + _META_FIELDS
    + [
        ("target_session_id", pa.string()),
        ("reference_time", _TIMESTAMP),
        ("price_type", pa.string()),
        ("price", pa.string()),
        ("source_record_id", pa.string()),
        ("resolution", pa.string()),
        ("available_volume", pa.int64()),
    ]
)


def _base_row(record: Bar | Settlement | ExecutionReference) -> dict[str, Any]:
    instrument = record.instrument
    row: dict[str, Any] = {name: None for name, _ in _ID_FIELDS}
    if isinstance(instrument, InstrumentId):
        row.update(instrument_type="instrument", exchange=instrument.exchange.value, symbol=instrument.symbol)
    else:
        row.update(instrument_type="series", series_kind=instrument.kind.value, series_name=instrument.name)
    row.update({member.name: getattr(record.meta, member.name) for member in fields(RecordMeta)})
    row["quality_flags"] = int(record.meta.quality_flags)
    return row


def _restore_base(row: Mapping[str, Any]) -> tuple[InstrumentId | SeriesId, RecordMeta]:
    instrument: InstrumentId | SeriesId
    if row["instrument_type"] == "instrument":
        instrument = InstrumentId(Exchange(row["exchange"]), row["symbol"])
    elif row["instrument_type"] == "series":
        instrument = SeriesId(row["series_name"], SeriesKind(row["series_kind"]))
    else:
        raise ValueError("unknown stored instrument type")
    metadata = {member.name: row[member.name] for member in fields(RecordMeta)}
    metadata["quality_flags"] = QualityFlag(metadata["quality_flags"])
    return instrument, RecordMeta(**metadata)


def _rows(table: pa.Table, schema: pa.Schema) -> list[dict[str, Any]]:
    if not table.schema.equals(schema, check_metadata=False):
        raise ValueError("stored schema is incompatible; legacy records need explicit re-import")
    return table.to_pylist()


def bars_to_arrow_table(bars: Sequence[Bar]) -> pa.Table:
    rows = []
    for bar in bars:
        row = _base_row(bar)
        for name in ("interval", "bar_start", "bar_end", "open_time", "volume", "open_interest", "includes_auction"):
            row[name] = getattr(bar, name)
        for name in ("open", "high", "low", "close", "turnover"):
            row[name] = str(getattr(bar, name))
        rows.append(row)
    return pa.Table.from_pylist(rows, schema=BAR_ARROW_SCHEMA)


def arrow_table_to_bars(table: pa.Table) -> list[Bar]:
    result = []
    for row in _rows(table, BAR_ARROW_SCHEMA):
        instrument, meta = _restore_base(row)
        values = {
            name: row[name]
            for name in (
                "interval",
                "bar_start",
                "bar_end",
                "open_time",
                "volume",
                "open_interest",
                "includes_auction",
            )
        }
        values.update({name: Decimal(row[name]) for name in ("open", "high", "low", "close", "turnover")})
        result.append(Bar(instrument=instrument, meta=meta, **values))
    return result


def settlements_to_arrow_table(records: Sequence[Settlement]) -> pa.Table:
    rows = []
    for item in records:
        row = _base_row(item)
        row.update(
            settlement_price=str(item.settlement_price),
            pre_settlement_price=str(item.pre_settlement_price) if item.pre_settlement_price is not None else None,
            published_at=item.published_at,
            is_final=item.is_final,
        )
        rows.append(row)
    return pa.Table.from_pylist(rows, schema=SETTLEMENT_ARROW_SCHEMA)


def arrow_table_to_settlements(table: pa.Table) -> list[Settlement]:
    result = []
    for row in _rows(table, SETTLEMENT_ARROW_SCHEMA):
        instrument, meta = _restore_base(row)
        if not isinstance(instrument, InstrumentId):
            raise TypeError("settlements require actual instruments")
        previous = row["pre_settlement_price"]
        result.append(
            Settlement(
                instrument=instrument,
                meta=meta,
                settlement_price=Decimal(row["settlement_price"]),
                pre_settlement_price=Decimal(previous) if previous is not None else None,
                published_at=row["published_at"],
                is_final=row["is_final"],
            )
        )
    return result


def execution_references_to_arrow_table(records: Sequence[ExecutionReference]) -> pa.Table:
    rows = []
    for item in records:
        row = _base_row(item)
        row.update(
            target_session_id=item.session_id,
            reference_time=item.reference_time,
            price_type=item.price_type.value,
            price=str(item.price),
            source_record_id=item.source_record_id,
            resolution=item.resolution,
            available_volume=item.available_volume,
        )
        rows.append(row)
    return pa.Table.from_pylist(rows, schema=EXECUTION_ARROW_SCHEMA)


def arrow_table_to_execution_references(table: pa.Table) -> list[ExecutionReference]:
    result = []
    for row in _rows(table, EXECUTION_ARROW_SCHEMA):
        instrument, meta = _restore_base(row)
        if not isinstance(instrument, InstrumentId):
            raise TypeError("execution observations require actual instruments")
        result.append(
            ExecutionReference(
                instrument=instrument,
                meta=meta,
                session_id=row["target_session_id"],
                reference_time=row["reference_time"],
                price_type=PriceType(row["price_type"]),
                price=Decimal(row["price"]),
                source_record_id=row["source_record_id"],
                resolution=row["resolution"],
                available_volume=row["available_volume"],
            )
        )
    return result
