"""S5-05：Journal 策略订阅、固定代次、耐久 outbox、恢复及共享风控纸面闭环."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from qh_trader.core.constants import EventKind, Exchange, JournalConflictError, Offset, OrderStatus, SendState, Side
from qh_trader.core.event import CanonicalEvent, JournalTransaction
from qh_trader.core.execution import CommandStatus, ExecutionNotReadyError, ExecutionOwnershipError
from qh_trader.core.objects import (
    AccountFunds,
    Bar,
    ControlEpoch,
    ControlRecord,
    InstrumentId,
    LocalSendResult,
    OrderIdentity,
    OrderUpdate,
    QueryBatch,
    QueryResult,
    RecordMeta,
    Trade,
    TradeKey,
)
from qh_trader.core.ports import StrategyContextPort, StrategyJournalPort, StrategyRuntimeStorePort
from qh_trader.domain.recovery import RecoveryCoordinator
from qh_trader.engine.base_engine import InstrumentEconomics
from qh_trader.engine.execution_service import ExecutionService
from qh_trader.engine.journal_strategy_engine import LiveStrategyEngine as JournalStrategyEngine
from qh_trader.engine.live_account_model import AccountOpening, LiveAccountModel
from qh_trader.engine.live_engine import LiveStrategyEngine, StrategyReplayError
from qh_trader.infrastructure.command_queue import SQLiteCommandClient, SQLiteExecutionStore
from qh_trader.infrastructure.journal import SQLiteJournal
from qh_trader.infrastructure.strategy_runtime import SQLiteStrategyRuntimeStore
from qh_trader.monitor.heartbeat import HeartbeatFile, read_heartbeat
from qh_trader.strategy.base import StrategyBase
from qh_trader.strategy.examples.async_trend_following import AsyncDualMovingAverageStrategy
from scripts.run_strategy import ExecutionLivenessGate, main, runtime_paths


@pytest.mark.parametrize("missing", [False, True], ids=["sharing-violation", "replace-window"])
def test_heartbeat_read_retry_is_bounded_and_never_approves_persistent_failure(tmp_path, monkeypatch, missing):
    from scripts import run_strategy

    writer = HeartbeatFile(tmp_path / "execution.json", role="execution", instance_id=CONTROL.controller_id)
    gate = ExecutionLivenessGate(writer.path, 1, 5, controller_id=CONTROL.controller_id)
    writer.beat(control_epoch=1, ready=True)
    assert not gate()
    writer.beat(control_epoch=1, ready=True)
    assert gate()
    read = run_strategy.read_heartbeat
    attempts = []

    def transient(path):
        attempts.append(path)
        if len(attempts) == 1:
            if missing:
                return None
            raise PermissionError("fixture replace contention")
        return read(path)

    monkeypatch.setattr(run_strategy, "read_heartbeat", transient)
    assert gate() and len(attempts) == 2
    attempts.clear()

    def persistent(path):
        attempts.append(path)
        if missing:
            return None
        raise PermissionError("fixture persistent failure")

    monkeypatch.setattr(run_strategy, "read_heartbeat", persistent)
    assert not gate() and len(attempts) == 5


NOW = datetime(2024, 9, 10, 1, 1, tzinfo=timezone.utc)
DAY = date(2024, 9, 10)
ACCOUNT = "strategy-test"
CONTROL = ControlEpoch("paper-controller", 1)
RB = InstrumentId(Exchange.SHFE, "rb2410")


class RecordingGateway:
    def __init__(self):
        self.calls = []

    def submit(self, order, epoch):
        self.calls.append((order, epoch))
        return LocalSendResult(SendState.SENT_UNKNOWN, 0, "paper-local")

    def cancel(self, identity, epoch):
        self.calls.append((identity, epoch))
        return LocalSendResult(SendState.SENT_UNKNOWN, 0, "paper-cancel")


@pytest.fixture
def paper(tmp_path):
    with ExitStack() as stack:
        path = tmp_path / "trading.db"
        journal = stack.enter_context(SQLiteJournal(path, account_id=ACCOUNT))
        journal.migrate()
        journal.append(
            JournalTransaction(
                transaction_id="seed",
                events=(),
                cursor_before=0,
                cursor_after=0,
                control_record=ControlRecord(CONTROL, NOW, 1),
                state_updates={"account_view": {"positions": (), "active_orders": 0}},
            )
        )
        store = stack.enter_context(SQLiteExecutionStore(journal))
        store.migrate()
        client = stack.enter_context(SQLiteCommandClient(path, account_id=ACCOUNT))
        model = LiveAccountModel(
            ACCOUNT,
            AccountOpening(Decimal(1000), DAY),
            {RB: InstrumentEconomics(Decimal(10), Decimal(1), Decimal(0), Decimal("0.1"), "test")},
            now=lambda: NOW,
        )
        recovery = RecoveryCoordinator(model.replica().orders, model.replica().positions)
        gateway = RecordingGateway()
        service = ExecutionService(store=store, model=model, gateway=gateway, recovery=recovery, wall_time=lambda: NOW)
        recovery.start_recovery(expected_trading_day=DAY)
        recovery.begin_reconciliation()

        def query(kind, records=()):
            return QueryResult(
                batch=QueryBatch(kind, ACCOUNT, DAY, NOW),
                records=records,
                available_at=NOW,
                source_id="paper",
                source_version="1",
                complete=True,
            )

        recovery.merge_order_query(query("orders"))
        recovery.merge_trade_query(query("trades"))
        recovery.reconcile_positions(query("positions"))
        recovery.reconcile_funds(
            query("funds", (AccountFunds(Decimal(1000), Decimal(1000), Decimal(0), Decimal(1000)),)),
            model.ledger.balance,
        )
        service.enable_after_reconciliation()
        metadata = {
            "account_id": ACCOUNT,
            "strategy_id": "strategy",
            "run_id": "run-one",
            "control": CONTROL,
            "start_cursor": journal.head_seq,
            "configuration": "test-v1",
        }
        yield SimpleNamespace(
            path=path,
            journal=journal,
            store=store,
            client=client,
            model=model,
            service=service,
            gateway=gateway,
            metadata=metadata,
            state=tmp_path / "strategy.db",
        )


def market(seq=1):
    end = NOW + timedelta(minutes=seq)
    bar = Bar(
        instrument=RB,
        meta=RecordMeta(
            event_time=end,
            available_at=end,
            ingested_at=end,
            trading_day=DAY,
            source_id="paper",
            source_version="1",
            ingest_seq=seq,
        ),
        bar_start=end - timedelta(minutes=1),
        bar_end=end,
        open_time=end - timedelta(minutes=1),
        interval="1m",
        open=Decimal(100),
        high=Decimal(100),
        low=Decimal(100),
        close=Decimal(100),
        volume=1,
        turnover=Decimal(100),
        open_interest=1,
        includes_auction=False,
    )
    return CanonicalEvent(
        event_id=f"bar-{seq}",
        kind=EventKind.MARKET_DATA,
        event_time=end,
        available_at=end,
        sequence=0,
        source_id="paper",
        payload=bar,
    )


def fact(paper, event):
    paper.service.enqueue(event)
    paper.service.run_once()


class Buyer(StrategyBase):
    def __init__(self, context, *, size=1, crash=False):
        super().__init__("strategy", context)
        self.size, self.crash = size, crash
        self.bars, self.updates, self.trades, self.ids = [], [], [], []

    def on_bar(self, bar):
        self.bars.append(bar)
        self.ids.append(self.buy(RB, self.size, limit_price_ticks=100))
        if self.crash:
            raise RuntimeError("callback crash")

    def on_order(self, update):
        self.updates.append(update)

    def on_trade(self, trade):
        self.trades.append(trade)


def engine_for(paper, runtime, *, engine_type=LiveStrategyEngine, **kwargs):
    engine = engine_type(journal=paper.client, runtime=runtime, **kwargs)
    strategy = Buyer(engine)
    engine.start(strategy)
    return engine, strategy


@contextmanager
def execution_heartbeat(paper):
    path = paper.path.with_name("execution-heartbeat.json")
    writer = HeartbeatFile(path, role="execution", instance_id=CONTROL.controller_id)
    stopped = threading.Event()

    def write():
        while not stopped.is_set():
            writer.beat(control_epoch=CONTROL.epoch, ready=True)
            stopped.wait(0.02)

    thread = threading.Thread(target=write, daemon=True)
    thread.start()
    try:
        yield path
    finally:
        stopped.set()
        thread.join(timeout=2)


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail("timed out waiting for child strategy progress")


@pytest.mark.parametrize("engine_type", [JournalStrategyEngine, LiveStrategyEngine], ids=["journal", "legacy-import"])
def test_strategy_journal_command_risk_and_trade_round_trip(paper, engine_type):
    with SQLiteStrategyRuntimeStore(paper.state, metadata=paper.metadata) as runtime:
        engine, strategy = engine_for(paper, runtime, engine_type=engine_type)
        assert isinstance(engine, StrategyContextPort)
        assert isinstance(runtime, StrategyRuntimeStorePort)
        assert isinstance(paper.client, StrategyJournalPort)
        # 未提交行情不可见。
        paper.service.enqueue(market())
        assert engine.run_once() == 0
        paper.service.run_once()
        engine.run_once()
        assert len(strategy.bars) == 1 and len(paper.gateway.calls) == 0
        command = paper.client.commands()[0].command
        assert command.control == CONTROL and command.payload.strategy_id == "strategy"
        assert paper.client.get(command.command_id).status == CommandStatus.PENDING
        paper.service.process_next_command()
        assert len(paper.gateway.calls) == 1
        assert paper.model.ledger.get_funds_reservation(command.payload.client_order_id).margin == Decimal(100)
        engine.run_once()
        identity = OrderIdentity(
            account_id=ACCOUNT, exchange=Exchange.SHFE, client_order_id=strategy.ids[0], exchange_order_id="EX1"
        )
        update = OrderUpdate(
            identity=identity,
            instrument=RB,
            side=Side.BUY,
            offset=Offset.OPEN,
            status=OrderStatus.ACCEPTED,
            quantity=1,
            filled_quantity=0,
            event_time=NOW,
            available_at=NOW,
        )
        fact(
            paper,
            CanonicalEvent(
                event_id="order",
                kind=EventKind.ORDER_REPORT,
                event_time=NOW,
                available_at=NOW,
                sequence=0,
                source_id="paper",
                payload=update,
            ),
        )
        trade = Trade(
            account_id=ACCOUNT,
            instrument=RB,
            trading_day=DAY,
            trade_id="T1",
            side=Side.BUY,
            offset=Offset.OPEN,
            quantity=1,
            price=Decimal(100),
            event_time=NOW,
            available_at=NOW,
            order_identity=identity,
            deduplication_key=TradeKey(ACCOUNT, Exchange.SHFE, DAY, "T1"),
        )
        fact(
            paper,
            CanonicalEvent(
                event_id="trade",
                kind=EventKind.TRADE_REPORT,
                event_time=NOW,
                available_at=NOW,
                sequence=0,
                source_id="paper",
                payload=trade,
            ),
        )
        engine.run_once()
        assert len(strategy.updates) == 1 and strategy.trades == [trade]
        assert engine.get_position(RB) == 1
        assert not engine.is_order_active(strategy.ids[0])
        engine.stop()
    with SQLiteStrategyRuntimeStore(paper.state, metadata=paper.metadata) as runtime:
        resumed, replayed = engine_for(paper, runtime, engine_type=engine_type)
        assert replayed.ids == strategy.ids and resumed.get_position(RB) == 1
        assert resumed.run_once() == 0 and len(paper.client.commands()) == 1


def test_insufficient_funds_is_rejected_by_shared_model_and_reported_to_strategy(paper):
    with SQLiteStrategyRuntimeStore(paper.state, metadata=paper.metadata) as runtime:
        engine = LiveStrategyEngine(journal=paper.client, runtime=runtime)
        strategy = Buyer(engine, size=11)
        engine.start(strategy)
        fact(paper, market())
        engine.run_once()
        paper.service.process_next_command()
        engine.run_once()
        assert paper.client.commands()[0].status == CommandStatus.REJECTED
        assert not paper.gateway.calls
        assert strategy.updates[-1].status == OrderStatus.REJECTED
        assert not engine.is_order_active(strategy.ids[0])


def test_callback_failure_never_publishes_partial_intents(paper):
    with SQLiteStrategyRuntimeStore(paper.state, metadata=paper.metadata) as runtime:
        beats = []
        engine = LiveStrategyEngine(journal=paper.client, runtime=runtime, heartbeat=beats.append)
        engine.start(Buyer(engine, crash=True))
        fact(paper, market())
        with pytest.raises(RuntimeError, match="callback crash"):
            engine.run_once()
        assert not paper.client.commands() and not runtime.pending()
        assert runtime.cursor == paper.metadata["start_cursor"]
        assert beats[-1] is False and not engine.ready


@pytest.mark.parametrize("crash_after_insert", [False, True])
def test_crash_outbox_retries_exact_command_id_once(paper, monkeypatch, crash_after_insert):
    submit = paper.client.submit
    captured = []

    def interrupted(command):
        captured.append(command)
        if crash_after_insert:
            submit(command)
        raise OSError("simulated loss before acknowledgment")

    with SQLiteStrategyRuntimeStore(paper.state, metadata=paper.metadata) as runtime:
        engine, _ = engine_for(paper, runtime)
        fact(paper, market())
        monkeypatch.setattr(paper.client, "submit", interrupted)
        with pytest.raises(OSError):
            engine.run_once()
        assert len(runtime.pending()) == 1
        assert runtime.cursor == paper.metadata["start_cursor"]
    monkeypatch.setattr(paper.client, "submit", submit)
    with SQLiteStrategyRuntimeStore(paper.state, metadata=paper.metadata) as runtime:
        resumed, strategy = engine_for(paper, runtime)
        resumed.run_once()
        assert paper.client.commands()[0].command == captured[0]
        assert len(paper.client.commands()) == 1 and len(strategy.bars) == 1
        assert not runtime.pending()


def test_replay_divergence_stops_before_outbox_is_sent(paper, monkeypatch):
    submit = paper.client.submit
    with SQLiteStrategyRuntimeStore(paper.state, metadata=paper.metadata) as runtime:
        engine, _ = engine_for(paper, runtime)
        fact(paper, market())
        monkeypatch.setattr(paper.client, "submit", lambda command: (_ for _ in ()).throw(OSError("crash")))
        with pytest.raises(OSError):
            engine.run_once()
    monkeypatch.setattr(paper.client, "submit", submit)
    with SQLiteStrategyRuntimeStore(paper.state, metadata=paper.metadata) as runtime:
        engine = LiveStrategyEngine(journal=paper.client, runtime=runtime)
        with pytest.raises(StrategyReplayError):
            engine.start(Buyer(engine, size=2))
        assert not paper.client.commands()


def test_new_runtime_starts_at_current_head_without_historical_orders(paper):
    fact(paper, market())
    metadata = paper.metadata | {"start_cursor": paper.journal.head_seq}
    with SQLiteStrategyRuntimeStore(paper.state, metadata=metadata) as runtime:
        engine, strategy = engine_for(paper, runtime)
        assert engine.run_once() == 0 and strategy.bars == []
        assert not paper.client.commands()


def test_epoch_change_stops_existing_process_and_restart(paper):
    with SQLiteStrategyRuntimeStore(paper.state, metadata=paper.metadata) as runtime:
        engine, _ = engine_for(paper, runtime)
        cp = paper.store.checkpoint()
        next_control = ControlEpoch("other-controller", 2)
        paper.journal.append(
            JournalTransaction(
                transaction_id="takeover",
                events=(),
                cursor_before=cp.cursor,
                cursor_after=cp.cursor,
                control_record=ControlRecord(next_control, NOW, cp.journal_seq + 1),
            )
        )
        with pytest.raises(ExecutionNotReadyError, match="epoch changed"):
            engine.run_once()
        assert not engine.ready and not paper.client.commands()
    with SQLiteStrategyRuntimeStore(paper.state, metadata=paper.metadata) as runtime:
        engine = LiveStrategyEngine(journal=paper.client, runtime=runtime)
        with pytest.raises(ExecutionNotReadyError):
            engine.start(Buyer(engine))
    with pytest.raises(JournalConflictError, match="binding"):
        SQLiteStrategyRuntimeStore(paper.state, metadata=paper.metadata | {"control": next_control})


def test_one_strategy_instance_lock_is_separate_from_execution_lock(paper):
    with SQLiteStrategyRuntimeStore(paper.state, metadata=paper.metadata):
        paper.store.assert_owner()
        with pytest.raises(ExecutionOwnershipError):
            SQLiteStrategyRuntimeStore(paper.state, metadata=paper.metadata)


def test_callbacks_cannot_submit_in_lifecycle_or_impersonate_another_strategy(paper):
    with SQLiteStrategyRuntimeStore(paper.state, metadata=paper.metadata) as runtime:
        engine, _ = engine_for(paper, runtime)
        with pytest.raises(ExecutionNotReadyError):
            engine.buy(RB, 1)
        with pytest.raises(ValueError, match="another strategy"):
            engine.buy(RB, 1, strategy_id="other")
        with pytest.raises(ValueError, match="does not own"):
            engine.cancel_order("someone-else")
        with pytest.raises(NotImplementedError, match="committed"):
            engine.schedule_timer(NOW, "timer")


def test_strategy_cli_runs_without_gateway_and_writes_stopped_heartbeat(paper):
    with execution_heartbeat(paper) as heartbeat:
        assert (
            main(
                [
                    "--journal",
                    str(paper.path),
                    "--account",
                    ACCOUNT,
                    "--strategy-id",
                    "cli",
                    "--run-id",
                    "test",
                    "--controller",
                    CONTROL.controller_id,
                    "--epoch",
                    "1",
                    "--instrument",
                    str(RB),
                    "--max-iterations",
                    "1",
                    "--execution-heartbeat",
                    str(heartbeat),
                ]
            )
            == 0
        )
    state, heartbeat = runtime_paths(paper.path, ACCOUNT, "cli")
    assert state.exists()
    beat = read_heartbeat(heartbeat)
    assert beat.role == "strategy:cli" and beat.control_epoch == 1 and not beat.ready
    assert not paper.gateway.calls and not paper.client.commands()


def test_bar_cannot_be_exposed_before_its_known_time(paper):
    with SQLiteStrategyRuntimeStore(paper.state, metadata=paper.metadata) as runtime:
        engine, _ = engine_for(paper, runtime)
        event = market()
        fact(paper, replace(event, available_at=NOW))
        with pytest.raises(ValueError, match="before its availability"):
            engine.run_once()
        assert not paper.client.commands()


def test_liveness_gate_requires_progress_and_stops_on_stale_or_unready_file(tmp_path):
    clock = [0.0]
    writer = HeartbeatFile(tmp_path / "execution.json", role="execution", instance_id=CONTROL.controller_id)
    writer.beat(control_epoch=1, ready=True)
    gate = ExecutionLivenessGate(writer.path, 1, 5, controller_id=CONTROL.controller_id, monotonic=lambda: clock[0])
    assert not gate()  # 遗留的 READY 文件不是存活证明。
    writer.beat(control_epoch=1, ready=True)
    assert gate()
    clock[0] = 6
    assert not gate()
    writer.beat(control_epoch=1, ready=True)
    assert gate()
    writer.beat(control_epoch=1, ready=False)
    assert not gate()
    writer.beat(control_epoch=2, ready=True)
    assert not gate()


def test_engine_stops_before_consuming_when_execution_heartbeat_loses_readiness(paper):
    alive = [True]
    with SQLiteStrategyRuntimeStore(paper.state, metadata=paper.metadata) as runtime:
        engine, strategy = engine_for(paper, runtime, execution_ready=lambda: alive[0])
        fact(paper, market())
        alive[0] = False
        with pytest.raises(ExecutionNotReadyError, match="heartbeat"):
            engine.run_once()
        assert strategy.bars == [] and not paper.client.commands()
        assert runtime.cursor == paper.metadata["start_cursor"]


@pytest.mark.parametrize("file_shape", ["missing", "empty", "tables"])
@pytest.mark.parametrize("exposure", ["pending", "active", "position"])
def test_new_cli_runtime_refuses_nonflat_account_even_with_empty_or_partial_database(paper, file_shape, exposure):
    with SQLiteStrategyRuntimeStore(paper.state, metadata=paper.metadata) as runtime:
        engine, _ = engine_for(paper, runtime)
        fact(paper, market())
        engine.run_once()
        assert paper.client.commands()[0].status == CommandStatus.PENDING
        if exposure != "pending":
            paper.service.process_next_command()
        if exposure == "position":
            commit_trade(paper, paper.client.commands()[0].command.command_id, "initial-position", 1)
    state, _ = runtime_paths(paper.path, ACCOUNT, "fresh")
    if file_shape == "empty":
        state.touch()
    elif file_shape == "tables":
        with sqlite3.connect(state) as connection:
            connection.execute(
                "CREATE TABLE strategy_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                "payload TEXT NOT NULL, sha256 TEXT NOT NULL, cursor INTEGER NOT NULL)"
            )
    with execution_heartbeat(paper) as heartbeat:
        assert (
            main(
                [
                    "--journal",
                    str(paper.path),
                    "--account",
                    ACCOUNT,
                    "--strategy-id",
                    "fresh",
                    "--run-id",
                    "new",
                    "--controller",
                    CONTROL.controller_id,
                    "--epoch",
                    "1",
                    "--instrument",
                    str(RB),
                    "--max-iterations",
                    "1",
                    "--execution-heartbeat",
                    str(heartbeat),
                ]
            )
            == 2
        )
    with sqlite3.connect(state) as connection:
        assert connection.execute("SELECT COUNT(*) FROM strategy_meta").fetchone()[0] == 0
    assert len(paper.client.commands()) == 1


@pytest.mark.parametrize(
    "view", [None, {}, {"positions": ()}, {"active_orders": 0}, "invalid", {"positions": (), "active_orders": "0"}]
)
def test_new_runtime_requires_complete_committed_account_view(paper, view):
    cp = paper.store.checkpoint()
    paper.journal.append(
        JournalTransaction(
            transaction_id="incomplete-view",
            events=(),
            cursor_before=cp.cursor,
            cursor_after=cp.cursor,
            state_updates={"account_view": view},
        )
    )
    with execution_heartbeat(paper) as heartbeat:
        assert (
            main(
                [
                    "--journal",
                    str(paper.path),
                    "--account",
                    ACCOUNT,
                    "--strategy-id",
                    "fresh",
                    "--run-id",
                    "new",
                    "--controller",
                    CONTROL.controller_id,
                    "--epoch",
                    "1",
                    "--instrument",
                    str(RB),
                    "--max-iterations",
                    "1",
                    "--execution-heartbeat",
                    str(heartbeat),
                ]
            )
            == 2
        )
    state, _ = runtime_paths(paper.path, ACCOUNT, "fresh")
    with sqlite3.connect(state) as connection:
        assert connection.execute("SELECT COUNT(*) FROM strategy_meta").fetchone()[0] == 0
    assert not paper.client.commands()


def commit_trade(paper, command_id, trade_id, quantity, *, identity=None):
    intent = paper.client.get(command_id).command.payload
    identity = identity or OrderIdentity(
        account_id=ACCOUNT, exchange=Exchange.SHFE, client_order_id=intent.client_order_id
    )
    trade = Trade(
        account_id=ACCOUNT,
        instrument=RB,
        trading_day=DAY,
        trade_id=trade_id,
        side=intent.side,
        offset=intent.offset,
        quantity=quantity,
        price=Decimal(100),
        event_time=NOW,
        available_at=NOW,
        order_identity=identity,
        deduplication_key=TradeKey(ACCOUNT, Exchange.SHFE, DAY, trade_id),
    )
    fact(
        paper,
        CanonicalEvent(
            event_id=trade_id,
            kind=EventKind.TRADE_REPORT,
            event_time=NOW,
            available_at=NOW,
            sequence=0,
            source_id="paper",
            payload=trade,
        ),
    )


def commit_report(paper, command_id, status, filled, *, identity=None):
    intent = paper.client.get(command_id).command.payload
    report = OrderUpdate(
        identity=identity
        or OrderIdentity(account_id=ACCOUNT, exchange=Exchange.SHFE, client_order_id=intent.client_order_id),
        instrument=RB,
        side=intent.side,
        offset=intent.offset,
        status=status,
        quantity=intent.quantity,
        filled_quantity=filled,
        event_time=NOW,
        available_at=NOW,
    )
    fact(
        paper,
        CanonicalEvent(
            event_id=f"{command_id}-{status}-{filled}",
            kind=EventKind.ORDER_REPORT,
            event_time=NOW,
            available_at=NOW,
            sequence=0,
            source_id="paper",
            payload=report,
        ),
    )


def feed_closes(paper, closes, start=1):
    for seq, price in enumerate(closes, start=start):
        event = market(seq)
        bar = replace(event.payload, open=Decimal(price), high=Decimal(price), low=Decimal(price), close=Decimal(price))
        fact(paper, replace(event, payload=bar))


def async_engine(paper, runtime, size=2):
    engine = LiveStrategyEngine(journal=paper.client, runtime=runtime)
    strategy = AsyncDualMovingAverageStrategy("strategy", engine, RB, fast_window=1, slow_window=2, order_size=size)
    engine.start(strategy)
    return engine, strategy


def test_async_dma_backlog_partial_fills_restart_and_two_leg_reversal(paper):
    with SQLiteStrategyRuntimeStore(paper.state, metadata=paper.metadata) as runtime:
        engine, strategy = async_engine(paper, runtime)
        feed_closes(paper, (100, 100, 110, 90, 110))
        engine.run_once()
        assert len(paper.client.commands()) == 1
        first = paper.client.commands()[0].command
        assert (first.payload.side, first.payload.offset, first.payload.quantity) == (Side.BUY, Offset.OPEN, 2)
        assert tuple(strategy._closes) == (Decimal(90), Decimal(110))
        assert strategy._target == 2 and engine.get_position(RB) == 0
        paper.service.process_next_command()
        commit_trade(paper, first.command_id, "partial-one", 1)
        engine.run_once()
        assert engine.get_position(RB) == 1
        feed_closes(paper, (90, 110, 90), start=6)
        engine.run_once()
        assert len(paper.client.commands()) == 1 and strategy._target == -2
        engine.stop()
    with SQLiteStrategyRuntimeStore(paper.state, metadata=paper.metadata) as runtime:
        engine, strategy = async_engine(paper, runtime)
        assert engine.get_position(RB) == 1 and strategy._pending_id == first.command_id
        assert len(paper.client.commands()) == 1
        # 全成委托回报先到，第二笔真实成交尚未提交：不得先平仓或开反向仓。
        commit_report(paper, first.command_id, OrderStatus.FILLED, 2)
        engine.run_once()
        feed_closes(paper, (80,), start=9)
        engine.run_once()
        assert len(paper.client.commands()) == 1
        commit_trade(paper, first.command_id, "partial-two", 1)
        engine.run_once()
        closing = paper.client.commands()[0].command
        assert len(paper.client.commands()) == 2
        assert (closing.payload.side, closing.payload.offset, closing.payload.quantity) == (
            Side.SELL,
            Offset.CLOSE_TODAY,
            2,
        )
        paper.service.process_next_command()
        commit_report(paper, closing.command_id, OrderStatus.FILLED, 2)
        engine.run_once()
        assert len(paper.client.commands()) == 2
        commit_trade(paper, closing.command_id, "close-one", 1)
        engine.run_once()
        assert len(paper.client.commands()) == 2 and engine.get_position(RB) == 1
        commit_trade(paper, closing.command_id, "close-two", 1)
        engine.run_once()
        opening = paper.client.commands()[0].command
        assert len(paper.client.commands()) == 3 and engine.get_position(RB) == 0
        assert (opening.payload.side, opening.payload.offset, opening.payload.quantity) == (Side.SELL, Offset.OPEN, 2)


@pytest.mark.parametrize("terminal", [OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.EXPIRED])
def test_async_dma_terminal_abandons_old_target_and_waits_for_fresh_cross(paper, terminal):
    with SQLiteStrategyRuntimeStore(paper.state, metadata=paper.metadata) as runtime:
        engine, strategy = async_engine(paper, runtime)
        feed_closes(paper, (100, 100, 110))
        engine.run_once()
        first = paper.client.commands()[0].command
        paper.service.process_next_command()
        commit_report(paper, first.command_id, terminal, 0)
        engine.run_once()
        assert strategy._target is None and strategy._pending_id is None
        feed_closes(paper, (120,), start=4)
        engine.run_once()
        assert len(paper.client.commands()) == 1
        feed_closes(paper, (90,), start=5)
        engine.run_once()
        second = paper.client.commands()[0].command
        assert len(paper.client.commands()) == 2
        assert (second.payload.side, second.payload.offset, second.payload.quantity) == (Side.SELL, Offset.OPEN, 2)


def test_async_dma_shared_risk_rejection_clears_inflight_without_retry(paper):
    with SQLiteStrategyRuntimeStore(paper.state, metadata=paper.metadata) as runtime:
        engine, strategy = async_engine(paper, runtime, size=11)
        feed_closes(paper, (100, 100, 110))
        engine.run_once()
        paper.service.process_next_command()
        engine.run_once()
        assert paper.client.commands()[0].status == CommandStatus.REJECTED
        assert strategy._pending_id is None and strategy._target is None
        feed_closes(paper, (120, 130), start=4)
        engine.run_once()
        assert len(paper.client.commands()) == 1 and not paper.gateway.calls


def test_async_dma_remote_only_identities_and_late_trade_linking(paper, monkeypatch):
    original_submit = paper.gateway.submit
    session = OrderIdentity(account_id=ACCOUNT, exchange=Exchange.SHFE, front_id=1, session_id=2, order_ref="1")

    def submit(intent, epoch):
        original_submit(intent, epoch)
        return LocalSendResult(SendState.SENT_UNKNOWN, 0, "paper-session", remote_identity=session)

    monkeypatch.setattr(paper.gateway, "submit", submit)
    with SQLiteStrategyRuntimeStore(paper.state, metadata=paper.metadata) as runtime:
        engine, strategy = async_engine(paper, runtime)
        feed_closes(paper, (100, 100, 110))
        engine.run_once()
        first = paper.client.commands()[0].command
        paper.service.process_next_command()
        engine.run_once()
        # 成交只带尚未关联的交易所单号；随后委托回报提供会话三元组与交易所单号的连接。
        exchange_only = OrderIdentity(account_id=ACCOUNT, exchange=Exchange.SHFE, exchange_order_id="EX-remote")
        commit_trade(paper, first.command_id, "remote-trade", 2, identity=exchange_only)
        engine.run_once()
        assert strategy._pending_id == first.command_id and engine.get_position(RB) == 0
        commit_report(
            paper, first.command_id, OrderStatus.FILLED, 2, identity=replace(session, exchange_order_id="EX-remote")
        )
        engine.run_once()
        assert strategy._pending_id is None and engine.get_position(RB) == 2
        assert strategy._inventory[(Side.BUY, DAY)] == 2
        _, events = paper.client.read_events(0)
        original = next(event.payload for event in events if event.event_id == "remote-trade")
        assert original.order_identity.client_order_id is None  # 只修改策略回调副本。


def test_execution_heartbeat_regression_and_wrong_instance_revoke_liveness(tmp_path):
    path = tmp_path / "execution.json"
    writer = HeartbeatFile(path, role="execution", instance_id=CONTROL.controller_id)
    gate = ExecutionLivenessGate(path, 1, 5, controller_id=CONTROL.controller_id)
    writer.beat(control_epoch=1, ready=True)
    assert not gate()
    writer.beat(control_epoch=1, ready=True)
    assert gate()
    writer.sequence = 0
    writer.beat(control_epoch=1, ready=True)
    assert not gate()  # 同一实例序号倒退清空观察窗口，不能当作推进。
    assert not gate()
    writer.beat(control_epoch=1, ready=True)
    assert gate()
    other = HeartbeatFile(path, role="execution", instance_id="another-controller")
    other.beat(control_epoch=1, ready=True)
    assert not gate()


def test_async_dma_closes_yesterday_and_today_serially_before_reversal():
    class Context:
        def __init__(self):
            self.position = 0
            self.intents = []

        def get_position(self, instrument):
            return self.position

        def buy(self, instrument, quantity, offset, limit_price_ticks=None, *, strategy_id):
            self.intents.append((Side.BUY, offset, quantity))
            return str(len(self.intents))

        def sell(self, instrument, quantity, offset, limit_price_ticks=None, *, strategy_id):
            self.intents.append((Side.SELL, offset, quantity))
            return str(len(self.intents))

    context = Context()
    strategy = AsyncDualMovingAverageStrategy("strategy", context, RB, fast_window=1, slow_window=2, order_size=2)
    for seq, price in enumerate((100, 100, 110), start=1):
        bar = market(seq).payload
        strategy.on_bar(
            replace(bar, open=Decimal(price), high=Decimal(price), low=Decimal(price), close=Decimal(price))
        )
    assert context.intents == [(Side.BUY, Offset.OPEN, 2)]

    def trade(order_id, identifier, side, offset, day, position):
        context.position = position
        strategy.on_trade(
            Trade(
                account_id=ACCOUNT,
                instrument=RB,
                trading_day=day,
                trade_id=identifier,
                side=side,
                offset=offset,
                quantity=1,
                price=Decimal(100),
                event_time=NOW,
                available_at=NOW,
                order_identity=OrderIdentity(account_id=ACCOUNT, exchange=Exchange.SHFE, client_order_id=order_id),
                deduplication_key=TradeKey(ACCOUNT, Exchange.SHFE, day, identifier),
            )
        )

    trade("1", "day-one-fill", Side.BUY, Offset.OPEN, DAY, 1)
    next_day = DAY + timedelta(days=1)
    bar = market(4).payload
    strategy.on_bar(
        replace(
            bar,
            meta=replace(bar.meta, trading_day=next_day),
            open=Decimal(90),
            high=Decimal(90),
            low=Decimal(90),
            close=Decimal(90),
        )
    )
    assert len(context.intents) == 1
    trade("1", "day-two-fill", Side.BUY, Offset.OPEN, next_day, 2)
    assert context.intents[-1] == (Side.SELL, Offset.CLOSE_YESTERDAY, 1)
    trade("2", "close-old", Side.SELL, Offset.CLOSE_YESTERDAY, next_day, 1)
    assert context.intents[-1] == (Side.SELL, Offset.CLOSE_TODAY, 1)
    trade("3", "close-today", Side.SELL, Offset.CLOSE_TODAY, next_day, 0)
    assert context.intents[-1] == (Side.SELL, Offset.OPEN, 2)
    assert len(context.intents) == 4


def test_strategy_refreshes_heartbeat_inside_large_committed_batch(paper):
    clock = [0.0]
    beats = []

    class SlowObserver(Buyer):
        def on_bar(self, bar):
            self.bars.append(bar)
            clock[0] += 0.2

    with SQLiteStrategyRuntimeStore(paper.state, metadata=paper.metadata) as runtime:
        engine = LiveStrategyEngine(
            journal=paper.client,
            runtime=runtime,
            heartbeat=lambda ready: beats.append((clock[0], ready)),
            heartbeat_interval=0.5,
            monotonic=lambda: clock[0],
        )
        strategy = SlowObserver(engine)
        engine.start(strategy)
        feed_closes(paper, (100,) * 20)
        engine.run_once()
        assert len(strategy.bars) == 20
        times = [stamp for stamp, ready in beats if ready]
        assert len(times) >= 7 and max(right - left for left, right in zip(times, times[1:], strict=False)) <= 0.61


def test_strategy_child_process_subscribes_committed_bars_and_restarts_without_resending(paper):
    root = Path(__file__).resolve().parents[2]
    state, heartbeat = runtime_paths(paper.path, ACCOUNT, "process-dma")
    with execution_heartbeat(paper) as execution_beat:
        command = [
            sys.executable,
            str(root / "scripts/run_strategy.py"),
            "--journal",
            str(paper.path),
            "--account",
            ACCOUNT,
            "--strategy-id",
            "process-dma",
            "--run-id",
            "subprocess-test",
            "--controller",
            CONTROL.controller_id,
            "--epoch",
            "1",
            "--instrument",
            str(RB),
            "--execution-heartbeat",
            str(execution_beat),
            "--fast-window",
            "1",
            "--slow-window",
            "2",
            "--interval",
            "0.05",
            "--max-iterations",
            "70",
        ]
        child = subprocess.Popen(
            command,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            wait_for(lambda: (beat := read_heartbeat(heartbeat)) is not None and beat.ready)
            assert state.exists()
            # 两根平价 Bar 预热，第三根完成 Bar 形成金叉；父进程仍持有唯一执行锁。
            for seq, price in enumerate((100, 100, 110), start=1):
                event = market(seq)
                bar = replace(
                    event.payload, open=Decimal(price), high=Decimal(price), low=Decimal(price), close=Decimal(price)
                )
                fact(paper, replace(event, payload=bar))
            wait_for(lambda: bool(paper.client.commands()))
            queued = paper.client.commands()[0]
            assert queued.status == CommandStatus.PENDING
            assert queued.command.control == CONTROL
            assert queued.command.payload.strategy_id == "process-dma"
            assert not paper.gateway.calls
            paper.service.process_next_command()
            assert len(paper.gateway.calls) == 1
            stdout, stderr = child.communicate(timeout=8)
            assert child.returncode == 0, (stdout, stderr)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=3)
        # 先前账户仍有活动单，但既有策略状态允许恢复；不得把已消费信号再次发单。
        restarted = subprocess.run(
            command[:-1] + ["1"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert restarted.returncode == 0, (restarted.stdout, restarted.stderr)
        assert len(paper.client.commands()) == 1 and len(paper.gateway.calls) == 1
        assert not read_heartbeat(heartbeat).ready
