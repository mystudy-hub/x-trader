"""S5-04: real SQLite/process failures and a staged S2 risk/ledger test adapter.

The adapter below deliberately covers a small, fixed account fixture. It is not a
live account assembly and is never shipped as a permissive execution default.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from contextlib import ExitStack
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from threading import Thread

import pytest

from qh_trader.core.constants import (
    EventKind,
    Exchange,
    JournalConflictError,
    Offset,
    OrderType,
    SendState,
    Side,
)
from qh_trader.core.event import CanonicalEvent, JournalTransaction
from qh_trader.core.execution import (
    CommandKind,
    CommandPlan,
    CommandStatus,
    ExecutionCommand,
    ExecutionNotReadyError,
    ExecutionOwnershipError,
    TakeoverRequest,
)
from qh_trader.core.objects import (
    AccountFunds,
    ControlEpoch,
    ControlRecord,
    InstrumentId,
    LocalSendResult,
    OrderIdentity,
    OrderIntent,
    QueryBatch,
    QueryResult,
    Trade,
    TradeKey,
)
from qh_trader.core.ports import ExecutionModelPort, ExecutionStorePort
from qh_trader.domain.ledger import AccountLedger
from qh_trader.domain.orders import OrderManager
from qh_trader.domain.positions import PositionManager
from qh_trader.domain.recovery import RecoveryCoordinator
from qh_trader.domain.risk import RiskManager, RiskViolationError
from qh_trader.engine.execution_service import ExecutionService
from qh_trader.infrastructure import journal_codec
from qh_trader.infrastructure.command_queue import SQLiteCommandClient, SQLiteExecutionStore
from qh_trader.infrastructure.journal import SQLiteJournal

NOW = datetime(2024, 9, 10, 1, tzinfo=timezone.utc)
DAY = date(2024, 9, 10)
ACCOUNT = "execution-test-account"
CONTROL = ControlEpoch("strategy-controller", 1)
INSTRUMENT = InstrumentId(Exchange.SHFE, "rb2410")
ROOT = Path(__file__).resolve().parents[2]
PROCESS_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def command(identifier="one", *, kind=CommandKind.SUBMIT, control=CONTROL, quantity=1):
    if kind == CommandKind.SUBMIT:
        payload = OrderIntent(
            client_order_id=identifier,
            account_id=ACCOUNT,
            strategy_id="fixture-strategy",
            instrument=INSTRUMENT,
            side=Side.BUY,
            offset=Offset.OPEN,
            quantity=quantity,
            order_type=OrderType.LIMIT,
            created_at=NOW,
            limit_price_ticks=3500,
        )
    elif kind == CommandKind.CANCEL:
        payload = OrderIdentity(account_id=ACCOUNT, exchange=Exchange.SHFE, client_order_id=identifier)
    elif kind == CommandKind.TAKEOVER_REQUEST:
        payload = TakeoverRequest(controller_id="replacement-controller", reason="fixture takeover")
    else:
        payload = {"reason": "fixture control"}
    return ExecutionCommand(
        command_id=identifier, account_id=ACCOUNT, producer_id="fixture-producer",
        control=control, kind=kind, submitted_at=NOW, payload=payload,
    )


def event(identifier, *, kind=EventKind.CONTROL):
    payload = {"fixture_fact": identifier}
    if kind == EventKind.TRADE_REPORT:
        payload = Trade(
            account_id=ACCOUNT,
            instrument=INSTRUMENT,
            trading_day=DAY,
            trade_id=identifier,
            side=Side.BUY,
            offset=Offset.OPEN,
            quantity=1,
            price=Decimal("3500"),
            event_time=NOW,
            available_at=NOW,
            deduplication_key=TradeKey(ACCOUNT, Exchange.SHFE, DAY, identifier),
        )
    return CanonicalEvent(
        event_id=identifier, kind=kind, event_time=NOW, available_at=NOW,
        sequence=0, source_id="fixture-old-gateway", payload=payload,
    )


class StagedAccountFixture:
    def __init__(self):
        self.state = {}
        self.published = []
        self.command_stages = []
        self.after_publish = None

    def publish(self, checkpoint):
        self.state = checkpoint.state
        self.published.append(checkpoint)
        if self.after_publish is not None:
            self.after_publish(checkpoint)

    def stage_command(self, request):
        self.command_stages.append(request)
        reservations = dict(self.state.get("reservations", {}))
        intents = dict(self.state.get("intents", {}))
        if request.kind == CommandKind.SUBMIT:
            order = request.payload
            if order.client_order_id in intents:
                return CommandPlan(approved=False, reason="duplicate client order")
            # Exercise real S2 atomic risk/reservations on a private candidate.
            ledger = AccountLedger(account_id=ACCOUNT, initial_capital=Decimal("1000"), trading_day=DAY)
            orders = OrderManager()
            for identity, amount in reservations.items():
                previous = intents[identity]
                ledger.reserve_funds(identity, amount, Decimal("0"))
                ledger.position_manager.reserve_for_order(
                    identity, previous.instrument, previous.side, previous.offset, previous.quantity,
                )
                orders.create_order(previous)
            risk = RiskManager(ACCOUNT, control=request.control, trading_day=DAY)
            amount = Decimal("100") * order.quantity
            try:
                risk.check_and_reserve(
                    order, request.control, ledger, ledger.position_manager,
                    list(orders.orders()), amount, Decimal("0"), DAY,
                )
            except RiskViolationError as exc:
                return CommandPlan(approved=False, reason=str(exc))
            reservations[order.client_order_id] = amount
            intents[order.client_order_id] = order
        return CommandPlan(approved=True, reason="fixture domain checks passed", state_updates={
            "reservations": reservations, "intents": intents,
        })

    def stage_send_result(self, request, result):
        reservations = dict(self.state.get("reservations", {}))
        if request.kind == CommandKind.SUBMIT and result.state == SendState.NOT_SENT:
            reservations.pop(request.payload.client_order_id, None)
        return {"reservations": reservations}

    def stage_fact(self, fact):
        trades = tuple(self.state.get("trades", ()))
        if isinstance(fact.payload, Trade):
            trades += (fact.payload,)
        ledger = AccountLedger(account_id=ACCOUNT, initial_capital=Decimal("1000"), trading_day=DAY)
        for trade in trades:
            ledger.on_trade(trade, commission=Decimal("0"), multiplier=Decimal("10"))
        positions = tuple(position.to_position_snapshot() for position in ledger.position_manager.all_positions())
        return {
            "trades": trades, "positions": positions,
            "facts": tuple(self.state.get("facts", ())) + (fact.event_id,),
        }


class RecordingGateway:
    def __init__(self):
        self.calls = []
        self.result = LocalSendResult(SendState.SENT_UNKNOWN, 0, "accepted locally; remote result unknown")
        self.before_call = None
        self.error = None

    def submit(self, order, control):
        return self._call(order, control)

    def cancel(self, identity, control):
        return self._call(identity, control)

    def _call(self, payload, control):
        if self.before_call:
            self.before_call(payload, control)
        self.calls.append((payload, control))
        if self.error is not None:
            raise self.error
        return self.result

    def capabilities(self):
        raise AssertionError("transport tests do not supply verified broker capabilities")


class Isolation:
    def __init__(self, harness, *, allowed=True):
        self.harness = harness
        self.allowed = allowed
        self.calls = []

    def isolate(self, previous, request):
        assert not self.harness.journal.connection.in_transaction
        assert self.harness.store.control() == previous
        self.calls.append((previous, request))
        return self.allowed


class Harness:
    def __init__(self, path, **service_options):
        self.path = path
        self.stack = ExitStack()
        self.journal = self.stack.enter_context(SQLiteJournal(path, account_id=ACCOUNT))
        self.journal.migrate()
        if self.journal.head_seq == 0:
            seed = replace(event("initial-control"), sequence=1)
            self.journal.append(JournalTransaction(
                transaction_id="initial-control", events=(seed,), cursor_before=0, cursor_after=1,
                control_record=ControlRecord(CONTROL, NOW, 1),
            ))
        self.store = self.stack.enter_context(SQLiteExecutionStore(self.journal))
        self.store.migrate()
        self.client = self.stack.enter_context(SQLiteCommandClient(path, account_id=ACCOUNT))
        self.model = StagedAccountFixture()
        self.gateway = RecordingGateway()
        self.recovery = RecoveryCoordinator(OrderManager(), PositionManager(ACCOUNT))
        self.service = ExecutionService(
            store=self.store, model=self.model, gateway=self.gateway,
            recovery=self.recovery, wall_time=lambda: NOW, **service_options,
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.stack.close()

    def begin_reconciliation(self, *, include_funds=True):
        self.recovery.start_recovery(expected_trading_day=DAY)
        self.recovery.begin_reconciliation()
        self.recovery.merge_order_query(self.query("orders"))
        self.recovery.merge_trade_query(self.query("trades"))
        self.recovery.reconcile_positions(self.query("positions"))
        if include_funds:
            self.add_funds()

    def query(self, kind, records=(), **changes):
        return QueryResult(
            batch=QueryBatch(kind, ACCOUNT, DAY, NOW), records=records,
            available_at=NOW, source_id="fixture-broker", source_version="1",
            **({"complete": True} | changes),
        )

    def add_funds(self, *, balance=Decimal("1000"), **changes):
        funds = AccountFunds(balance, balance, Decimal("0"), balance)
        self.recovery.reconcile_funds(self.query("funds", (funds,), **changes), Decimal("1000"))

    def make_ready(self):
        self.begin_reconciliation()
        self.service.enable_after_reconciliation()


@pytest.fixture
def harness(tmp_path):
    with Harness(tmp_path / "trading.db") as instance:
        yield instance


@pytest.fixture
def ready_harness(harness):
    harness.make_ready()
    return harness


def test_command_contracts_codec_and_injected_ports(harness):
    assert isinstance(harness.store, ExecutionStorePort)
    assert isinstance(harness.model, ExecutionModelPort)
    for kind in CommandKind:
        request = command(kind=kind)
        assert journal_codec.loads(journal_codec.dumps(request)) == request
    with pytest.raises(TypeError, match="normalized payload"):
        replace(command(), payload={})
    with pytest.raises(ValueError, match="another account"):
        replace(command(), account_id="other")
    with pytest.raises(ValueError, match="cannot reserve"):
        CommandPlan(approved=False, reason="denied", state_updates={"reserved": 100})


def test_insert_idempotency_and_conflicting_reuse(harness):
    first = harness.client.submit(command())
    assert harness.client.submit(command()) == first
    with pytest.raises(JournalConflictError, match="different contents"):
        harness.client.submit(command(quantity=2))
    assert harness.client.get("one") == first
    assert harness.client.submission_failures == 1


def test_producer_cannot_write_journal_acknowledge_or_delete(harness):
    harness.client.submit(command())
    for sql in (
        "UPDATE command_queue SET status='DISPATCHING', processed_seq=1",
        "DELETE FROM command_queue",
        "UPDATE journal_meta SET head_seq=99",
        "INSERT INTO journal_control VALUES (1, '', '', 1)",
        "DROP TABLE command_queue",
    ):
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            harness.client._connection.execute(sql)
    with SQLiteCommandClient(harness.path, account_id=ACCOUNT, read_only=True) as reader:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.submit(command("cannot-write"))


def test_client_requires_existing_account_database(harness, tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        SQLiteCommandClient(missing, account_id=ACCOUNT)
    assert not missing.exists()
    with pytest.raises(JournalConflictError, match="another account"):
        SQLiteCommandClient(harness.path, account_id="other")


def test_failed_atomic_ack_rolls_back_intent_reservation_and_journal(ready_harness):
    h = ready_harness
    queued = h.client.submit(command())
    before = h.store.checkpoint()
    h.journal.connection.execute("""
        CREATE TRIGGER fail_command BEFORE UPDATE ON command_queue
        BEGIN SELECT RAISE(ABORT, 'injected command write failure'); END
    """)
    with pytest.raises(sqlite3.DatabaseError, match="injected"):
        h.service.process_next_command()
    assert h.client.get("one") == queued
    assert h.store.checkpoint() == before
    assert h.model.published[-1] == before
    assert h.gateway.calls == []
    assert not h.service.ready


def test_network_call_observes_committed_intent_and_does_not_hold_write_lock(ready_harness):
    h = ready_harness
    h.client.submit(command())

    def inspect_committed(payload, control):
        assert not h.journal.connection.in_transaction
        queued = h.client.get("one")
        assert queued.status == CommandStatus.DISPATCHING
        assert queued.processed_seq == h.journal.head_seq
        assert h.model.state["reservations"]["one"] == Decimal("100")
        # A separate producer can write while the network call is in progress.
        h.client.submit(command("two"))

    h.gateway.before_call = inspect_committed
    assert h.service.process_next_command()
    assert h.client.get("one").status == CommandStatus.SENT_UNKNOWN
    assert h.client.get("two").status == CommandStatus.PENDING
    assert len(h.gateway.calls) == 1


@pytest.mark.parametrize("state", [SendState.NOT_SENT, SendState.SENT_UNKNOWN])
def test_only_proven_not_sent_releases_reservations(ready_harness, state):
    h = ready_harness
    h.gateway.result = LocalSendResult(state, -1, "fixture local result")
    h.client.submit(command())
    h.service.process_next_command()
    assert ("one" in h.model.state["reservations"]) == (state == SendState.SENT_UNKNOWN)
    assert h.client.get("one").status.value == state.value


def test_exception_after_network_entry_stays_unknown_and_does_not_leak_text(ready_harness):
    h = ready_harness
    h.gateway.error = RuntimeError("password=never-persist-this")
    h.client.submit(command())
    h.service.process_next_command()
    assert h.client.get("one").status == CommandStatus.SENT_UNKNOWN
    assert h.model.state["reservations"]["one"] == Decimal("100")
    encoded = journal_codec.dumps(tuple(h.journal.replay_from(0)))
    assert "never-persist-this" not in encoded
    assert "RuntimeError" in encoded


def test_persisted_epoch_is_checked_again_immediately_before_network_call(ready_harness):
    h = ready_harness
    h.client.submit(command())

    def change_control(checkpoint):
        if h.client.get("one").status != CommandStatus.DISPATCHING:
            return
        h.model.after_publish = None
        h.journal.append(JournalTransaction(
            transaction_id="injected-control-change", events=(),
            cursor_before=checkpoint.cursor, cursor_after=checkpoint.cursor,
            control_record=ControlRecord(ControlEpoch("replacement", 2), NOW, checkpoint.journal_seq + 1),
        ))

    h.model.after_publish = change_control
    h.service.process_next_command()
    assert h.gateway.calls == []
    assert h.client.get("one").status == CommandStatus.NOT_SENT
    assert h.model.state["reservations"] == {}
    assert not h.service.ready


@pytest.mark.parametrize("kind", [kind for kind in CommandKind if kind != CommandKind.TAKEOVER_REQUEST])
@pytest.mark.parametrize(
    "control", [ControlEpoch("strategy-controller", 0), ControlEpoch("other", 1), ControlEpoch("x", 2)],
)
def test_every_ordinary_command_rejects_wrong_controller_or_epoch(harness, kind, control):
    h = harness
    h.client.submit(command(kind=kind, control=control))
    h.service.process_next_command()
    assert h.client.get("one").status == CommandStatus.REJECTED_STALE
    assert h.model.command_stages == []
    assert h.gateway.calls == []


def test_pending_takeover_neither_self_authorizes_nor_blocks_current_controller(ready_harness):
    h = ready_harness
    h.client.submit(command("takeover", kind=CommandKind.TAKEOVER_REQUEST))
    h.client.submit(command())
    h.service.process_next_command()
    assert h.store.control().epoch == CONTROL
    assert h.client.get("takeover").status == CommandStatus.PENDING
    assert len(h.gateway.calls) == 1


def test_takeover_isolates_then_advances_before_reconciliation(harness):
    h = harness
    h.client.submit(command("takeover", kind=CommandKind.TAKEOVER_REQUEST))
    isolation = Isolation(h)
    replacement = h.service.take_over("takeover", isolation)
    assert isolation.calls[0][0].epoch == CONTROL
    assert replacement == ControlEpoch("replacement-controller", 2)
    assert h.store.control().epoch == replacement
    assert not h.service.ready
    h.client.submit(command("old"))
    h.client.submit(command("new", control=replacement))
    h.service.process_next_command()
    h.service.process_next_command()
    assert h.client.get("old").status == CommandStatus.REJECTED_STALE
    assert h.client.get("new").status == CommandStatus.REJECTED
    h.begin_reconciliation(include_funds=False)
    with pytest.raises(ExecutionNotReadyError, match="funds"):
        h.service.enable_after_reconciliation()
    h.add_funds()
    h.service.enable_after_reconciliation()
    assert h.service.ready
    assert h.gateway.calls == []


def test_failed_isolation_cannot_advance_control_or_open_trading(harness):
    h = harness
    h.client.submit(command("takeover", kind=CommandKind.TAKEOVER_REQUEST))
    with pytest.raises(ExecutionNotReadyError, match="isolated"):
        h.service.take_over("takeover", Isolation(h, allowed=False))
    assert h.store.control().epoch == CONTROL
    assert h.client.get("takeover").status == CommandStatus.REJECTED
    assert not h.service.ready


@pytest.mark.parametrize("changes", [{"complete": False}, {"balance": None}, {"balance": Decimal("900")}])
def test_incomplete_or_inconsistent_funds_never_enable_trading(harness, changes):
    h = harness
    h.begin_reconciliation(include_funds=False)
    h.add_funds(**changes)
    with pytest.raises(ExecutionNotReadyError, match="reconciliation"):
        h.service.enable_after_reconciliation()
    assert not h.service.ready


def test_old_controller_trade_fact_is_booked_during_reconciliation_and_deduplicated(harness):
    h = harness
    h.client.submit(command("takeover", kind=CommandKind.TAKEOVER_REQUEST))
    h.service.take_over("takeover", Isolation(h))
    trade = event("late-trade", kind=EventKind.TRADE_REPORT)
    h.service.enqueue(trade)
    h.service.enqueue(replace(trade, event_id="duplicate-trade"))
    h.service.run_once()
    assert len(h.model.state["trades"]) == 1
    assert sum(position.pos_td for position in h.model.state["positions"]) == 1
    assert h.journal.contains_trade(trade.payload.deduplication_key)
    assert h.service.metrics["duplicate_facts"] == 1
    assert not h.service.ready
    assert h.gateway.calls == []


def test_failed_fact_commit_retains_event_without_publishing_partial_account(ready_harness):
    h = ready_harness
    trade = event("late-trade", kind=EventKind.TRADE_REPORT)
    before = h.store.checkpoint()
    h.journal.connection.execute("""
        CREATE TRIGGER fail_fact BEFORE INSERT ON journal_events
        BEGIN SELECT RAISE(ABORT, 'injected storage failure'); END
    """)
    h.service.enqueue(trade)
    with pytest.raises(sqlite3.DatabaseError, match="injected"):
        h.service.run_once()
    assert h.service.pending_fact == trade
    assert h.model.published[-1] == before
    assert not h.journal.contains_trade(trade.payload.deduplication_key)
    h.journal.connection.execute("DROP TRIGGER fail_fact")
    h.service.retry_pending_fact()
    assert h.service.pending_fact is None
    assert len(h.model.state["trades"]) == 1
    assert not h.service.ready


def test_trade_flood_still_services_cancel_and_never_prioritizes_market(tmp_path):
    with Harness(tmp_path / "flood.db", trade_batch_size=2) as h:
        h.make_ready()
        h.client.submit(command("cancel", kind=CommandKind.CANCEL))
        for index in range(5):
            h.service.enqueue(event("trade-" + str(index)))
        h.service.enqueue(event("quote", kind=EventKind.MARKET_DATA))
        assert h.service.run_once() == 3
        assert h.model.state["facts"] == ("trade-0", "trade-1")
        assert h.client.get("cancel").status == CommandStatus.SENT_UNKNOWN
        assert h.service.metrics["trade_queue_depth"] == 3
        assert h.service.metrics["market_queue_depth"] == 1


def test_callback_threads_only_enqueue_and_market_is_bounded(tmp_path):
    with Harness(tmp_path / "callbacks.db", market_capacity=1, trade_high_water=2) as h:
        before = h.store.checkpoint()
        outcomes = []

        def callbacks():
            outcomes.extend(h.service.enqueue(event(str(index))) for index in range(5))
            outcomes.append(h.service.enqueue(event("quote", kind=EventKind.MARKET_DATA)))
            outcomes.append(h.service.enqueue(event("discarded-quote", kind=EventKind.MARKET_DATA)))
            with pytest.raises(ExecutionOwnershipError):
                h.service.process_next_command()

        thread = Thread(target=callbacks)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert outcomes == [True] * 6 + [False]
        assert h.store.checkpoint() == before
        assert h.service.metrics["trade_high_water"] == 1
        assert h.service.metrics["market_dropped"] == 1


def test_callback_conversion_error_keeps_sanitized_dead_letter_and_blocks_commands(ready_harness):
    h = ready_harness
    h.service.enqueue_callback_error("fixture-ctp", ValueError("AuthCode=never-persist-this"))
    h.client.submit(command())
    h.service.run_once()
    assert not h.service.ready
    assert h.client.get("one").status == CommandStatus.REJECTED
    history = journal_codec.dumps(tuple(h.journal.replay_from(0)))
    assert "callback_failure" in history and "ValueError" in history
    assert "never-persist-this" not in history
    assert h.gateway.calls == []


def test_serial_command_reservations_use_committed_s2_funds(ready_harness):
    h = ready_harness
    for index in range(3):
        h.client.submit(command(str(index), quantity=4))
    for _ in range(3):
        h.service.process_next_command()
    assert len(h.gateway.calls) == 2
    assert sum(h.model.state["reservations"].values()) == Decimal("800")
    assert h.client.get("2").status == CommandStatus.REJECTED


def test_restart_keeps_processed_command_and_does_not_repeat_it(tmp_path):
    path = tmp_path / "restart.db"
    with Harness(path) as h:
        h.make_ready()
        h.client.submit(command())
        h.service.process_next_command()
    with Harness(path) as restarted:
        assert not restarted.service.ready
        restarted.client.submit(command())
        assert restarted.service.process_next_command() is False
        assert restarted.model.state["reservations"]["one"] == Decimal("100")
        assert restarted.gateway.calls == []


def test_single_local_writer_is_enforced_across_processes(harness):
    code = """
import sys
from qh_trader.infrastructure.journal import SQLiteJournal
from qh_trader.infrastructure.command_queue import SQLiteExecutionStore
from qh_trader.core.execution import ExecutionOwnershipError
with SQLiteJournal(sys.argv[1], account_id=sys.argv[2]) as journal:
    try:
        with SQLiteExecutionStore(journal):
            raise SystemExit(3)
    except ExecutionOwnershipError:
        raise SystemExit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(harness.path), ACCOUNT],
        cwd=ROOT, capture_output=True, text=True, timeout=15, creationflags=PROCESS_FLAGS,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("record_remote_side_effect", [False, True])
def test_hard_process_exit_at_send_boundary_never_resends(tmp_path, record_remote_side_effect):
    path = tmp_path / "crash.db"
    remote_marker = tmp_path / "remote-call.txt"
    with Harness(path) as h:
        h.client.submit(command())
    code = """
import os, runpy, sys
from pathlib import Path
fixture = runpy.run_path(sys.argv[1])
with fixture["Harness"](Path(sys.argv[2])) as h:
    h.make_ready()
    def crash(payload, control):
        if sys.argv[4] == "yes":
            Path(sys.argv[3]).write_text("remote call may have been accepted", encoding="utf-8")
        os._exit(37)
    h.gateway.before_call = crash
    h.service.process_next_command()
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(Path(__file__).resolve()), str(path), str(remote_marker),
         "yes" if record_remote_side_effect else "no"],
        cwd=ROOT, capture_output=True, text=True, timeout=20, creationflags=PROCESS_FLAGS,
    )
    assert result.returncode == 37, result.stderr
    assert remote_marker.exists() == record_remote_side_effect
    with Harness(path) as restarted:
        assert restarted.client.get("one").status == CommandStatus.SENT_UNKNOWN
        assert restarted.model.state["reservations"]["one"] == Decimal("100")
        assert restarted.service.process_next_command() is False
        assert restarted.gateway.calls == []


def test_concurrent_producers_insert_one_identical_command(harness, tmp_path):
    payload = tmp_path / "command.json"
    payload.write_text(journal_codec.dumps(command()), encoding="utf-8")
    code = """
import sys
from pathlib import Path
from qh_trader.infrastructure import journal_codec
from qh_trader.infrastructure.command_queue import SQLiteCommandClient
request = journal_codec.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
with SQLiteCommandClient(sys.argv[1], account_id=request.account_id) as client:
    for _ in range(4):
        client.submit(request)
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(harness.path), str(payload)], cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=PROCESS_FLAGS,
        )
        for _ in range(3)
    ]
    for process in processes:
        _, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr
    assert harness.journal.connection.execute("SELECT COUNT(*) FROM command_queue").fetchone()[0] == 1


def test_busy_insert_is_explicitly_failed_then_retry_is_idempotent(harness):
    h = harness
    # Keep the fault injection fast, after checking the production timeout.
    h.client._connection.set_authorizer(None)
    assert h.client._connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    h.client._connection.execute("PRAGMA busy_timeout=10")
    h.client._connection.set_authorizer(h.client._authorize)
    with sqlite3.connect(h.path, isolation_level=None) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            h.client.submit(command())
        blocker.rollback()
    assert h.client.submission_failures == 1
    assert h.client.get("one") is None
    assert h.client.submit(command()) == h.client.submit(command())


def test_subscribers_read_journal_by_consistent_cursor_without_a_second_event_table(ready_harness):
    h = ready_harness
    cursor, initial = h.client.read_events(0)
    assert initial
    h.client.submit(command())
    h.service.process_next_command()
    next_cursor, events = h.client.read_events(cursor)
    assert next_cursor > cursor
    assert [fact.payload["action"] for fact in events] == ["command_prepared", "local_send_result"]
    assert h.client.read_events(next_cursor) == (next_cursor, ())
    tables = {row[0] for row in h.journal.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "execution_events" not in tables
    with pytest.raises(JournalConflictError, match="beyond"):
        h.client.read_events(next_cursor + 1)


def test_disconnection_of_recovery_coordinator_immediately_blocks_commands(ready_harness):
    h = ready_harness
    h.recovery.on_disconnected("fixture lost broker connection")
    h.client.submit(command())
    h.service.process_next_command()
    assert not h.service.ready
    assert h.client.get("one").status == CommandStatus.REJECTED
    assert h.gateway.calls == []


def test_market_loss_is_audited_and_blocks_new_commands_before_polling(tmp_path, caplog):
    with Harness(tmp_path / "loss.db", market_capacity=1, trade_high_water=1) as h:
        h.make_ready()
        h.service.enqueue(event("priority-fact"))
        h.service.enqueue(event("quote", kind=EventKind.MARKET_DATA))
        assert not h.service.enqueue(event("missing-quote", kind=EventKind.MARKET_DATA))
        h.client.submit(command())
        h.service.process_next_command()
        assert not h.service.ready
        assert h.client.get("one").status == CommandStatus.REJECTED
        assert h.gateway.calls == []
        assert "high-water" in caplog.text
        assert "lost data" in caplog.text
        assert any(
            fact.payload.get("action") == "market_data_loss"
            for fact in h.journal.replay_from(0) if fact.kind == EventKind.CONTROL
        )


def test_reentrant_publication_cannot_start_another_account_command(ready_harness):
    h = ready_harness
    h.client.submit(command("one"))
    h.client.submit(command("two"))
    rejections = []

    def reenter(checkpoint):
        with pytest.raises(ExecutionOwnershipError, match="reentered"):
            h.service.process_next_command()
        rejections.append(checkpoint.journal_seq)

    h.model.after_publish = reenter
    h.service.process_next_command()
    assert rejections
    assert len(h.gateway.calls) == 1
    assert h.client.get("two").status == CommandStatus.PENDING


def test_cancel_return_never_releases_the_original_live_order_reservation(ready_harness):
    h = ready_harness
    h.client.submit(command())
    h.service.process_next_command()
    cancel = command("cancel-command", kind=CommandKind.CANCEL)
    cancel = replace(cancel, payload=replace(cancel.payload, client_order_id="one"))
    h.client.submit(cancel)
    h.gateway.result = LocalSendResult(SendState.NOT_SENT, -1, "cancel was not sent")
    h.service.process_next_command()
    assert h.client.get("cancel-command").status == CommandStatus.NOT_SENT
    assert h.model.state["reservations"]["one"] == Decimal("100")


def test_conflicting_callback_identity_does_not_replace_committed_fact(harness):
    h = harness
    first = event("same-callback")
    second = replace(first, payload={"fixture_fact": "different contents"})
    h.service.enqueue(first)
    h.service.enqueue(second)
    with pytest.raises(JournalConflictError, match="different contents"):
        h.service.run_once()
    assert h.model.state["facts"] == ("same-callback",)
    assert h.store.event("same-callback").payload == first.payload
    assert h.service.pending_fact == second


def test_cross_account_trade_is_rejected_before_persistence(harness):
    h = harness
    original = event("foreign-trade", kind=EventKind.TRADE_REPORT)
    foreign = replace(
        original.payload, account_id="foreign-account",
        deduplication_key=replace(original.payload.deduplication_key, account_id="foreign-account"),
    )
    before = h.store.checkpoint()
    h.service.enqueue(replace(original, payload=foreign))
    with pytest.raises(JournalConflictError, match="cross-account"):
        h.service.run_once()
    assert h.store.checkpoint() == before
    assert h.model.state == before.state


def test_atomic_ack_retry_is_idempotent_and_stale_ack_cannot_add_history(harness):
    h = harness
    queued = h.client.submit(command())
    before = h.store.checkpoint()
    fact = replace(event("ack"), sequence=h.store.next_ingress_sequence())
    tx = JournalTransaction(
        transaction_id="ack-tx", events=(fact,), cursor_before=before.cursor,
        cursor_after=before.cursor + 1,
    )
    first_seq = h.store.commit(tx, expected_control=CONTROL, command=queued, status=CommandStatus.COMPLETED)
    assert h.store.commit(tx, expected_control=CONTROL, command=queued, status=CommandStatus.COMPLETED) == first_seq
    after = h.store.checkpoint()
    stale = replace(
        tx, transaction_id="different-ack",
        events=(replace(fact, event_id="second-ack", sequence=h.store.next_ingress_sequence()),),
        cursor_before=after.cursor, cursor_after=after.cursor + 1,
    )
    with pytest.raises(JournalConflictError, match="already been processed"):
        h.store.commit(stale, expected_control=CONTROL, command=queued, status=CommandStatus.COMPLETED)
    assert h.store.checkpoint() == after
    assert h.store.event("second-ack") is None


def test_takeover_commit_failure_does_not_leave_a_half_advanced_epoch(harness):
    h = harness
    queued = h.client.submit(command("takeover", kind=CommandKind.TAKEOVER_REQUEST))
    before = h.store.checkpoint()
    h.journal.connection.execute("""
        CREATE TRIGGER fail_takeover BEFORE UPDATE ON command_queue
        BEGIN SELECT RAISE(ABORT, 'injected takeover write failure'); END
    """)
    with pytest.raises(sqlite3.DatabaseError, match="injected"):
        h.service.take_over("takeover", Isolation(h))
    assert h.client.get("takeover") == queued
    assert h.store.checkpoint() == before
    assert h.store.control().epoch == CONTROL
    assert not h.service.ready


def test_monotonic_budget_limits_trade_batch_even_if_wall_clock_moves_backwards(tmp_path):
    with Harness(tmp_path / "clock.db", trade_batch_size=32) as h:
        h.make_ready()
        h.client.submit(command("cancel", kind=CommandKind.CANCEL))
        for index in range(5):
            h.service.enqueue(event("clock-fact-" + str(index)))
        # deadline=100.1, get budget=0.05, first processing finishes at 100.2.
        times = iter((100.0, 100.05, 100.2))
        h.service._monotonic = lambda: next(times)
        h.service._wall_time = lambda: datetime(2024, 9, 9, 1, tzinfo=timezone.utc)
        h.service.run_once(wait=True)
        assert h.model.state["facts"] == ("clock-fact-0",)
        assert h.client.get("cancel").status == CommandStatus.SENT_UNKNOWN
        assert h.service.metrics["trade_queue_depth"] == 4


def test_retry_republishes_committed_fact_after_publication_failed(ready_harness):
    h = ready_harness
    trade = event("committed-not-published", kind=EventKind.TRADE_REPORT)
    previous_state = h.model.state
    original_publish = h.model.publish

    def fail_publication(checkpoint):
        raise RuntimeError("injected publication failure")

    h.model.publish = fail_publication
    h.service.enqueue(trade)
    with pytest.raises(RuntimeError, match="publication"):
        h.service.run_once()
    assert h.journal.contains_trade(trade.payload.deduplication_key)
    assert h.model.state == previous_state
    assert h.service.pending_fact == trade
    h.model.publish = original_publish
    h.service.retry_pending_fact()
    assert h.model.state == h.store.checkpoint().state
    assert len(h.model.state["trades"]) == 1
    assert not h.service.ready


@pytest.mark.parametrize("failure", ["market_drop", "callback_error"])
def test_new_callback_failure_between_prepare_and_send_closes_final_gate(ready_harness, failure):
    h = ready_harness
    h.client.submit(command())

    def callback_before_send(checkpoint):
        h.model.after_publish = None
        if failure == "market_drop":
            for index in range(1025):
                h.service.enqueue(event("quote-" + str(index), kind=EventKind.MARKET_DATA))
        else:
            h.service.enqueue_callback_error("fixture-ctp", ValueError("invalid broker callback"))

    h.model.after_publish = callback_before_send
    h.service.process_next_command()
    assert h.gateway.calls == []
    assert h.client.get("one").status == CommandStatus.NOT_SENT
    assert h.model.state["reservations"] == {}
    assert not h.service.ready
