"""Unit tests for continuous series builder (S4-02, FR-CON-03, A10)."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from qh_trader.core.constants import Exchange, QualityFlag
from qh_trader.core.objects import Bar, InstrumentId, ProductId, RecordMeta
from qh_trader.data.continuous import (
    AdjustmentMethod,
    ContinuousSeriesBuilder,
)
from qh_trader.data.dominant_contract import DominantMappingEntry, DominantContractResolver

PROD_RB = ProductId(Exchange.SHFE, "rb")
RB2410 = InstrumentId(Exchange.SHFE, "rb2410")
RB2501 = InstrumentId(Exchange.SHFE, "rb2501")
BASE_TIME = datetime(2024, 8, 1, 0, 0, tzinfo=timezone.utc)


def make_test_bar(inst: InstrumentId, day_offset: int, op: str, cl: str) -> Bar:
    start = BASE_TIME + timedelta(days=day_offset)
    end = start + timedelta(hours=7)
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
        open=Decimal(op),
        high=max(Decimal(op), Decimal(cl)) + Decimal("10"),
        low=min(Decimal(op), Decimal(cl)) - Decimal("10"),
        close=Decimal(cl),
        volume=100,
        turnover=Decimal("100000"),
        open_interest=1000,
        includes_auction=False,
    )


def test_continuous_series_no_gap_momentum() -> None:
    # 构造换月场景：
    # Day 0: rb2410 close=3000, rb2501 close=3200 (价差 200 点升水)
    # Day 1: 主力切换至 rb2501，rb2501 open=3200, close=3220 (新合约自身涨 20 点)
    # 如果错误地把新合约 3220 对比旧合约 3000，会算出 +220 点 (+7.3%) 虚假暴涨！
    # 正确做法：应该算新合约自身昨日到今日涨跌幅 (3220 - 3200) / 3200 = +0.625%
    day0 = BASE_TIME.date()
    day1 = (BASE_TIME + timedelta(days=1)).date()

    entries = [
        DominantMappingEntry(
            product=PROD_RB,
            instrument=RB2410,
            trading_day=day0,
            decision_time=BASE_TIME,
            effective_from=BASE_TIME,
            effective_to=BASE_TIME + timedelta(days=1),
            open_interest=1000,
            volume=100,
            version="v1",
        ),
        DominantMappingEntry(
            product=PROD_RB,
            instrument=RB2501,
            trading_day=day1,
            decision_time=BASE_TIME + timedelta(days=1),
            effective_from=BASE_TIME + timedelta(days=1),
            effective_to=None,
            open_interest=2000,
            volume=200,
            version="v1",
        ),
    ]
    resolver = DominantContractResolver(PROD_RB, entries, "v1")

    bars_rb2410 = [
        make_test_bar(RB2410, 0, "3000", "3000"),
        make_test_bar(RB2410, 1, "3010", "3010"),
    ]
    bars_rb2501 = [
        make_test_bar(RB2501, 0, "3200", "3200"),
        make_test_bar(RB2501, 1, "3200", "3220"),
    ]

    builder = ContinuousSeriesBuilder(resolver, method=AdjustmentMethod.DIFF)
    series = builder.build_series({RB2410: bars_rb2410, RB2501: bars_rb2501})

    assert len(series) == 2
    # Day 0: 底层是 rb2410, raw close = 3000
    assert series[0].underlying_instrument == RB2410
    assert series[0].raw_bar.close == Decimal("3000")

    # Day 1: 底层是 rb2501
    assert series[1].underlying_instrument == RB2501
    # Day 1 收益率必须基于同合约计算 (3220 - 3200) / 3200 = 20 / 3200 = 0.00625
    # 绝对不能是 (3220 - 3000) / 3000 = 220 / 3000 = 0.0733
    expected_ret = Decimal("20") / Decimal("3200")
    assert abs(series[1].single_day_return - expected_ret) < Decimal("0.0001")
