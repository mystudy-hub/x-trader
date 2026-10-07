"""通达信来源只发布完整会话窗口，字段缺失、映射和分页边界均显式处理。"""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from qh_trader.core.constants import Exchange, MarketPhase, MissingRuleError
from qh_trader.core.objects import InstrumentId, Permissions, Session
from qh_trader.data.calendar import TradingCalendar
from qh_trader.data.sources import TdxExHqDataSource, TdxPaginationError, create_data_source
from qh_trader.data.tdx_aggregate import aggregate_tdx_minutes

INST = InstrumentId(Exchange.SHFE, "rb2701")
DAY = date(2026, 9, 28)
START = datetime.fromisoformat("2026-09-28T09:00:00+08:00")


def row(stamp, *, price="3000", volume=2, open_interest=100):
    return {
        "datetime": stamp,
        **{name: Decimal(price) for name in ("open", "high", "low", "close")},
        "volume": volume,
        "open_interest": open_interest,
        "turnover": None,
        "settlement_price": None,
        "price": Decimal("3001"),
    }


def minutes(start=START, count=60):
    return [row(start + timedelta(minutes=i), open_interest=100 + i) for i in range(1, count + 1)]


def session(start=START, count=60, *, name="day", trading_day=DAY):
    return Session(
        instrument=INST,
        session_id=name,
        trading_day=trading_day,
        start=start,
        end=start + timedelta(minutes=count),
        phase=MarketPhase.CONTINUOUS,
        permissions=Permissions(True, True, True),
        rule_version="test-v1",
        source_id="test-calendar",
        available_at=START - timedelta(days=10),
    )


def calendar(*sessions, extra_days=()):
    days = sorted({item.trading_day for item in sessions} | set(extra_days))
    return TradingCalendar(
        sessions,
        trading_days=days,
        coverage_start=days[0],
        coverage_end=days[-1],
        version="test-v1",
        source_id="test-calendar",
        available_at=START - timedelta(days=10),
    )


class Pages:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get_instrument_bars(self, category, market, code, start, count):
        self.calls.append((category, market, code, start, count))
        offset = 0
        for page in self.pages:
            if start == offset:
                return page
            offset += len(page)
        return []


def test_factory_and_resolved_contract_mapping_never_guess_exchange_or_decade():
    source = create_data_source("tdx")
    assert isinstance(source, TdxExHqDataSource)
    assert source.source_id == "tdx_exhq"
    assert source.to_tdx_code(INST) == (30, "RB2701")
    assert source.to_tdx_code("CZCE.fg2701") == (28, "FG701")
    assert source.to_tdx_code("CFFEX.IF2701") == (47, "IF2701")
    for symbol in ("CZCE.FG701", "rb2701", "SHFE.RBL8", "SHFE.rb2713"):
        with pytest.raises(ValueError):
            source.to_tdx_code(symbol)
    with pytest.raises(ValueError, match="unverified"):
        source.to_tdx_code("INE.sc2701")
    verified = TdxExHqDataSource(market_overrides={Exchange.INE: 30})
    assert verified.to_tdx_code("INE.sc2701") == (30, "SC2701")


def test_daily_pages_are_sorted_and_identical_overlap_is_deduplicated():
    days = [row(datetime(2026, 9, day)) for day in (25, 26, 27)]
    client = Pages([[days[2], days[1]], [days[1], days[0]], []])
    source = TdxExHqDataSource(client=client, page_size=2)
    bars = source.fetch_daily_bars(INST)
    assert [item["date"] for item in bars] == ["2026-09-25", "2026-09-26", "2026-09-27"]
    assert bars[0]["turnover"] is None and bars[0]["settlement_price"] is None
    assert bars[0]["settlement_proxy"] == Decimal("3001")
    assert [call[3] for call in client.calls] == [0, 2, 4]


def test_daily_pagination_stops_at_requested_start_and_filters_both_boundaries():
    client = Pages([[row(datetime(2026, 9, 28)), row(datetime(2026, 9, 27))]])
    source = TdxExHqDataSource(client=client, page_size=2, max_pages=1)
    assert [item["date"] for item in source.fetch_daily_bars(INST, "2026-09-27", "2026-09-27")] == ["2026-09-27"]
    assert len(client.calls) == 1


def test_short_page_continues_at_actual_offset_until_empty():
    client = Pages([[row(datetime(2026, 9, 28))], [row(datetime(2026, 9, 25))], []])
    source = TdxExHqDataSource(client=client, page_size=700)
    assert [item["date"] for item in source.fetch_daily_bars(INST)] == ["2026-09-25", "2026-09-28"]
    assert [call[3] for call in client.calls] == [0, 1, 2]


def test_short_page_at_limit_is_not_complete_history():
    source = TdxExHqDataSource(client=Pages([[row(datetime(2026, 9, 28))]]), max_pages=1)
    with pytest.raises(TdxPaginationError, match="truncated"):
        source.fetch_daily_bars(INST)


def test_truncated_or_stuck_pages_and_conflicting_duplicate_are_refused():
    page = [row(datetime(2026, 9, 28)), row(datetime(2026, 9, 27))]
    source = TdxExHqDataSource(client=Pages([page]), page_size=2, max_pages=1)
    with pytest.raises(TdxPaginationError, match="truncated"):
        source.fetch_daily_bars(INST)
    source = TdxExHqDataSource(client=Pages([page, page]), page_size=2)
    with pytest.raises(TdxPaginationError, match="stopped advancing"):
        source.fetch_daily_bars(INST)
    changed = {**page[1], "volume": 20}
    source = TdxExHqDataSource(client=Pages([page, [changed, row(datetime(2026, 9, 26))]]), page_size=2)
    with pytest.raises(ValueError, match="conflicting"):
        source.fetch_daily_bars(INST)


def test_session_recess_and_short_tail_are_never_stitched_into_a_bar():
    second = START + timedelta(minutes=90)
    cal = calendar(session(count=75), session(second, name="after-break"))
    bars = aggregate_tdx_minutes(minutes(count=75) + minutes(second), instrument=INST, calendar=cal)
    assert [item["bar_end"].astimezone(START.tzinfo).strftime("%H:%M") for item in bars] == [
        "09:30",
        "10:00",
        "11:00",
        "11:30",
    ]
    assert [item["volume"] for item in bars] == [60, 60, 60, 60]
    assert bars[0]["open_interest"] == 130
    assert bars[-1]["turnover"] is None and bars[-1]["settlement_price"] is None


def test_gap_discards_only_affected_bucket_and_preserves_ohlc_and_last_interest():
    records = minutes()
    records[40] = row(records[40]["datetime"], price="3010", open_interest=999)
    records[50] = row(records[50]["datetime"], price="2990", open_interest=999)
    del records[5]
    bars = aggregate_tdx_minutes(records, instrument=INST, calendar=calendar(session()))
    assert len(bars) == 1
    assert bars[0]["bar_start"] == START + timedelta(minutes=30)
    assert (bars[0]["open"], bars[0]["high"], bars[0]["low"], bars[0]["close"]) == tuple(
        map(Decimal, ("3000", "3010", "2990", "3000"))
    )
    assert bars[0]["open_interest"] == 160


def test_midnight_end_stamp_and_weekend_night_keep_explicit_trading_day():
    night = datetime.fromisoformat("2026-09-25T23:30:00+08:00")
    cal = calendar(session(night, name="friday-night"), extra_days=(date(2026, 9, 29),))
    bars = aggregate_tdx_minutes(minutes(night), instrument=INST, calendar=cal)
    assert len(bars) == 2
    assert bars[0]["bar_end"].astimezone(START.tzinfo).isoformat() == "2026-09-26T00:00:00+08:00"
    assert all(item["trading_day"] == DAY and item["session_id"] == "friday-night" for item in bars)


def test_minute_must_fit_registered_session_and_have_valid_underlying_ohlc():
    cal = calendar(session())
    with pytest.raises(MissingRuleError, match="session"):
        aggregate_tdx_minutes([row(START)], instrument=INST, calendar=cal)
    records = minutes()
    records[0]["low"] = Decimal("3001")
    with pytest.raises(ValueError, match="quality"):
        aggregate_tdx_minutes(records, instrument=INST, calendar=cal)


def test_source_requires_calendar_for_30m_and_always_requests_category_7():
    client = Pages([minutes()])
    source = TdxExHqDataSource(client=client)
    with pytest.raises(ValueError, match="--calendar"):
        source.fetch_minute_bars(INST, "30")
    assert not client.calls
    assert len(source.fetch_minute_bars(INST, "1", end_time="2026-09-28")) == 60
    source.calendar = calendar(session())
    assert len(source.fetch_minute_bars(INST, "30", "2026-09-28", "2026-09-28")) == 2
    assert all(call[0] == 7 for call in client.calls)


def test_date_range_fetches_prior_friday_night_of_monday_trading_day():
    night = datetime.fromisoformat("2026-09-25T23:30:00+08:00")
    cal = calendar(session(night, name="night"), session())
    client = Pages([minutes(night) + minutes()])
    source = TdxExHqDataSource(client=client, calendar=cal)
    bars = source.fetch_minute_bars(INST, "30", "2026-09-28", "2026-09-28")
    assert len(bars) == 4
    assert all(item["trading_day"] == DAY for item in bars)
    assert bars[0]["bar_start"] == night


def test_cli_cannot_publish_tdx_without_explicit_research_mode():
    from scripts.download_data import main

    with pytest.raises(SystemExit) as result:
        main(["--source", "tdx", "--publish", "--catalog", "x", "--calendar", "x", "--timings", "x"])
    assert result.value.code == 2
