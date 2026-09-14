"""MarketDataPort over one pinned, hash-checked dataset snapshot."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from qh_trader.core.clock import utc_timestamp
from qh_trader.core.constants import AmbiguousRuleError, MarketPhase, PriceType
from qh_trader.core.objects import Bar, ExecutionReference, InstrumentId, SeriesId, Tick, VersionedValue
from qh_trader.data.snapshots import DataSnapshot
from qh_trader.data.storage import INTERVALS, ParquetDataStorage, normalize_interval


class HistoricalMarketDataAdapter:
    def __init__(
        self,
        storage: ParquetDataStorage | Path | str | None = None,
        preload: bool = False,
        *,
        snapshot: DataSnapshot | str | None = None,
        execution_interval: str | None = None,
    ) -> None:
        self.storage = (
            storage if isinstance(storage, ParquetDataStorage) else ParquetDataStorage(storage or "data_storage")
        )
        self.snapshot = snapshot if isinstance(snapshot, DataSnapshot) else self.storage.capture_snapshot(snapshot)
        self.execution_interval = normalize_interval(execution_interval) if execution_interval is not None else None
        self._subscribed: set[InstrumentId] = set()
        self._bar_cache: dict[tuple[InstrumentId, str], tuple[Bar, ...]] = {}
        self._reference_cache: dict[tuple[InstrumentId, str], tuple[ExecutionReference, ...]] = {}
        self.preload = preload

    def subscribe(self, instruments: Sequence[InstrumentId]) -> None:
        for instrument in instruments:
            if not isinstance(instrument, InstrumentId):
                raise TypeError("subscribe requires actual instruments")
            self._subscribed.add(instrument)
            if self.preload:
                for interval in sorted(INTERVALS):
                    self._load_cache(instrument, interval)

    def _load_cache(self, instrument: InstrumentId, interval: str) -> tuple[Bar, ...]:
        key = instrument, normalize_interval(interval)
        if key not in self._bar_cache:
            rows = self.storage.read_bars(instrument, key[1], snapshot=self.snapshot)
            self._bar_cache[key] = tuple(sorted(rows, key=lambda row: row.bar_end))
        return self._bar_cache[key]

    def _references(self, instrument: InstrumentId, interval: str) -> tuple[ExecutionReference, ...]:
        key = instrument, normalize_interval(interval)
        if key not in self._reference_cache:
            self._reference_cache[key] = tuple(
                self.storage.read_execution_references(instrument, key[1], snapshot=self.snapshot)
            )
        return self._reference_cache[key]

    def bars(self, instrument: InstrumentId | SeriesId, interval: str, until: datetime) -> Sequence[Bar]:
        cutoff = utc_timestamp(until)
        if not isinstance(instrument, InstrumentId):
            raise NotImplementedError("derived-series storage is not enabled")
        return tuple(row for row in self._load_cache(instrument, interval) if row.meta.available_at <= cutoff)

    def execution_reference(
        self,
        instrument: InstrumentId,
        session_id: str,
        reference_time: datetime,
        price_type: PriceType,
        known_at: datetime,
    ) -> ExecutionReference | None:
        requested, cutoff = utc_timestamp(reference_time), utc_timestamp(known_at)
        if cutoff < requested:
            return None
        intervals = (self.execution_interval,) if self.execution_interval is not None else sorted(INTERVALS)
        matches = [
            row
            for interval in intervals
            for row in self._references(instrument, interval)
            if row.reference_time == requested
            and row.session_id == session_id
            and row.price_type == price_type
            and row.meta.available_at <= cutoff
        ]
        if len(matches) > 1:
            raise AmbiguousRuleError(
                "multiple visible execution observations match; choose an explicit data resolution"
            )
        return matches[0] if matches else None

    def replay_schedule(
        self, instrument: InstrumentId, interval: str
    ) -> tuple[tuple[datetime, ...], tuple[tuple, ...]]:
        """Assembly metadata only. Strategies obtain prices through the visibility-limited port."""
        bars = self._load_cache(instrument, normalize_interval(interval))
        observations = tuple(sorted({row.meta.available_at for row in bars}))
        openings = tuple((row.open_time, row.meta.session_id, PriceType.BAR_OPEN) for row in bars)
        return observations, openings

    def latest_tick(self, instrument: InstrumentId, known_at: datetime) -> Tick | None:
        utc_timestamp(known_at)
        return None

    def instrument_status(
        self,
        instrument: InstrumentId,
        known_at: datetime,
    ) -> VersionedValue[MarketPhase] | None:
        utc_timestamp(known_at)
        return None
