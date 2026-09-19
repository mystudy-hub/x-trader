"""Unit tests for dominant contract resolver (S4-01, FR-CON-02, FR-CON-04)."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from qh_trader.core.constants import Exchange, QualityFlag
from qh_trader.core.objects import Bar, InstrumentId, ProductId, RecordMeta
from qh_trader.data.dominant_contract import (
    DominantContractResolver,
    build_dominant_mappings,
)

PROD_RB = ProductId(Exchange.SHFE, "rb")
RB2410 = InstrumentId(Exchange.SHFE, "rb2410")
RB2501 = InstrumentId(Exchange.SHFE, "rb2501")
BASE_TIME = datetime(2024, 8, 1, 0, 0, tzinfo=timezone.utc)


def make_day_bar(inst: InstrumentId, day_offset: int, oi: int, vol: int) -> Bar:
    start = BASE_TIME + timedelta(days=day_offset)
    end = start + timedelta(hours=7)  # 00:00 ~ 07:00
    d = start.date()
    meta = RecordMeta(
        event_time=end,
        available_at=end,
        ingested_at=end,
        trading_day=d,
        source_id="test",
        source_version="v1",
        ingest_seq=day_offset + 1,
    )
    return Bar(
        instrument=inst,
        meta=meta,
        bar_start=start,
        bar_end=end,
        open_time=start,
        interval="1d",
        open=Decimal("3000"),
        high=Decimal("3050"),
        low=Decimal("2980"),
        close=Decimal("3020"),
        volume=vol,
        turnover=Decimal("100000"),
        open_interest=oi,
        includes_auction=False,
    )


def test_dominant_contract_resolver_consecutive_confirm() -> None:
    # 模拟 5 天行情：
    # Day 0: rb2410 oi=1000, rb2501 oi=500 -> 初始主力 rb2410
    # Day 1: rb2410 oi=1000, rb2501 oi=1200 (第 1 天超) -> 暂不切换
    # Day 2: rb2410 oi=900, rb2501 oi=1300 (第 2 天超) -> 触发切换，生效时间为 Day 2 结束时
    # Day 3: rb2501 成为主力
    bars = [
        make_day_bar(RB2410, 0, oi=1000, vol=100),
        make_day_bar(RB2501, 0, oi=500, vol=50),

        make_day_bar(RB2410, 1, oi=1000, vol=100),
        make_day_bar(RB2501, 1, oi=1200, vol=150),

        make_day_bar(RB2410, 2, oi=900, vol=80),
        make_day_bar(RB2501, 2, oi=1300, vol=200),

        make_day_bar(RB2410, 3, oi=700, vol=50),
        make_day_bar(RB2501, 3, oi=1500, vol=300),
    ]

    resolver = build_dominant_mappings(PROD_RB, bars, confirm_days=2)
    assert len(resolver.entries) == 2

    # Day 0 ~ Day 2: rb2410
    t_day1 = BASE_TIME + timedelta(days=1, hours=2)
    dom1 = resolver.dominant(PROD_RB, t_day1)
    assert dom1.value == RB2410

    # Day 3: rb2501
    t_day3 = BASE_TIME + timedelta(days=3, hours=2)
    dom3 = resolver.dominant(PROD_RB, t_day3)
    assert dom3.value == RB2501
