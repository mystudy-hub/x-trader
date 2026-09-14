"""Derive exact execution observations only when their timing and price meaning are explicit."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime

from qh_trader.core.objects import Bar, ExecutionReference, InstrumentId
from qh_trader.data.schemas import BarTiming


def derive_execution_references(
    bars: Sequence[Bar],
    timings: Mapping[datetime, BarTiming],
) -> list[ExecutionReference]:
    result = []
    for bar in bars:
        if not isinstance(bar.instrument, InstrumentId):
            raise TypeError("execution observations require actual instruments")
        timing = timings.get(bar.bar_start)
        if timing is None or timing.open_available_at is None:
            continue
        if (timing.open_time, timing.session_id) != (bar.open_time, bar.meta.session_id):
            raise ValueError("execution timing must describe the source bar's actual opening observation")
        for price_type in timing.price_types:
            meta = replace(bar.meta, event_time=bar.open_time, available_at=timing.open_available_at)
            result.append(
                ExecutionReference(
                    instrument=bar.instrument,
                    meta=meta,
                    session_id=timing.session_id,
                    reference_time=bar.open_time,
                    price_type=price_type,
                    price=bar.open,
                    source_record_id=f"{bar.meta.source_id}:{bar.meta.source_version}:{bar.meta.ingest_seq}",
                    resolution=bar.interval,
                    # Period volume does not establish volume available at its opening instant.
                    available_volume=None,
                )
            )
    return sorted(result, key=lambda item: (item.reference_time, item.session_id, item.price_type.value))
