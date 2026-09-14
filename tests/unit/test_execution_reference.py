"""ExecutionReference 派生单元测试 (S1-06, FR-EXEC-02, 03, A21)."""

from datetime import date, datetime, timezone
from decimal import Decimal

from qh_trader.core.constants import Exchange, PriceType, QualityFlag
from qh_trader.core.objects import Bar, InstrumentId, RecordMeta
from qh_trader.data.execution_reference import derive_execution_references
from qh_trader.data.schemas import BarTiming


def test_derive_execution_references():
    inst = InstrumentId(Exchange.SHFE, "rb2410")
    t_day = date(2024, 10, 14)
    open_t = datetime(2024, 10, 14, 1, 0, 0, tzinfo=timezone.utc)
    end_t = datetime(2024, 10, 14, 7, 0, 0, tzinfo=timezone.utc)

    meta = RecordMeta(
        event_time=end_t,
        available_at=end_t,
        ingested_at=end_t,
        trading_day=t_day,
        source_id="test",
        source_version="1.0",
        ingest_seq=1,
        session_id="day_continuous",
        quality_flags=QualityFlag.OK,
    )

    bar = Bar(
        instrument=inst,
        meta=meta,
        bar_start=open_t,
        bar_end=end_t,
        interval="1d",
        open=Decimal("3320"),
        high=Decimal("3440"),
        low=Decimal("3320"),
        close=Decimal("3420"),
        volume=1950,
        turnover=Decimal("64680000"),
        open_interest=4410,
        open_time=open_t,
        includes_auction=False,
    )

    timing = BarTiming(
        trading_day=t_day,
        bar_start=open_t,
        bar_end=end_t,
        open_time=open_t,
        available_at=end_t,
        session_id="day_continuous",
        includes_auction=False,
        evidence_ref="exchange_notice",
        open_available_at=open_t,
        price_types=(PriceType.BAR_OPEN, PriceType.SESSION_OPEN),
    )

    refs = derive_execution_references([bar], {bar.bar_start: timing})
    assert len(refs) == 2
    assert refs[0].price == Decimal("3320")
    assert refs[0].reference_time == open_t
    assert refs[0].available_volume is None  # 不冒充成交量
