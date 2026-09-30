"""实盘策略生产者的隔离、行情时效、重启与命令归属测试 (S5-05)."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from qh_trader.core.constants import Exchange, MarketPhase, Offset, OrderStatus, OrderType, QualityFlag, Side
from qh_trader.core.execution import CommandKind, CommandStatus, QueuedCommand
from qh_trader.core.objects import (
    Bar,
    ControlEpoch,
    InstrumentId,
    OrderIdentity,
    OrderUpdate,
    RecordMeta,
    Tick,
    Trade,
    TradeKey,
)
from qh_trader.core.ports import StrategyContextPort
from qh_trader.engine.live_engine import LiveEngine
from qh_trader.strategy.base import StrategyBase
from qh_trader.strategy.examples.trend_following import DualMovingAverageStrategy

INSTRUMENT = InstrumentId(Exchange.SHFE, "rb2610")
BASE = datetime(2026, 9, 28, 1, 0, tzinfo=timezone.utc)
DAY = date(2026, 9, 28)


def bar(number=1, close="3000"):
    start = BASE + timedelta(minutes=30 * (number - 1))
    end = start + timedelta(minutes=30)
    price = Decimal(close)
    return Bar(
        instrument=INSTRUMENT,
        meta=meta(end, number),
        bar_start=start,
        bar_end=end,
        interval="30m",
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1,
        turnover=price * 10,
        open_interest=100,
        open_time=start,
        includes_auction=False,
    )


def meta(at, sequence=1):
    return RecordMeta(
        event_time=at,
        available_at=at,
        ingested_at=at,
        trading_day=DAY,
        source_id="test",
        source_version="v1",
        ingest_seq=sequence,
    )


def tick(at, price="2990"):
    value = Decimal(price)
    return Tick(
        instrument=INSTRUMENT,
        meta=meta(at),
        last_price=value,
        bid_price=value - 1,
        ask_price=value + 1,
        bid_volume=10,
        ask_volume=10,
        cumulative_volume=200,
        cumulative_turnover=Decimal("5980000"),
        open_interest=100,
        pre_settlement_price=Decimal("3000"),
        upper_limit_price=Decimal("3300"),
        lower_limit_price=Decimal("2700"),
        phase=MarketPhase.CONTINUOUS,
    )


class Store:
    def __init__(self):
        self.records = {}
        self.fail = False

    def load(self, stream_id):
        return self.records.get(stream_id)

    def save(self, checkpoint):
        if self.fail:
            raise OSError("storage unavailable")
        self.records[checkpoint.stream_id] = checkpoint


class Client:
    def __init__(self, store):
        self.rows = {}
        self.store = store
        self.fail_after_insert = False

    def get(self, command_id):
        return self.rows.get(command_id)

    def submit(self, command):
        assert any(record.processing for record in self.store.records.values())
        row = QueuedCommand(len(self.rows) + 1, command, CommandStatus.PENDING)
        self.rows[command.command_id] = row
        if self.fail_after_insert:
            raise TimeoutError("insert response was lost")
        return row

    def not_sent(self):
        for key, row in self.rows.items():
            self.rows[key] = replace(row, status=CommandStatus.NOT_SENT, processed_seq=1)


class Account:
    position = 0
    active = False

    def __init__(self):
        self.identities = {}

    def get_position(self, instrument):
        return self.position

    def is_order_active(self, client_order_id):
        return self.active

    def has_active_orders(self, instrument):
        return self.active

    def order_identity(self, client_order_id):
        return self.identities.get(client_order_id)


class Clock:
    current = bar().bar_end

    def now(self):
        return self.current

    def schedule(self, at, event):
        pass


class Strategy(StrategyBase):
    def __init__(self, context):
        super().__init__("custom", context)
        self.seen, self.orders, self.trades = [], [], []
        self.action = lambda: self.buy(INSTRUMENT, 1, limit_price_ticks=3001)
        self.tick_action = lambda: self.sell(INSTRUMENT, 1, Offset.CLOSE_TODAY, 2990)

    def on_bar(self, item):
        self.seen.append(item)
        self.action()

    def on_tick(self, item):
        self.tick_action()

    def on_order(self, order):
        self.orders.append(order)

    def on_trade(self, trade):
        self.trades.append(trade)


class Harness:
    def __init__(self):
        self.store, self.account, self.clock = Store(), Account(), Clock()
        self.client = Client(self.store)
        self.ready = True
        self.control = ControlEpoch("executor", 4)

    def engine(self, **kwargs):
        engine = LiveEngine(
            account_id="sim",
            producer_id="strategy-process",
            strategy_id="custom",
            config_version="rules-v1",
            instrument=INSTRUMENT,
            interval="30m",
            command_client=self.client,
            account_view=self.account,
            checkpoint_store=self.store,
            clock=self.clock,
            control_provider=lambda: self.control,
            ready_provider=lambda: self.ready,
            order_translator=kwargs.pop("order_translator", lambda intent: (intent,)),
            max_bar_age=timedelta(seconds=60),
            equity_provider=lambda: Decimal("100000"),
            **kwargs,
        )
        strategy = Strategy(engine)
        engine.attach_strategy(strategy)
        return engine, strategy


def test_only_queues_commands_and_preserves_original_intent():
    harness = Harness()
    engine, strategy = harness.engine()
    assert isinstance(engine, StrategyContextPort)
    engine.start()
    result = engine.on_bar(bar())
    assert result.reason == "processed"
    assert len(result.commands) == 1
    intent, command = result.intents[0], result.commands[0]
    assert command.kind == CommandKind.SUBMIT
    assert command.control == harness.control
    assert command.payload.parent_order_id is None
    assert command.payload.client_order_id.rsplit("-", 1)[0] == intent.client_order_id
    assert command.payload.limit_price_ticks == 3001
    assert engine.is_order_active(intent.client_order_id)
    assert harness.account.position == 0
    assert engine.get_equity() == Decimal("100000")
    assert engine.checkpoint.processing is False
    assert strategy.seen == [bar()]


@pytest.mark.parametrize("blocked", ["not_ready", "stale_bar", "pending_orders"])
def test_blocked_bars_advance_indicators_without_submitting(blocked):
    harness = Harness()
    if blocked == "not_ready":
        harness.ready = False
    elif blocked == "stale_bar":
        harness.clock.current = bar().bar_end + timedelta(seconds=61)
    engine, strategy = harness.engine()
    engine.start()
    if blocked == "pending_orders":
        harness.account.active = True
    result = engine.on_bar(bar())
    assert result.reason == blocked
    assert len(result.intents) == 1
    assert not result.commands and not harness.client.rows
    assert strategy.seen == [bar()]


def test_warmup_does_not_submit_and_requires_ordered_closed_bars():
    harness = Harness()
    harness.clock.current = bar(3).bar_end
    engine, _ = harness.engine(min_warmup_bars=2)
    with pytest.raises(RuntimeError, match="not enough"):
        engine.start()
    engine.warmup([bar(1), bar(2)])
    assert not harness.client.rows and engine.checkpoint is None
    with pytest.raises(ValueError, match="strictly ordered"):
        engine.warmup([bar(2)])
    engine.start()
    assert engine.on_bar(bar(2)).reason == "duplicate_or_out_of_order"
    assert len(engine.on_bar(bar(3)).commands) == 1


def test_restart_replays_indicators_but_never_resubmits_same_bar():
    harness = Harness()
    engine, _ = harness.engine()
    engine.start()
    result = engine.on_bar(bar())
    harness.client.not_sent()
    restarted, strategy = harness.engine()
    with pytest.raises(RuntimeError, match="replay warmup"):
        restarted.start()
    restarted.warmup([bar()])
    restarted.start()
    assert restarted.on_bar(bar()).reason == "duplicate_or_out_of_order"
    assert len(harness.client.rows) == 1
    assert strategy.seen == [bar()]
    harness.clock.current = bar(2).bar_end
    next_result = restarted.on_bar(bar(2))
    assert next_result.commands[0].command_id != result.commands[0].command_id


def test_unknown_insert_result_poisoned_and_restart_refuses_resubmit():
    harness = Harness()
    harness.client.fail_after_insert = True
    engine, _ = harness.engine()
    engine.start()
    with pytest.raises(TimeoutError):
        engine.on_bar(bar())
    assert engine.fault and engine.checkpoint.processing
    assert len(harness.client.rows) == 1
    restarted, _ = harness.engine()
    restarted.warmup([bar()])
    with pytest.raises(RuntimeError, match="incomplete"):
        restarted.start()
    assert len(harness.client.rows) == 1


def test_failed_cursor_save_never_calls_strategy_or_submits():
    harness = Harness()
    engine, strategy = harness.engine()
    engine.start()
    harness.store.fail = True
    with pytest.raises(OSError):
        engine.on_bar(bar())
    assert not strategy.seen and not harness.client.rows


def test_queue_pending_prevents_next_bar_before_account_publication():
    harness = Harness()
    engine, _ = harness.engine()
    engine.start()
    engine.on_bar(bar())
    harness.clock.current = bar(2).bar_end
    assert engine.on_bar(bar(2)).reason == "pending_orders"
    assert len(harness.client.rows) == 1


@pytest.mark.parametrize("state", ["position", "pending", "active"])
def test_restart_exposure_requires_strategy_state_reconciliation(state):
    harness = Harness()
    engine, _ = harness.engine()
    engine.start()
    engine.on_bar(bar())
    if state != "pending":
        harness.client.not_sent()
    if state == "position":
        harness.account.position = 1
    if state == "active":
        harness.account.active = True
    restarted, _ = harness.engine()
    restarted.warmup([bar()])
    with pytest.raises(RuntimeError, match="exposure or pending"):
        restarted.start()


def test_translation_is_explicit_and_close_buckets_are_final():
    harness = Harness()

    def translate(intent):
        return (replace(intent, offset=Offset.CLOSE_TODAY, order_type=OrderType.LIMIT, limit_price_ticks=3000),)

    engine, strategy = harness.engine(order_translator=translate)
    strategy.action = lambda: strategy.sell(INSTRUMENT, 1)
    engine.start()
    result = engine.on_bar(bar())
    assert result.intents[0].offset == Offset.CLOSE
    assert result.intents[0].order_type == OrderType.MARKET
    assert result.commands[0].payload.offset == Offset.CLOSE_TODAY
    assert result.commands[0].payload.limit_price_ticks == 3000


@pytest.mark.parametrize("translation", ["side", "quantity", "unresolved_close"])
def test_invalid_translation_rejects_before_any_child_insert(translation):
    harness = Harness()

    def translate(intent):
        if translation == "side":
            return (replace(intent, side=Side.SELL),)
        if translation == "quantity":
            return (replace(intent, quantity=2),)
        return (replace(intent, offset=Offset.CLOSE),)

    engine, _ = harness.engine(order_translator=translate)
    engine.start()
    with pytest.raises(ValueError, match="translation"):
        engine.on_bar(bar())
    assert not harness.client.rows


def test_control_change_during_translation_fails_closed():
    harness = Harness()

    def translate(intent):
        harness.control = ControlEpoch("replacement", 5)
        return (intent,)

    engine, _ = harness.engine(order_translator=translate)
    engine.start()
    with pytest.raises(RuntimeError, match="control epoch changed"):
        engine.on_bar(bar())
    assert not harness.client.rows


def test_future_and_wrong_interval_are_rejected_before_processing():
    harness = Harness()
    engine, _ = harness.engine()
    engine.start()
    with pytest.raises(ValueError, match="unavailable"):
        engine.on_bar(bar(2))
    with pytest.raises(ValueError, match="interval"):
        engine.on_bar(replace(bar(), interval="1h"))
    assert engine.checkpoint is None


def test_tick_stop_uses_durable_commands_and_deduplicates_snapshot():
    harness = Harness()
    engine, _ = harness.engine()
    engine.start()
    harness.account.position = 1
    quote = tick(harness.clock.current)
    result = engine.on_tick(quote)
    assert result.commands[0].payload.offset == Offset.CLOSE_TODAY
    assert engine.checkpoint.tick_time == quote.meta.event_time
    assert engine.checkpoint.bar_end is None
    assert engine.on_tick(quote).reason == "duplicate_or_out_of_order"
    assert len(harness.client.rows) == 1


def test_tick_callbacks_cannot_open_and_stale_tick_cannot_close():
    harness = Harness()
    engine, strategy = harness.engine()
    engine.start()
    quote = tick(harness.clock.current - timedelta(seconds=6))
    assert engine.on_tick(quote).reason == "stale_tick"
    assert not harness.client.rows
    strategy.tick_action = lambda: strategy.buy(INSTRUMENT, 1)
    with pytest.raises(ValueError, match="only reduce"):
        engine.on_tick(tick(harness.clock.current))


def test_cancel_can_run_while_new_submissions_are_blocked_by_active_order():
    harness = Harness()
    identity = OrderIdentity(
        account_id="sim",
        exchange=Exchange.SHFE,
        client_order_id="existing",
        front_id=2,
        session_id=-5,
        order_ref="17",
    )
    harness.account.identities["existing"] = identity
    engine, strategy = harness.engine()
    strategy.action = lambda: engine.cancel_order("existing")
    engine.start()
    harness.account.active = True
    result = engine.on_bar(bar())
    assert result.reason == "pending_orders"
    assert result.commands[0].kind == CommandKind.CANCEL
    assert result.commands[0].payload == identity


def test_callbacks_filter_ownership_and_map_child_id_to_parent():
    harness = Harness()
    engine, strategy = harness.engine()
    engine.start()
    result = engine.on_bar(bar())
    child = result.commands[0].payload
    identity = OrderIdentity(account_id="sim", exchange=Exchange.SHFE, client_order_id=child.client_order_id)
    update = OrderUpdate(
        identity=identity,
        instrument=INSTRUMENT,
        side=Side.BUY,
        offset=Offset.OPEN,
        status=OrderStatus.FILLED,
        quantity=1,
        filled_quantity=1,
        event_time=BASE,
        available_at=BASE,
    )
    trade = Trade(
        account_id="sim",
        instrument=INSTRUMENT,
        trading_day=DAY,
        trade_id="t1",
        side=Side.BUY,
        offset=Offset.OPEN,
        quantity=1,
        price=Decimal("3001"),
        event_time=BASE,
        available_at=BASE,
        deduplication_key=TradeKey("sim", Exchange.SHFE, DAY, "t1"),
        order_identity=identity,
    )
    engine.on_order(update)
    engine.on_trade(replace(trade, order_identity=None))
    engine.on_trade(trade)
    engine.on_trade(trade)
    engine.on_trade(replace(trade, available_at=trade.available_at + timedelta(seconds=1)))
    assert strategy.orders[0].identity.client_order_id == result.intents[0].client_order_id
    assert strategy.trades[0].order_identity.client_order_id == result.intents[0].client_order_id
    engine.on_order(replace(update, identity=replace(identity, client_order_id="other-strategy")))
    engine.on_trade(replace(trade, order_identity=None))
    assert len(strategy.orders) == len(strategy.trades) == 1


def test_existing_strategy_can_share_the_live_context():
    harness = Harness()
    engine, _ = harness.engine()
    strategy = DualMovingAverageStrategy("custom", engine, INSTRUMENT, fast_window=2, slow_window=3)
    # 既有策略仅作接口兼容夹具，非当前用户策略的默认选择。
    engine._strategy = strategy
    engine._translate = lambda intent: (replace(intent, order_type=OrderType.LIMIT, limit_price_ticks=3001),)
    harness.clock.current = bar(5).bar_end
    engine.warmup([bar(index, value) for index, value in enumerate(["3", "2", "1", "2"], 1)])
    engine.start()
    result = engine.on_bar(bar(5, "4"))
    assert result.commands[0].payload.side == Side.BUY


def test_suppressed_submission_notifies_local_rejection_after_sender_returns():
    harness = Harness()
    harness.ready = False
    engine, strategy = harness.engine()
    pending = []
    strategy.action = lambda: pending.append(strategy.buy(INSTRUMENT, 1))

    def on_order(order):
        assert order.status == OrderStatus.REJECTED
        pending.remove(order.identity.client_order_id)

    strategy.on_order = on_order
    engine.start()
    engine.on_bar(bar())
    assert not pending and not harness.client.rows


def test_final_child_enters_actual_execution_service_and_callbacks_restore_signal_id(tmp_path):
    from scripts.run_simnow_strategy import CommittedAccountView
    from tests.unit import test_live_account_model as fixture

    with fixture.Harness(tmp_path / "account.db") as account:
        account.make_ready()
        clock = Clock()
        clock.current = fixture.NOW
        store = Store()
        engine = LiveEngine(
            account_id=fixture.ACCOUNT,
            producer_id="strategy-process",
            strategy_id="custom",
            config_version="live-account-integration",
            instrument=fixture.RB,
            interval="30m",
            command_client=account.client,
            account_view=CommittedAccountView(account.model, account.store),
            checkpoint_store=store,
            clock=clock,
            control_provider=lambda: fixture.CONTROL,
            ready_provider=lambda: True,
            order_translator=lambda intent: (intent,),
            max_bar_age=timedelta(seconds=60),
        )
        strategy = Strategy(engine)
        strategy.action = lambda: engine.buy(fixture.RB, 1, limit_price_ticks=100, strategy_id="custom")
        engine.attach_strategy(strategy)
        engine.start()
        item = replace(
            bar(),
            instrument=fixture.RB,
            bar_end=fixture.NOW,
            bar_start=fixture.NOW - timedelta(minutes=30),
            open_time=fixture.NOW - timedelta(minutes=30),
            meta=meta(fixture.NOW),
        )
        result = engine.on_bar(item)
        account.service.process_next_command()
        child = result.commands[0].payload
        assert account.client.get(child.client_order_id).status == CommandStatus.SENT_UNKNOWN
        assert account.model.orders.get_order(child.client_order_id) is not None
        assert account.gateway.calls[0][1].parent_order_id is None
        accepted = fixture.order_report(child.client_order_id, OrderStatus.ACCEPTED)
        fill = fixture.trade_event("integration-fill", order=child.client_order_id)
        account.fact(accepted)
        engine.on_order(accepted.payload)
        account.fact(fill)
        engine.on_trade(fill.payload)
        assert engine.get_position(fixture.RB) == 1
        assert strategy.orders[0].identity.client_order_id == result.intents[0].client_order_id
        assert strategy.trades[0].order_identity.client_order_id == result.intents[0].client_order_id


@pytest.mark.parametrize("kind", ["bar", "tick"])
def test_invalid_market_quality_never_advances_strategy(kind):
    harness = Harness()
    engine, strategy = harness.engine()
    engine.start()
    event = bar() if kind == "bar" else tick(harness.clock.current)
    event = replace(event, meta=replace(event.meta, quality_flags=QualityFlag.SYNTHETIC))
    with pytest.raises(ValueError, match="quality"):
        (engine.on_bar if kind == "bar" else engine.on_tick)(event)
    assert not harness.client.rows and not strategy.seen


def test_real_ema_strategy_retries_stop_after_explicit_local_rejection():
    from qh_trader.strategy.examples.ema_trend import EmaTrendParameters, EmaTrendStrategy

    harness = Harness()
    engine, _ = harness.engine(
        order_translator=lambda intent: (
            replace(
                intent,
                order_type=OrderType.LIMIT,
                limit_price_ticks=110,
                offset=Offset.CLOSE_TODAY if intent.offset == Offset.CLOSE else intent.offset,
            ),
        )
    )
    parameters = EmaTrendParameters(
        fast_period=2,
        slow_period=3,
        trend_period=5,
        atr_period=2,
        breakout_lookback=3,
        structure_lookback=2,
        slope_lookback=2,
    )
    strategy = EmaTrendStrategy(
        "custom",
        engine,
        INSTRUMENT,
        parameters=parameters,
        multiplier=Decimal("10"),
        price_tick=Decimal("1"),
        equity_provider=engine.get_equity,
    )
    engine._strategy = strategy

    def candle(index, value):
        price = Decimal(value)
        return replace(bar(index, str(price)), open=price - Decimal("0.5"), high=price + Decimal("0.5"), low=price - 2)

    harness.clock.current = bar(10).bar_end
    engine.warmup([candle(index, 99 + index) for index in range(1, 10)])
    assert strategy._pending_open is None and not harness.client.rows
    engine.start()
    result = engine.on_bar(candle(10, 110))
    assert len(result.commands) == 1
    child = result.commands[0].payload
    identity = OrderIdentity(account_id="sim", exchange=Exchange.SHFE, client_order_id=child.client_order_id)
    row = harness.client.rows[child.client_order_id]
    harness.client.rows[child.client_order_id] = replace(row, status=CommandStatus.SENT_UNKNOWN, processed_seq=1)
    harness.account.identities[child.client_order_id] = identity
    harness.account.position = 1
    at = harness.clock.current
    engine.on_trade(
        Trade(
            account_id="sim",
            instrument=INSTRUMENT,
            trading_day=DAY,
            trade_id="actual-fill",
            side=Side.BUY,
            offset=Offset.OPEN,
            quantity=1,
            price=Decimal("111"),
            event_time=at,
            available_at=at,
            deduplication_key=TradeKey("sim", Exchange.SHFE, DAY, "actual-fill"),
            order_identity=identity,
        )
    )
    assert strategy.stop_price is not None
    harness.ready = False
    harness.clock.current += timedelta(seconds=1)
    rejected = engine.on_tick(tick(harness.clock.current, "100"))
    assert rejected.reason == "not_ready"
    assert strategy._pending_close is None
    harness.ready = True
    harness.clock.current += timedelta(seconds=1)
    resumed = engine.on_tick(tick(harness.clock.current, "99"))
    assert resumed.commands[0].payload.offset == Offset.CLOSE_TODAY
    assert strategy._pending_close == resumed.intents[0].client_order_id
