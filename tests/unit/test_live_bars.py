"""实时快照只有具备会话、连续性与可用时刻证据才进入闭合 Bar。"""

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from qh_trader.core.constants import Exchange, MarketPhase, QualityFlag
from qh_trader.core.objects import InstrumentId, Permissions, RecordMeta, Session, Tick
from qh_trader.data.live_bars import LiveBarAggregator, LiveBarDataError

INST = InstrumentId(Exchange.SHFE, "rb2610")
START = datetime.fromisoformat("2026-09-28T21:00:00+08:00")
DAY = date(2026, 9, 29)


def session(start=START, *, duration=180, day=DAY, name="night"):
    return Session(
        instrument=INST,
        session_id=name,
        trading_day=day,
        start=start,
        end=start + timedelta(seconds=duration),
        phase=MarketPhase.CONTINUOUS,
        permissions=Permissions(True, True, True),
        rule_version="calendar-verified-v1",
        source_id="test-calendar",
        available_at=start - timedelta(days=1),
    )


def tick(second, *, price="3000", volume=100, turnover="100000", day=DAY, start=START):
    at = start + timedelta(seconds=second)
    return Tick(
        instrument=INST,
        meta=RecordMeta(
            event_time=at,
            available_at=at,
            ingested_at=at,
            receive_time=at,
            trading_day=day,
            source_id="test-md",
            source_version="1",
            ingest_seq=int(second * 1000),
        ),
        last_price=Decimal(price),
        bid_price=None,
        ask_price=None,
        bid_volume=None,
        ask_volume=None,
        cumulative_volume=volume,
        cumulative_turnover=Decimal(turnover),
        open_interest=123,
        pre_settlement_price=None,
        upper_limit_price=None,
        lower_limit_price=None,
        phase=MarketPhase.UNKNOWN,
    )


def aggregator(*sessions, **kwargs):
    return LiveBarAggregator(
        instrument=INST,
        sessions=sessions or (session(),),
        interval=timedelta(minutes=1),
        max_gap=timedelta(seconds=30),
        **kwargs,
    )


def feed(agg, *ticks):
    return tuple(bar for item in ticks for bar in agg.on_tick(item, now=item.meta.available_at))


def test_closed_bar_excludes_initial_cumulative_and_next_bucket_price():
    agg = aggregator()
    bars = feed(
        agg,
        tick(0),
        tick(20, price="3002", volume=102, turnover="160000"),
        tick(40, price="2999", volume=103, turnover="190000"),
        tick(60, price="3010", volume=105, turnover="250000"),
    )
    assert len(bars) == 1
    bar = bars[0]
    assert (bar.open, bar.high, bar.low, bar.close) == tuple(map(Decimal, ("3000", "3002", "2999", "2999")))
    assert (bar.volume, bar.turnover) == (3, Decimal("90000"))
    assert bar.bar_start == START and bar.bar_end == START + timedelta(minutes=1)
    assert bar.meta.available_at == bar.bar_end
    assert bar.meta.trading_day == DAY  # 夜盘自然日不能被当作交易日。
    assert bar.meta.session_id == "night"


def test_mid_bucket_start_is_discarded_but_following_continuous_bucket_is_usable():
    agg = aggregator()
    assert feed(agg, tick(15), tick(40), tick(60)) == ()
    assert agg.discarded_bars == 1
    feed(agg, tick(80), tick(100))
    bars = agg.advance(START + timedelta(seconds=120))
    assert len(bars) == 1
    assert bars[0].bar_start == START + timedelta(seconds=60)
    assert agg.advance(START + timedelta(seconds=180)) == ()


def test_advance_never_exposes_future_bar_or_emits_it_twice():
    agg = aggregator()
    feed(agg, tick(0), tick(20), tick(40), tick(59))
    assert agg.advance(START + timedelta(seconds=59, milliseconds=500)) == ()
    (bar,) = agg.advance(START + timedelta(seconds=60))
    assert bar.meta.event_time == bar.bar_end == bar.meta.available_at
    assert agg.advance(START + timedelta(seconds=60)) == ()


@pytest.mark.parametrize("last_second,close_second", [(20, 60), (40, 90)])
def test_missing_tail_or_late_timer_does_not_create_a_tradeable_bar(last_second, close_second):
    agg = aggregator()
    feed(agg, tick(0), tick(last_second / 2), tick(last_second))
    assert agg.advance(START + timedelta(seconds=close_second)) == ()
    assert agg.discarded_bars == 1
    assert agg.quality_gaps == 1


def test_session_recess_does_not_leak_cumulative_volume_and_tail_is_clipped():
    first = session(duration=75)
    second = session(START + timedelta(seconds=120), duration=70, name="after-break")
    agg = aggregator(first, second)
    feed(agg, tick(0), tick(20), tick(40), tick(60, volume=103, turnover="190000"))
    feed(agg, tick(74, volume=104, turnover="220000"))
    (tail,) = agg.advance(START + timedelta(seconds=75))
    assert tail.bar_start == START + timedelta(seconds=60)
    assert tail.bar_end == first.end
    assert tail.volume == 4  # 60 秒的快照差分属于 [60, 75)。
    feed(agg, tick(120, volume=200, turnover="300000"), tick(140, volume=201, turnover="330000"))
    feed(agg, tick(160, volume=203, turnover="390000"))
    (new,) = agg.advance(START + timedelta(seconds=180))
    assert new.volume == 3
    assert new.turnover == Decimal("90000")
    assert new.meta.session_id == "after-break"


def test_disconnect_discards_partial_bar_and_resets_counter_baseline():
    agg = aggregator()
    feed(agg, tick(0), tick(20, volume=101))
    agg.mark_gap()
    feed(agg, tick(40, volume=110), tick(60, volume=112), tick(80, volume=113), tick(100, volume=114))
    (bar,) = agg.advance(START + timedelta(seconds=120))
    assert agg.discarded_bars == 2
    assert bar.bar_start == START + timedelta(seconds=60)
    assert bar.volume == 4  # 断线期间累积的 9 手不被灌入恢复后的 Bar。


def test_detected_long_gap_rejects_tick_then_requires_fresh_baseline():
    agg = aggregator()
    feed(agg, tick(0), tick(20))
    with pytest.raises(LiveBarDataError, match="gap"):
        feed(agg, tick(55, volume=110))
    assert feed(agg, tick(56, volume=111), tick(60, volume=112)) == ()
    assert agg.quality_gaps == 1


def test_timer_detects_disconnect_before_bar_close():
    agg = aggregator()
    feed(agg, tick(0), tick(10))
    assert agg.advance(START + timedelta(seconds=41)) == ()
    assert agg.quality_gaps == 1
    assert agg.advance(START + timedelta(seconds=60)) == ()


@pytest.mark.parametrize(
    "change,match",
    [
        ({"day": date(2026, 9, 28)}, "trading day"),
        ({"volume": 99}, "counters"),
        ({"turnover": "99000"}, "counters"),
    ],
)
def test_untrusted_ticks_invalidate_open_bar(change, match):
    agg = aggregator()
    feed(agg, tick(0))
    with pytest.raises(LiveBarDataError, match=match):
        feed(agg, tick(20, **change))
    assert agg.discarded_bars == 1
    assert agg.advance(START + timedelta(seconds=60)) == ()


@pytest.mark.parametrize("quality", [QualityFlag.STALE, QualityFlag.PARTIAL, QualityFlag.INVALID])
def test_quality_flags_are_rejected(quality):
    agg = aggregator()
    item = tick(0)
    item = replace(item, meta=replace(item.meta, quality_flags=quality))
    with pytest.raises(LiveBarDataError, match="quality"):
        feed(agg, item)


@pytest.mark.parametrize("offset", [-1, 6])
def test_future_and_stale_event_times_are_rejected(offset):
    agg = aggregator()
    item = tick(20)
    with pytest.raises(LiveBarDataError, match="stale or future"):
        agg.on_tick(item, now=item.meta.event_time + timedelta(seconds=offset))


def test_duplicate_and_out_of_order_ticks_are_rejected_without_reopening_closed_bar():
    agg = aggregator()
    feed(agg, tick(0), tick(20), tick(40))
    agg.advance(START + timedelta(seconds=60))
    with pytest.raises(LiveBarDataError, match="after its bar was closed"):
        agg.on_tick(tick(59), now=START + timedelta(seconds=60))
    feed(agg, tick(61))
    with pytest.raises(LiveBarDataError, match="out-of-order"):
        agg.on_tick(tick(61), now=START + timedelta(seconds=62))


def test_trading_day_transition_accepts_new_counter_baseline_from_explicit_night_session():
    tomorrow_start = START + timedelta(days=1)
    tomorrow = session(tomorrow_start, day=DAY + timedelta(days=1), name="next-night")
    agg = aggregator(session(), tomorrow)
    feed(agg, tick(0, volume=1000), tick(20, volume=1001))
    feed(
        agg,
        tick(0, start=tomorrow_start, day=tomorrow.trading_day, volume=1, turnover="30000"),
        tick(20, start=tomorrow_start, day=tomorrow.trading_day, volume=2, turnover="60000"),
        tick(40, start=tomorrow_start, day=tomorrow.trading_day, volume=3, turnover="90000"),
    )
    (bar,) = agg.advance(tomorrow_start + timedelta(seconds=60))
    assert bar.volume == 2
    assert bar.meta.trading_day == tomorrow.trading_day


def test_night_crossing_midnight_keeps_registered_trading_day():
    start = datetime.fromisoformat("2026-09-28T23:59:30+08:00")
    agg = aggregator(session(start))
    feed(agg, *(tick(second, start=start) for second in (0, 20, 40)))
    (bar,) = agg.advance(start + timedelta(seconds=60))
    assert bar.meta.trading_day == DAY
    assert bar.bar_start == start
    assert bar.bar_end == start + timedelta(seconds=60)


def test_no_ticks_and_single_snapshot_do_not_fabricate_bar():
    agg = aggregator()
    assert agg.advance(START) == ()
    feed(agg, tick(0))
    assert agg.advance(START + timedelta(seconds=60)) == ()


def test_session_end_is_exclusive_and_unknown_calendar_is_refused():
    agg = aggregator(session(duration=60))
    with pytest.raises(LiveBarDataError, match="session covers"):
        feed(agg, tick(60))
    with pytest.raises(ValueError, match="explicit sessions"):
        LiveBarAggregator(instrument=INST, sessions=())


def test_observation_clock_cannot_move_backwards():
    agg = aggregator()
    agg.advance(START + timedelta(seconds=30))
    with pytest.raises(LiveBarDataError, match="clock moved backwards"):
        feed(agg, tick(20))


def test_default_hour_bars_align_to_session_start_instead_of_wall_clock_hour():
    start = datetime.fromisoformat("2026-09-29T13:30:00+08:00")
    agg = LiveBarAggregator(instrument=INST, sessions=(session(start, duration=5400),))
    bars = feed(agg, *(tick(second, start=start, volume=100 + second) for second in range(0, 3601, 10)))
    (bar,) = bars
    assert bar.interval == "1h"
    assert bar.bar_start == start
    assert bar.bar_end == start + timedelta(hours=1)
    assert bar.volume == 3590


@pytest.mark.parametrize("field", ["available_at", "ingested_at"])
def test_future_availability_is_not_consumed(field):
    agg = aggregator()
    item = tick(0)
    item = replace(item, meta=replace(item.meta, **{field: START + timedelta(seconds=1)}))
    with pytest.raises(LiveBarDataError, match="availability"):
        agg.on_tick(item, now=START)


def test_phase_and_session_identity_must_agree_with_calendar():
    agg = aggregator()
    item = tick(0)
    with pytest.raises(LiveBarDataError, match="phase"):
        feed(agg, replace(item, phase=MarketPhase.SUSPENDED))
    with pytest.raises(LiveBarDataError, match="session does not match"):
        feed(agg, replace(item, meta=replace(item.meta, session_id="wrong-session")))


def test_30m_bucket_accepts_first_snapshot_half_second_after_boundary_with_continuous_baseline():
    agg = LiveBarAggregator(
        instrument=INST,
        sessions=(session(duration=7200),),
        interval=timedelta(minutes=30),
    )
    bars = feed(agg, *(tick(index * 5 + 0.5, volume=100 + index) for index in range(721)))
    (bar,) = bars
    assert agg.discarded_bars == 1  # 初始 21:00:00.500 之前缺少可信基线，首桶不能发信号。
    assert agg.quality_gaps == 0
    assert bar.interval == "30m"
    assert bar.bar_start == START + timedelta(minutes=30)
    assert bar.open_time == START + timedelta(minutes=30, milliseconds=500)
    assert bar.bar_end == START + timedelta(hours=1)
    assert bar.meta.available_at == START + timedelta(hours=1, milliseconds=500)
    assert bar.volume == 360
