"""S5 recovery query facts commit through the sole executor before a fresh readiness check."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from qh_trader.core.constants import EventKind, Exchange, Offset, OrderStatus, OrderType, PositionSide, Side
from qh_trader.core.event import CanonicalEvent
from qh_trader.core.execution import CommandKind, CommandStatus, ExecutionCommand, ExecutionNotReadyError
from qh_trader.core.objects import (
    AccountFunds,
    InstrumentId,
    OrderIdentity,
    OrderIntent,
    OrderUpdate,
    Position,
    QueryResult,
    Trade,
    TradeKey,
)
from qh_trader.engine.live_account_model import account_facts
from scripts.live_assembly import ExecutionSpec, assemble

ROOT = Path(__file__).resolve().parents[2]
DAY = date(2024, 9, 10)
NOW = datetime(2024, 9, 10, 3, tzinfo=timezone.utc)
RB = InstrumentId(Exchange.SHFE, "rb2410")
ACCOUNT = "query-recovery-test"


class BrokerSnapshot:
    """Independent deterministic query results; no counter binding or network."""

    def __init__(self, assembled, trades=(), orders=None):
        self.trades = tuple(trades)
        self.orders = tuple(orders) if orders is not None else tuple(
            OrderUpdate(
                identity=order.identity, instrument=order.instrument, side=order.side, offset=order.offset,
                status=OrderStatus.PARTIALLY_FILLED if self.trades else OrderStatus.ACCEPTED,
                quantity=order.quantity,
                filled_quantity=sum(t.quantity for t in self.trades if t.order_identity == order.identity),
                event_time=NOW, available_at=NOW,
            )
            for order in assembled.model.orders.orders()
        )
        self.balance = (Decimal("100000")
                        - sum(t.quantity for t in self.trades) * assembled.economics[RB].commission_per_lot)
        self.rounds = 0
        self.complete = True
        self.on_positions = None

    def result(self, batch, records):
        return QueryResult(batch=batch, records=tuple(records), available_at=NOW,
                           source_id="broker-fixture", source_version="v1", complete=self.complete)

    def query_orders(self, batch):
        self.rounds += 1
        return self.result(batch, self.orders)

    def query_trades(self, batch):
        return self.result(batch, self.trades)

    def query_positions(self, batch):
        if self.on_positions is not None:
            self.on_positions()
        quantity = sum(trade.quantity for trade in self.trades)
        records = (Position(instrument=RB, side=PositionSide.LONG, hedge_flag="SPECULATION",
                            pos_yd=0, pos_td=quantity, frozen_yd=0, frozen_td=0),) if quantity else ()
        return self.result(batch, records)

    def query_account(self, batch):
        return self.result(batch, (AccountFunds(self.balance, self.balance, None, self.balance),))


def start(tmp_path):
    spec = ExecutionSpec(
        mode="paper", account_id=ACCOUNT, journal_path=tmp_path / "journal.db",
        catalog_path=ROOT / "config/contract_catalog_s4_2024v1.json", symbols=(str(RB),),
        initial_capital=Decimal("100000"), trading_day=DAY,
        controller_id="query-executor", heartbeat_path=tmp_path / "heartbeat.json",
    )
    assembled = assemble(spec)
    request = assembled.request_control("fixture start")
    assembled.take_over(request.command_id, assembled.isolation(operator_confirmed=True))
    assembled.reconcile_and_enable()
    return assembled


def submit(assembled, cid="o1", quantity=2, *, process=True):
    intent = OrderIntent(client_order_id=cid, account_id=ACCOUNT, strategy_id="fixture", instrument=RB,
                         side=Side.BUY, offset=Offset.OPEN, quantity=quantity, order_type=OrderType.LIMIT,
                         created_at=NOW, limit_price_ticks=3500)
    command = ExecutionCommand(command_id=f"cmd-{cid}", account_id=ACCOUNT, producer_id="fixture",
                               control=assembled.store.control().epoch, kind=CommandKind.SUBMIT,
                               submitted_at=NOW, payload=intent)
    assembled.client.submit(command)
    if process:
        assembled.service.process_next_command()
        assembled.pump_gateway()
        assembled.service.run_once()
    return command


def fill(assembled, trade_id="t1", *, cid="o1", quantity=1):
    return Trade(account_id=ACCOUNT, instrument=RB, trading_day=DAY, trade_id=trade_id,
                 side=Side.BUY, offset=Offset.OPEN, quantity=quantity, price=Decimal("3500"),
                 event_time=NOW, available_at=NOW,
                 deduplication_key=TradeKey(ACCOUNT, Exchange.SHFE, DAY, trade_id),
                 order_identity=assembled.model.orders.get_order(cid).identity)


def trade_event(trade):
    return CanonicalEvent(event_id=f"callback:{trade.trade_id}", kind=EventKind.TRADE_REPORT,
                          event_time=trade.event_time, available_at=trade.available_at, sequence=0,
                          source_id="counter-fixture", payload=trade)


def booked(assembled):
    return [fact for fact in account_facts(assembled.store.checkpoint().state) if fact["kind"] == "trade"]


def test_restart_books_missing_partial_fill_once_and_retains_pending_commands(tmp_path):
    first = start(tmp_path)
    try:
        submit(first)
        trade = fill(first)
        snapshot = BrokerSnapshot(first, (trade,))
        spec = first.spec
    finally:
        first.close()
    restored = assemble(spec)
    try:
        restored.query = snapshot
        pending = submit(restored, "waiting", process=False)
        restored.reconcile_and_enable()
        assert restored.service.ready
        assert snapshot.rounds == 2
        assert len(booked(restored)) == 1
        order = restored.model.orders.get_order("o1")
        assert order.accounted_filled_qty == 1 and order.leaves_qty == 1
        assert restored.model.positions.get_position(RB, PositionSide.LONG).pos_td == 1
        assert restored.model.ledger.balance == snapshot.balance
        assert restored.store.get(pending.command_id).status == CommandStatus.PENDING
        assert restored.model.ledger.get_funds_reservation("o1") is not None
        restored.reconcile_and_enable()
        assert len(booked(restored)) == 1
        assert restored.service.ready
    finally:
        restored.close()
    restarted = assemble(spec)
    try:
        assert restarted.model.orders.get_order("o1").accounted_filled_qty == 1
        assert restarted.model.ledger.balance == snapshot.balance
        assert restarted.store.contains_trade(trade.deduplication_key)
    finally:
        restarted.close()


def test_query_order_binds_new_exchange_identity_before_missing_trade(tmp_path):
    assembled = start(tmp_path)
    try:
        submit(assembled, process=False)
        assembled.service.process_next_command()
        for event in assembled.gateway.drain_events():
            # Simulate an early acknowledgement carrying only the original session identity.
            update = replace(event.payload, identity=replace(event.payload.identity, exchange_order_id=None))
            assembled.service.enqueue(replace(event, payload=update))
        assembled.service.run_once()
        trade = fill(assembled)
        local = trade.order_identity
        assert local.exchange_order_id is None
        # The query supplies a new exchange ID through the already registered original session.
        remote = replace(local, exchange_order_id="new-exchange-id")
        original = BrokerSnapshot(assembled).orders[0]
        queried_order = replace(original, identity=remote, status=OrderStatus.FILLED, filled_quantity=2)
        queried_trade = replace(trade, quantity=2, order_identity=OrderIdentity(
            account_id=ACCOUNT, exchange=Exchange.SHFE, exchange_order_id="new-exchange-id"))
        snapshot = BrokerSnapshot(assembled, (queried_trade,), (queried_order,))
        assembled.query = snapshot
        assembled.reconcile_and_enable()
        assert assembled.service.ready and snapshot.rounds == 2
        assert assembled.model.orders.get_order("o1").status == OrderStatus.FILLED
        assert assembled.model.orders.get_order("o1").identity.exchange_order_id == "new-exchange-id"
        assert assembled.model.positions.get_position(RB, PositionSide.LONG).pos_td == 2
        assert assembled.model.ledger.get_funds_reservation("o1") is None
    finally:
        assembled.close()


@pytest.mark.parametrize("case", ["unknown", "ambiguous", "wrong_terms", "overfill", "incomplete", "wrong_day"])
def test_untrusted_query_trade_never_books_or_enables(tmp_path, case):
    assembled = start(tmp_path)
    try:
        submit(assembled)
        trade = fill(assembled)
        snapshot = BrokerSnapshot(assembled, (trade,))
        if case == "unknown":
            trade = replace(trade, order_identity=OrderIdentity(
                account_id=ACCOUNT, exchange=Exchange.SHFE, exchange_order_id="external"))
        elif case == "ambiguous":
            submit(assembled, "o2")
            second = assembled.model.orders.get_order("o2").identity
            trade = replace(trade, order_identity=replace(second, client_order_id="o1"))
        elif case == "wrong_terms":
            trade = replace(trade, side=Side.SELL)
        elif case == "overfill":
            trade = replace(trade, quantity=3)
        elif case == "wrong_day":
            other = date(2024, 9, 11)
            trade = replace(trade, trading_day=other,
                            deduplication_key=TradeKey(ACCOUNT, Exchange.SHFE, other, trade.trade_id))
        else:
            snapshot.complete = False
        snapshot.trades = (trade,)
        assembled.query = snapshot
        with pytest.raises(ExecutionNotReadyError, match="blocked"):
            assembled.reconcile_and_enable()
        assert not assembled.service.ready and not booked(assembled)
        assert assembled.recovery.report.blocking
        assert assembled.model.positions.get_position(RB, PositionSide.LONG).pos_td == 0
    finally:
        assembled.close()


def test_query_commit_failure_retains_fact_and_keeps_gate_closed(tmp_path, monkeypatch):
    assembled = start(tmp_path)
    try:
        submit(assembled)
        snapshot = BrokerSnapshot(assembled, (fill(assembled),))
        assembled.query = snapshot
        commit = assembled.store.commit

        def fail_trade(transaction, **kwargs):
            if transaction.events[0].kind == EventKind.TRADE_REPORT:
                raise OSError("injected journal failure")
            return commit(transaction, **kwargs)

        monkeypatch.setattr(assembled.store, "commit", fail_trade)
        with pytest.raises(OSError, match="injected"):
            assembled.reconcile_and_enable()
        assert not assembled.service.ready and not booked(assembled)
        assert assembled.service.pending_fact.kind == EventKind.TRADE_REPORT
        assert assembled.model.positions.get_position(RB, PositionSide.LONG).pos_td == 0
        monkeypatch.setattr(assembled.store, "commit", commit)
        assembled.service.retry_pending_fact()
        assert not assembled.service.ready
        assembled.reconcile_and_enable()
        assert assembled.service.ready and len(booked(assembled)) == 1
    finally:
        assembled.close()


def test_callback_arriving_during_query_is_committed_and_requeried(tmp_path):
    assembled = start(tmp_path)
    try:
        submit(assembled)
        trade = fill(assembled)
        snapshot = BrokerSnapshot(assembled, (trade,))

        def deliver_once():
            snapshot.on_positions = None
            assembled.service.enqueue(trade_event(trade))

        snapshot.on_positions = deliver_once
        assembled.query = snapshot
        assembled.reconcile_and_enable()
        assert assembled.service.ready
        assert len(booked(assembled)) == 1 and snapshot.rounds >= 2
    finally:
        assembled.close()


def test_duplicate_trade_key_with_changed_price_is_blocking(tmp_path):
    assembled = start(tmp_path)
    try:
        submit(assembled)
        trade = fill(assembled)
        assembled.service.enqueue(trade_event(trade))
        assembled.service.run_once()
        snapshot = BrokerSnapshot(assembled, (replace(trade, price=Decimal("3501")),))
        assembled.query = snapshot
        with pytest.raises(ExecutionNotReadyError, match="conflicting financial terms"):
            assembled.reconcile_and_enable()
        assert not assembled.service.ready and len(booked(assembled)) == 1
        assert assembled.model.positions.get_position(RB, PositionSide.LONG).pos_td == 1
    finally:
        assembled.close()


def test_missing_trade_details_for_reported_cumulative_fill_keep_gate_closed(tmp_path):
    assembled = start(tmp_path)
    try:
        submit(assembled)
        snapshot = BrokerSnapshot(assembled)
        snapshot.orders = (replace(snapshot.orders[0], filled_quantity=1, status=OrderStatus.PARTIALLY_FILLED),)
        assembled.query = snapshot
        with pytest.raises(ExecutionNotReadyError, match="lack corresponding trade details"):
            assembled.reconcile_and_enable()
        assert not assembled.service.ready and not booked(assembled)
    finally:
        assembled.close()


def test_continuous_callback_activity_has_bounded_recovery_and_cannot_enable(tmp_path):
    assembled = start(tmp_path)
    try:
        submit(assembled)
        trade = fill(assembled)
        snapshot = BrokerSnapshot(assembled, (trade,))
        snapshot.on_positions = lambda: assembled.service.enqueue(trade_event(trade))
        assembled.query = snapshot
        with pytest.raises(ExecutionNotReadyError, match="did not stabilize"):
            assembled.reconcile_and_enable()
        assert not assembled.service.ready and snapshot.rounds == 3
        assert len(booked(assembled)) == 1
    finally:
        assembled.close()


def test_funds_discrepancy_after_booking_still_blocks_readiness(tmp_path):
    assembled = start(tmp_path)
    try:
        submit(assembled)
        snapshot = BrokerSnapshot(assembled, (fill(assembled),))
        snapshot.balance -= Decimal("1")
        assembled.query = snapshot
        with pytest.raises(ExecutionNotReadyError, match="reconciliation is required"):
            assembled.reconcile_and_enable()
        assert not assembled.service.ready and len(booked(assembled)) == 1
        assert any(diff.category == "funds" for diff in assembled.recovery.report.blocking)
    finally:
        assembled.close()


def test_full_trade_details_resolve_order_missing_from_active_only_query(tmp_path):
    assembled = start(tmp_path)
    try:
        submit(assembled)
        snapshot = BrokerSnapshot(assembled, (fill(assembled, quantity=2),), orders=())
        assembled.query = snapshot
        assembled.reconcile_and_enable()
        assert assembled.service.ready and len(booked(assembled)) == 1
        assert assembled.model.orders.get_order("o1").status == OrderStatus.FILLED
        assert assembled.model.ledger.get_funds_reservation("o1") is None
    finally:
        assembled.close()


def test_duplicate_trade_cannot_change_its_registered_order_attribution(tmp_path):
    assembled = start(tmp_path)
    try:
        submit(assembled)
        submit(assembled, "o2")
        trade = fill(assembled)
        assembled.service.enqueue(trade_event(trade))
        assembled.service.run_once()
        changed = replace(trade, order_identity=assembled.model.orders.get_order("o2").identity)
        snapshot = BrokerSnapshot(assembled, (changed,))
        assembled.query = snapshot
        with pytest.raises(ExecutionNotReadyError, match="registered order attribution"):
            assembled.reconcile_and_enable()
        assert not assembled.service.ready and len(booked(assembled)) == 1
        assert assembled.model.orders.get_order("o2").accounted_filled_qty == 0
    finally:
        assembled.close()


def test_fresh_assembly_publishes_explicit_flat_account_evidence(tmp_path):
    assembled = start(tmp_path)
    try:
        _, view, pending = assembled.client.strategy_bootstrap()
        assert view["positions"] == ()
        assert view["active_orders"] == 0
        assert view["balance"] == Decimal("100000")
        assert pending == ()
    finally:
        assembled.close()


def test_missing_legacy_account_view_is_rebuilt_from_existing_fills(tmp_path):
    from scripts.live_assembly import _commit_account_state

    assembled = start(tmp_path)
    try:
        submit(assembled)
        assembled.service.enqueue(trade_event(fill(assembled)))
        assembled.service.run_once()
        expected_balance = assembled.model.ledger.balance
        _commit_account_state(assembled.store, "fixture_legacy_view", {}, {"account_view": None})
        spec = assembled.spec
    finally:
        assembled.close()
    restored = assemble(spec)
    try:
        _, view, _ = restored.client.strategy_bootstrap()
        assert view["positions"][0].pos_td == 1
        assert view["active_orders"] == 1
        assert view["balance"] == expected_balance
        assert not restored.service.ready
    finally:
        restored.close()
