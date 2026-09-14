"""Synthetic input evidence for data/research tests; not real exchange rule verification."""

from datetime import date, datetime, time, timezone
from decimal import Decimal

import pytest

from qh_trader.core.constants import Exchange, MarketPhase, PriceType
from qh_trader.core.objects import ContractSpec, InstrumentId, Permissions, ProductId, Session
from qh_trader.data.calendar import CHINA_TZ, TradingCalendar
from qh_trader.data.contracts import CatalogEntry, ContractResolver
from qh_trader.data.schemas import BarTiming, convert_daily_records_to_bars


@pytest.fixture
def sample_instrument():
    return InstrumentId(Exchange.SHFE, "rb2410")


@pytest.fixture
def sample_catalog(sample_instrument):
    entry = CatalogEntry(
        spec=ContractSpec(
            instrument=sample_instrument,
            product=ProductId(Exchange.SHFE, "rb"),
            delivery_year=2024,
            delivery_month=10,
            multiplier=Decimal(10),
            price_tick=Decimal(1),
            listed_on=date(2024, 1, 1),
            last_trading_day=date(2024, 10, 15),
        ),
        aliases=("rb2410",),
        source_id="synthetic-catalog",
        available_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    return ContractResolver([entry], catalog_version="synthetic-v1")


@pytest.fixture
def sample_calendar(sample_instrument):
    sessions = []
    days = [date(2024, 9, day) for day in range(9, 14)]
    available = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for day in days:
        for name, start, end, phase, permissions in (
            ("morning1", time(9), time(10, 15), MarketPhase.CONTINUOUS, Permissions(True, True, True)),
            ("break", time(10, 15), time(10, 30), MarketPhase.BREAK, Permissions(False, False, False)),
            ("morning2", time(10, 30), time(11, 30), MarketPhase.CONTINUOUS, Permissions(True, True, True)),
            ("lunch", time(11, 30), time(13, 30), MarketPhase.BREAK, Permissions(False, False, False)),
            ("afternoon", time(13, 30), time(15), MarketPhase.CONTINUOUS, Permissions(True, True, True)),
        ):
            sessions.append(
                Session(
                    instrument=sample_instrument,
                    session_id=name,
                    trading_day=day,
                    start=datetime.combine(day, start, CHINA_TZ),
                    end=datetime.combine(day, end, CHINA_TZ),
                    phase=phase,
                    permissions=permissions,
                    rule_version="synthetic-v1",
                    source_id="synthetic-calendar",
                    available_at=available,
                )
            )
    return TradingCalendar(
        sessions,
        trading_days=days,
        coverage_start=date(2024, 9, 6),
        coverage_end=date(2024, 9, 15),
        version="synthetic-v1",
        source_id="synthetic-calendar",
        available_at=available,
    )


@pytest.fixture
def source_records():
    return [
        dict(
            date=f"2024-09-{day:02}",
            open=str(price),
            high=str(price),
            low=str(price),
            close=str(price),
            volume=10,
            open_interest=100,
            turnover=str(price * 100),
        )
        for day, price in zip(range(9, 14), (100, 110, 120, 90, 80), strict=True)
    ]


@pytest.fixture
def timing_rows(source_records):
    result = {}
    for record in source_records:
        day = date.fromisoformat(record["date"])
        start = datetime.combine(day, time(9), CHINA_TZ)
        end = datetime.combine(day, time(15), CHINA_TZ)
        result[record["date"]] = BarTiming(
            trading_day=day,
            bar_start=start,
            bar_end=end,
            open_time=start,
            available_at=end,
            session_id="morning1",
            includes_auction=False,
            evidence_ref="synthetic/timing",
            open_available_at=start,
            price_types=(PriceType.BAR_OPEN,),
            time_assumption="synthetic test timing",
        )
    return result


@pytest.fixture
def sample_bars(sample_instrument, source_records, timing_rows, sample_calendar):
    return convert_daily_records_to_bars(
        source_records,
        sample_instrument,
        timings=timing_rows,
        calendar=sample_calendar,
        source_id="synthetic-source",
        source_version="test-source-v1",
        ingested_at=datetime(2026, 9, 14, tzinfo=timezone.utc),
    )
