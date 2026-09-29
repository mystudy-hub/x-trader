"""EMA 策略的可见性、风险预算、实际成交保护及单 Bar 路径边界。"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from qh_trader.core.constants import Exchange, MarketPhase, Offset, OrderStatus, Side
from qh_trader.core.objects import Bar, InstrumentId, OrderIdentity, OrderUpdate, RecordMeta, Tick, Trade, TradeKey
from qh_trader.strategy.examples.ema_trend import EmaTrendParameters, EmaTrendStrategy

D = Decimal
INST = InstrumentId(Exchange.SHFE, "rb2701")
BASE = datetime(2026, 9, 28, 1, 0, tzinfo=timezone.utc)


class Context:
    def __init__(self):
        self.at = BASE + timedelta(days=2)
        self.sent = []
        self.position = 0

    def now(self):
        return self.at

    def get_position(self, instrument):
        return self.position

    def buy(self, instrument, quantity, offset, limit_price_ticks, *, strategy_id):
        self.sent.append((Side.BUY, quantity, offset))
        return f"order-{len(self.sent)}"

    def sell(self, instrument, quantity, offset, limit_price_ticks, *, strategy_id):
        self.sent.append((Side.SELL, quantity, offset))
        return f"order-{len(self.sent)}"

    def cancel_order(self, order_id):
        pass


def bar(index, close, *, high=None, low=None, op=None):
    close = D(str(close))
    start = BASE + timedelta(minutes=30 * index)
    end = start + timedelta(minutes=30)
    return Bar(
        instrument=INST,
        meta=RecordMeta(
            event_time=end,
            available_at=end,
            ingested_at=end,
            trading_day=BASE.date(),
            source_id="test",
            source_version="test",
            ingest_seq=index + 1,
        ),
        bar_start=start,
        bar_end=end,
        open_time=start,
        interval="30m",
        open=D(str(op)) if op else close - D("0.5"),
        high=D(str(high)) if high else close + D("0.5"),
        low=D(str(low)) if low else close - 2,
        close=close,
        volume=100,
        turnover=D("10000"),
        open_interest=500,
        includes_auction=False,
    )


def strategy(*, equity="100000", mode="A"):
    ctx = Context()
    params = EmaTrendParameters(
        mode=mode,
        fast_period=2,
        slow_period=3,
        trend_period=5,
        atr_period=2,
        breakout_lookback=3,
        structure_lookback=2,
        slope_lookback=2,
    )
    s = EmaTrendStrategy(
        "ema", ctx, INST, parameters=params, multiplier=D("10"), price_tick=D("1"), equity_provider=lambda: D(equity)
    )
    return s, ctx


def enter(s, ctx):
    s.warmup([bar(i, 100 + i) for i in range(9)])
    assert ctx.sent == []
    s.on_start()
    s.on_bar(bar(9, 110))
    assert ctx.sent == [(Side.BUY, 1, Offset.OPEN)]


def fill(s, price="111", *, at=None, trade_id="fill", side=Side.BUY, offset=Offset.OPEN):
    at = at or BASE + timedelta(minutes=300)
    trade = Trade(
        account_id="test",
        instrument=INST,
        trading_day=BASE.date(),
        trade_id=trade_id,
        side=side,
        offset=offset,
        quantity=1,
        price=D(price),
        event_time=at,
        available_at=at,
        deduplication_key=TradeKey("test", Exchange.SHFE, BASE.date(), trade_id),
    )
    s.on_trade(trade)
    return trade


def tick(price, at):
    return Tick(
        instrument=INST,
        meta=RecordMeta(
            event_time=at,
            available_at=at,
            ingested_at=at,
            trading_day=BASE.date(),
            source_id="test",
            source_version="test",
            ingest_seq=1,
        ),
        last_price=D(str(price)),
        cumulative_volume=1,
        cumulative_turnover=D("1000"),
        open_interest=100,
        bid_price=D(str(price)) - 1,
        ask_price=D(str(price)) + 1,
        bid_volume=1,
        ask_volume=1,
        pre_settlement_price=None,
        upper_limit_price=None,
        lower_limit_price=None,
        phase=MarketPhase.CONTINUOUS,
    )


def test_warmup_future_and_duplicate_bar_do_not_generate_repeated_entries():
    s, ctx = strategy()
    enter(s, ctx)
    s.on_bar(bar(9, 110))
    s.on_bar(bar(10, 113))
    assert len(ctx.sent) == 1
    ctx.at = BASE
    with pytest.raises(ValueError, match="available"):
        s.on_bar(bar(11, 114))


def test_risk_floor_rejects_insufficient_budget_instead_of_forcing_one_lot():
    s, ctx = strategy(equity="100")
    s.warmup([bar(i, 100 + i) for i in range(9)])
    s.on_start()
    s.on_bar(bar(9, 110))
    assert ctx.sent == []
    assert s.decisions[-1]["reason"] == "risk_budget_below_one_lot"


def test_initial_stop_uses_actual_fill_and_duplicate_fill_is_idempotent():
    s, ctx = strategy()
    enter(s, ctx)
    assert s.stop_price is None
    trade = fill(s)
    assert s.entry_price == D("111")
    assert s.stop_price == D("106")  # 结构低点 105 / 信号 110，距离 5；实际成交 111。
    s.on_trade(trade)
    assert s._position == 1


def test_tick_breakeven_and_trailing_stop_is_monotonic_and_exits_once():
    s, ctx = strategy()
    enter(s, ctx)
    fill(s)
    at = BASE + timedelta(minutes=301)
    s.on_tick(tick(120, at))
    protected = s.stop_price
    assert protected >= s.entry_price
    s.on_tick(tick(118, at + timedelta(seconds=1)))
    assert s.stop_price >= protected
    s.on_tick(tick(100, at + timedelta(seconds=2)))
    s.on_tick(tick(99, at + timedelta(seconds=3)))
    assert ctx.sent == [(Side.BUY, 1, Offset.OPEN), (Side.SELL, 1, Offset.CLOSE)]
    assert s.decisions[-1]["reason"] == "tick_stop"


def test_bar_does_not_apply_new_high_then_same_bar_low_to_new_trailing_stop():
    s, ctx = strategy()
    enter(s, ctx)
    fill(s)
    s.on_bar(bar(10, 119, high=125, low=108, op=111))
    assert len(ctx.sent) == 1  # 原 stop=106 未触发，不假设高点125先于低点108。
    assert s.stop_price >= 111
    s.on_bar(bar(11, 120, high=121, low=110, op=119))
    assert ctx.sent[-1] == (Side.SELL, 1, Offset.CLOSE)


def test_mid_bar_fill_does_not_treat_before_fill_low_as_stop_hit():
    s, ctx = strategy()
    enter(s, ctx)
    fill(s, at=BASE + timedelta(minutes=310))
    s.on_bar(bar(10, 115, high=116, low=90, op=100))
    assert len(ctx.sent) == 1


def test_unknown_restart_position_refuses_unprotected_start():
    s, ctx = strategy()
    ctx.position = 1
    with pytest.raises(ValueError, match="protection state"):
        s.on_start()


def test_short_fill_stops_on_upward_tick():
    s, ctx = strategy()
    s._planned_distance = D("5")
    s._entry_atr = D("3")
    fill(s, price="111", side=Side.SELL)
    assert s.stop_price == D("116")
    s.on_tick(tick(117, BASE + timedelta(minutes=301)))
    assert ctx.sent == [(Side.BUY, 1, Offset.CLOSE)]


def test_breakout_requires_cross_and_prior_high_excludes_signal_bar():
    s, _ = strategy(mode="B")
    s._bars.extend([bar(0, 100), bar(1, 101), bar(2, 102)])
    s._trend.value, s._slow.value, s._fast.value = D("100"), D("103"), D("104")
    s._trend_history.extend([D("99"), D("99.5"), D("100")])
    s.atr = D("2")
    signal = bar(3, 106, high=110)
    assert s._entry_side(signal, s._bars[-1], D("102"), D("103")) == Side.BUY
    assert s._entry_side(signal, s._bars[-1], D("104"), D("103")) is None
    assert s._entry_side(bar(3, 102), s._bars[-1], D("102"), D("103")) is None


def test_parameters_do_not_allow_risk_above_user_ceiling():
    with pytest.raises(ValueError, match="1.5%"):
        replace(EmaTrendParameters(), risk_fraction=D("0.02"))
    with pytest.raises(ValueError, match="max_lots=1"):
        replace(EmaTrendParameters(), max_lots=2)


def test_generic_inactive_on_bar_warmup_leaves_no_pending_order():
    s, ctx = strategy()
    for i in range(12):
        s.on_bar(bar(i, 100 + i))
    assert s.ready and not s._pending_open and not ctx.sent


def test_declining_ema200_or_flat_repeated_crossings_block_long_entry():
    s, _ = strategy(mode="B")
    s._bars.extend([bar(0, 98), bar(1, 99), bar(2, 100)])
    s._trend.value, s._slow.value, s._fast.value = D("100"), D("101"), D("102")
    s.atr = D("20")
    signal = bar(3, 103)
    s._trend_history.extend([D("102"), D("101"), D("100")])
    assert s._entry_side(signal, s._bars[-1], D("100"), D("101")) is None
    s._trend_history.clear()
    s._trend_history.extend([D("100"), D("100"), D("100")])
    s._trend_signs.extend([1, -1, 1, -1, 1])
    assert s._entry_side(signal, s._bars[-1], D("100"), D("101")) is None


def test_first_tick_of_new_bucket_cannot_retroactively_stop_closed_old_bar():
    s, ctx = strategy()
    enter(s, ctx)
    fill(s)
    s.on_tick(tick(120, BASE + timedelta(minutes=330, milliseconds=500)))
    assert s.stop_price >= 111
    s.on_bar(bar(10, 119, high=125, low=108, op=111))
    assert ctx.sent == [(Side.BUY, 1, Offset.OPEN)]  # 旧桶开始时 stop=106，不能套用新桶首 Tick 的 stop。


def update(order_id, offset, status, *, filled=0):
    at = BASE + timedelta(minutes=301)
    return OrderUpdate(
        identity=OrderIdentity(account_id="test", exchange=Exchange.SHFE, client_order_id=order_id),
        instrument=INST,
        side=Side.BUY if offset == Offset.OPEN else Side.SELL,
        offset=offset,
        status=status,
        quantity=1,
        filled_quantity=filled,
        event_time=at,
        available_at=at,
    )


@pytest.mark.parametrize("terminal", [OrderStatus.FILLED, OrderStatus.CANCELLED])
def test_open_terminal_ahead_of_trade_keeps_pending_until_protection_exists(terminal):
    s, ctx = strategy()
    enter(s, ctx)
    s.on_order(update("order-1", Offset.OPEN, terminal, filled=1))
    assert s._pending_open == "order-1" and s.stop_price is None
    s.on_bar(bar(10, 112))
    assert len(ctx.sent) == 1
    fill(s)
    assert s._pending_open is None and s.stop_price is not None


def test_close_filled_report_ahead_of_trade_does_not_submit_duplicate_exit():
    s, ctx = strategy()
    enter(s, ctx)
    fill(s)
    at = BASE + timedelta(minutes=301)
    s.on_tick(tick(100, at))
    s.on_order(update("order-2", Offset.CLOSE, OrderStatus.FILLED, filled=1))
    s.on_tick(tick(99, at + timedelta(seconds=1)))
    assert len(ctx.sent) == 2 and s._pending_close == "order-2"
    fill(s, price="99", at=at, trade_id="close", side=Side.SELL, offset=Offset.CLOSE)
    assert s._pending_close is None and s._position == 0


def test_local_rejection_clears_matching_pending_but_not_from_an_old_order():
    s, ctx = strategy()
    enter(s, ctx)
    s.on_order(update("old-order", Offset.OPEN, OrderStatus.REJECTED))
    assert s._pending_open == "order-1"
    s.on_order(update("order-1", Offset.OPEN, OrderStatus.REJECTED))
    assert s._pending_open is None and s.stop_price is None
