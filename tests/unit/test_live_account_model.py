"""S5-04 实盘账户模型：S2 内核暂存 / 发布、命令与事实投影、重启重建、结算推进、代次围栏、心跳文件."""

from __future__ import annotations

import threading
from contextlib import ExitStack
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from qh_trader.core.constants import (
    EventKind,
    Exchange,
    MissingRuleError,
    Offset,
    OrderStatus,
    OrderType,
    PositionSide,
    QualityFlag,
    SendState,
    Side,
)
from qh_trader.core.event import CanonicalEvent, JournalTransaction
from qh_trader.core.execution import (
    CommandKind,
    CommandStatus,
    ExecutionCommand,
    ExecutionNotReadyError,
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
    OrderUpdate,
    Position,
    QueryBatch,
    QueryResult,
    RecordMeta,
    Settlement,
    Trade,
    TradeKey,
)
from qh_trader.core.ports import ExecutionModelPort, ExecutionPort
from qh_trader.domain.recovery import RecoveryCoordinator
from qh_trader.domain.risk import RiskState
from qh_trader.engine.base_engine import InstrumentEconomics
from qh_trader.engine.execution_service import ExecutionService
from qh_trader.engine.live_account_model import (
    ADVANCE_TRADING_DAY,
    FACTS_KEY,
    AccountModelCorruptionError,
    AccountOpening,
    LiveAccountModel,
)
from qh_trader.gateway.epoch_fence import EpochFencedGateway
from qh_trader.gateway.simulated_gateway import SimulatedGateway
from qh_trader.infrastructure.command_queue import SQLiteCommandClient, SQLiteExecutionStore
from qh_trader.infrastructure.journal import SQLiteJournal
from qh_trader.monitor.heartbeat import HeartbeatFile, read_heartbeat

NOW = datetime(2024, 9, 10, 1, tzinfo=timezone.utc)
D1 = date(2024, 9, 10)
D2 = date(2024, 9, 11)
ACCOUNT = "live-model-account"
CONTROL = ControlEpoch("strategy-controller", 1)
RB = InstrumentId(Exchange.SHFE, "rb2410")
ECONOMICS = {RB: InstrumentEconomics(Decimal("10"), Decimal("1"), Decimal("0"), Decimal("0.1"), "fixture")}
OPENING = AccountOpening(Decimal("1000"), D1)


def intent(identifier: str, *, side=Side.BUY, offset=Offset.OPEN, quantity=1, price=100) -> OrderIntent:
    return OrderIntent(
        client_order_id=identifier,
        account_id=ACCOUNT,
        strategy_id="fixture",
        instrument=RB,
        side=side,
        offset=offset,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        created_at=NOW,
        limit_price_ticks=price,
    )


def command(identifier: str, payload, *, kind=CommandKind.SUBMIT, control=CONTROL) -> ExecutionCommand:
    return ExecutionCommand(
        command_id=identifier,
        account_id=ACCOUNT,
        producer_id="fixture-producer",
        control=control,
        kind=kind,
        submitted_at=NOW,
        payload=payload,
    )


def trade_event(trade_id: str, *, day=D1, side=Side.BUY, offset=Offset.OPEN, price="100", quantity=1, order="o1"):
    trade = Trade(
        account_id=ACCOUNT,
        instrument=RB,
        trading_day=day,
        trade_id=trade_id,
        side=side,
        offset=offset,
        quantity=quantity,
        price=Decimal(price),
        event_time=NOW,
        available_at=NOW,
        deduplication_key=TradeKey(ACCOUNT, Exchange.SHFE, day, trade_id),
        order_identity=OrderIdentity(account_id=ACCOUNT, exchange=Exchange.SHFE, client_order_id=order),
    )
    return CanonicalEvent(
        event_id="trade:" + trade_id,
        kind=EventKind.TRADE_REPORT,
        event_time=NOW,
        available_at=NOW,
        sequence=0,
        source_id="fixture-broker",
        payload=trade,
    )


def order_report(order: str, status: OrderStatus, *, filled=0, quantity=1, side=Side.BUY, offset=Offset.OPEN):
    update = OrderUpdate(
        identity=OrderIdentity(
            account_id=ACCOUNT, exchange=Exchange.SHFE, client_order_id=order, exchange_order_id="EX-" + order
        ),
        instrument=RB,
        side=side,
        offset=offset,
        status=status,
        quantity=quantity,
        filled_quantity=filled,
        event_time=NOW,
        available_at=NOW,
    )
    return CanonicalEvent(
        event_id=f"report:{order}:{status.value}:{filled}",
        kind=EventKind.ORDER_REPORT,
        event_time=NOW,
        available_at=NOW,
        sequence=0,
        source_id="fixture-broker",
        payload=update,
    )


def settlement_event(day: date, price: str):
    meta = RecordMeta(
        event_time=NOW,
        available_at=NOW,
        ingested_at=NOW,
        trading_day=day,
        source_id="fixture-settlement",
        source_version="1",
        ingest_seq=1,
        quality_flags=QualityFlag.OK,
    )
    payload = Settlement(
        instrument=RB,
        meta=meta,
        settlement_price=Decimal(price),
        pre_settlement_price=None,
        published_at=NOW,
        is_final=True,
    )
    return CanonicalEvent(
        event_id=f"settle:{day}:{price}",
        kind=EventKind.SETTLEMENT,
        event_time=NOW,
        available_at=NOW,
        sequence=0,
        source_id="fixture-settlement",
        payload=payload,
    )


def advance_event(day: date, new_day: date):
    return CanonicalEvent(
        event_id=f"advance:{day}:{new_day}",
        kind=EventKind.CONTROL,
        event_time=NOW,
        available_at=NOW,
        sequence=0,
        source_id="lifecycle",
        payload={"action": ADVANCE_TRADING_DAY, "trading_day": day, "new_trading_day": new_day, "version": "v1"},
    )


class RecordingGateway:
    def __init__(self) -> None:
        self.calls: list = []
        self.result = LocalSendResult(SendState.SENT_UNKNOWN, 0, "accepted locally; remote result unknown")

    def submit(self, order, control):
        self.calls.append(("submit", order, control))
        return self.result

    def cancel(self, identity, control):
        self.calls.append(("cancel", identity, control))
        return self.result

    def capabilities(self):
        raise AssertionError("not needed")


class Harness:
    def __init__(self, path: Path, *, model: LiveAccountModel | None = None, gateway=None) -> None:
        self.path = path
        self.stack = ExitStack()
        self.journal = self.stack.enter_context(SQLiteJournal(path, account_id=ACCOUNT))
        self.journal.migrate()
        if self.journal.head_seq == 0:
            seed = CanonicalEvent(
                event_id="initial-control",
                kind=EventKind.CONTROL,
                event_time=NOW,
                available_at=NOW,
                sequence=1,
                source_id="fixture",
                payload={"fixture": "seed"},
            )
            self.journal.append(
                JournalTransaction(
                    transaction_id="initial-control",
                    events=(seed,),
                    cursor_before=0,
                    cursor_after=1,
                    control_record=ControlRecord(CONTROL, NOW, 1),
                )
            )
        self.store = self.stack.enter_context(SQLiteExecutionStore(self.journal))
        self.store.migrate()
        self.client = self.stack.enter_context(SQLiteCommandClient(path, account_id=ACCOUNT))
        self.model = model or LiveAccountModel(ACCOUNT, OPENING, ECONOMICS, now=lambda: NOW)
        self.gateway = gateway or RecordingGateway()
        self.recovery = RecoveryCoordinator(self.model.replica().orders, self.model.replica().positions)
        self.service = ExecutionService(
            store=self.store, model=self.model, gateway=self.gateway, recovery=self.recovery, wall_time=lambda: NOW
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.stack.close()

    def query(self, kind, records=(), **changes):
        return QueryResult(
            batch=QueryBatch(kind, ACCOUNT, D1, NOW),
            records=records,
            available_at=NOW,
            source_id="fixture-broker",
            source_version="1",
            **({"complete": True} | changes),
        )

    def make_ready(self, *, balance=Decimal("1000"), positions=()):
        self.recovery.start_recovery(expected_trading_day=D1)
        self.recovery.begin_reconciliation()
        self.recovery.merge_order_query(self.query("orders"))
        self.recovery.merge_trade_query(self.query("trades"))
        self.recovery.reconcile_positions(self.query("positions", positions))
        funds = AccountFunds(balance, balance, Decimal("0"), balance)
        self.recovery.reconcile_funds(self.query("funds", (funds,)), self.model.ledger.balance)
        self.service.enable_after_reconciliation()

    def submit(self, cmd: ExecutionCommand):
        self.client.submit(cmd)
        self.service.process_next_command()
        return self.client.get(cmd.command_id)

    def fact(self, event: CanonicalEvent) -> None:
        self.service.enqueue(event)
        self.service.run_once()


@pytest.fixture
def harness(tmp_path):
    with Harness(tmp_path / "trading.db") as instance:
        yield instance


@pytest.fixture
def ready(harness):
    harness.make_ready()
    return harness


# ---------------------------------------------------------------------- 契约与发布


def test_model_satisfies_port_and_publishes_opening_from_journal(harness):
    assert isinstance(harness.model, ExecutionModelPort)
    assert harness.model.kernel_ready
    assert harness.model.ledger.balance == Decimal("1000")
    assert harness.model.trading_day == D1
    assert harness.model.fact_count == 0


def test_submit_reserves_through_s2_risk_and_only_publishes_after_commit(ready):
    queued = ready.submit(command("c1", intent("o1")))
    assert queued.status == CommandStatus.SENT_UNKNOWN
    order = ready.model.orders.get_order("o1")
    assert order is not None and order.send_state == SendState.SENT_UNKNOWN
    reservation = ready.model.ledger.get_funds_reservation("o1")
    assert reservation is not None and reservation.margin == Decimal("100")  # 100 × 10 × 1 × 0.1
    assert ready.model.positions.get_reservation("o1") is not None
    assert ready.journal.load_state()[FACTS_KEY][0]["kind"] == "opened"
    assert [fact["kind"] for fact in ready.journal.load_state()[FACTS_KEY][1:]] == ["intent", "send_result"]


def test_insufficient_funds_is_rejected_without_staging_any_fact(ready):
    # 1000 元本金；每手预占 100 元保证金：第 11 手被拒
    for index in range(10):
        assert ready.submit(command(f"c{index}", intent(f"o{index}"))).status == CommandStatus.SENT_UNKNOWN
    rejected = ready.submit(command("c-too-many", intent("o-too-many")))
    assert rejected.status == CommandStatus.REJECTED
    assert ready.model.orders.get_order("o-too-many") is None
    facts = ready.journal.load_state()[FACTS_KEY]
    assert sum(1 for fact in facts if fact["kind"] == "intent") == 10


def test_not_sent_releases_reservation_but_unknown_keeps_it(ready):
    ready.gateway.result = LocalSendResult(SendState.NOT_SENT, -1, "rejected locally by gateway")
    assert ready.submit(command("c1", intent("o1"))).status == CommandStatus.NOT_SENT
    assert ready.model.ledger.get_funds_reservation("o1") is None
    assert ready.model.positions.get_reservation("o1") is None
    assert ready.model.orders.get_order("o1").status == OrderStatus.REJECTED
    ready.gateway.result = LocalSendResult(SendState.SENT_UNKNOWN, 0, "unknown")
    assert ready.submit(command("c2", intent("o2"))).status == CommandStatus.SENT_UNKNOWN
    assert ready.model.ledger.get_funds_reservation("o2") is not None


def test_trade_and_terminal_report_book_ledger_and_release_reservation(ready):
    ready.submit(command("c1", intent("o1")))
    ready.fact(order_report("o1", OrderStatus.ACCEPTED))
    ready.fact(trade_event("t1"))
    ready.fact(order_report("o1", OrderStatus.FILLED, filled=1))
    position = ready.model.positions.get_position(RB, PositionSide.LONG)
    assert position.pos_td == 1 and position.total_frozen == 0
    assert ready.model.ledger.get_funds_reservation("o1") is None
    assert ready.model.orders.get_order("o1").status == OrderStatus.FILLED
    assert ready.model.risk.today_open_lots(RB, D1) == 1


def test_duplicate_trade_is_not_booked_twice(ready):
    ready.submit(command("c1", intent("o1")))
    ready.fact(trade_event("t1"))
    ready.fact(replace(trade_event("t1"), event_id="trade:t1:again"))
    assert ready.model.positions.get_position(RB, PositionSide.LONG).pos_td == 1
    assert ready.service.metrics["duplicate_facts"] == 1


def test_cross_day_settlement_matches_the_a22_manual_ledger(ready):
    """100 开仓、结算 110 (+100)、次日 108 平仓 (盯市 −20 / 逐笔 +80)：权益只增加 80 (FR-LED-03)."""
    ready.submit(command("c1", intent("o1")))
    ready.fact(trade_event("t1"))
    ready.fact(order_report("o1", OrderStatus.FILLED, filled=1))
    ready.fact(settlement_event(D1, "110"))
    ready.fact(advance_event(D1, D2))
    assert ready.model.trading_day == D2
    assert ready.model.ledger.balance == Decimal("1100")
    assert ready.model.positions.get_position(RB, PositionSide.LONG).pos_yd == 1
    ready.submit(command("c2", intent("o2", side=Side.SELL, offset=Offset.CLOSE_YESTERDAY, price=108)))
    ready.fact(trade_event("t2", day=D2, side=Side.SELL, offset=Offset.CLOSE_YESTERDAY, price="108", order="o2"))
    ready.fact(order_report("o2", OrderStatus.FILLED, filled=1, side=Side.SELL, offset=Offset.CLOSE_YESTERDAY))
    assert ready.model.ledger.balance == Decimal("1080")
    assert ready.model.ledger.realized_trade_pnl == Decimal("80")
    assert ready.model.positions.get_position(RB, PositionSide.LONG).total_position == 0


def test_advance_without_settlement_price_blocks_new_risk_until_price_arrives(ready):
    ready.submit(command("c1", intent("o1")))
    ready.fact(trade_event("t1"))
    ready.fact(advance_event(D1, D2))
    assert ready.model.settlement_pending and ready.model.trading_day == D1
    assert ready.submit(command("c2", intent("o2"))).status == CommandStatus.REJECTED
    ready.fact(settlement_event(D1, "110"))
    assert not ready.model.settlement_pending and ready.model.trading_day == D2
    assert ready.submit(command("c3", intent("o3"))).status == CommandStatus.SENT_UNKNOWN


def test_pause_blocks_opens_allows_cancel_and_resume_needs_explicit_conditions(ready):
    ready.submit(command("c1", intent("o1")))
    assert ready.submit(command("p", {"reason": "operator pause"}, kind=CommandKind.PAUSE)).status == (
        CommandStatus.COMPLETED
    )
    assert ready.model.risk.risk_state == RiskState.HALTED
    assert ready.submit(command("c2", intent("o2"))).status == CommandStatus.REJECTED
    cancel = command(
        "x1", OrderIdentity(account_id=ACCOUNT, exchange=Exchange.SHFE, client_order_id="o1"), kind=CommandKind.CANCEL
    )
    assert ready.submit(cancel).status == CommandStatus.SENT_UNKNOWN
    assert ready.model.orders.get_order("o1").cancel_pending
    bad = command("r0", {"reason": "resume"}, kind=CommandKind.RESUME)
    assert ready.submit(bad).status == CommandStatus.REJECTED
    good = command(
        "r1", {"reason": "cause cleared", "cause_cleared": True, "account_consistent": True}, kind=CommandKind.RESUME
    )
    assert ready.submit(good).status == CommandStatus.COMPLETED
    assert ready.model.risk.risk_state == RiskState.NORMAL


def test_unknown_command_kinds_and_unregistered_instruments_are_explicit(ready):
    amend = command("a1", {"reason": "amend"}, kind=CommandKind.AMEND)
    assert ready.submit(amend).status == CommandStatus.REJECTED
    other = InstrumentId(Exchange.DCE, "m2501")
    foreign = replace(
        trade_event("t9").payload,
        instrument=other,
        deduplication_key=TradeKey(ACCOUNT, Exchange.DCE, D1, "t9"),
        order_identity=None,
    )
    event = replace(trade_event("t9"), payload=foreign)
    ready.service.enqueue(event)
    with pytest.raises(MissingRuleError):
        ready.service.run_once()
    assert ready.service.pending_fact is not None and not ready.service.ready


def test_restart_rebuilds_the_same_kernel_from_journal_facts(tmp_path):
    path = tmp_path / "trading.db"
    with Harness(path) as first:
        first.make_ready()
        first.submit(command("c1", intent("o1")))
        first.fact(trade_event("t1"))
        first.fact(order_report("o1", OrderStatus.FILLED, filled=1))
        first.submit(command("c2", intent("o2")))
        expected_balance = first.model.ledger.balance
        expected_facts = first.model.fact_count
    other_opening = AccountOpening(Decimal("5"), date(2020, 1, 1))
    with Harness(path, model=LiveAccountModel(ACCOUNT, other_opening, ECONOMICS, now=lambda: NOW)) as second:
        assert second.model.fact_count == expected_facts
        assert second.model.ledger.balance == expected_balance
        assert second.model.ledger.initial_capital == Decimal("1000")  # 持久化开立事实优先于构造参数
        assert second.model.positions.get_position(RB, PositionSide.LONG).pos_td == 1
        pending = second.model.orders.get_order("o2")
        assert pending is not None and pending.send_state == SendState.SENT_UNKNOWN
        assert second.model.ledger.get_funds_reservation("o2") is not None


def test_diverged_published_prefix_poisons_the_kernel_instead_of_guessing(ready):
    ready.submit(command("c1", intent("o1")))
    checkpoint = ready.store.checkpoint()
    facts = list(checkpoint.state[FACTS_KEY])
    facts[1] = dict(facts[1]) | {"margin": Decimal("999")}
    forged = replace(checkpoint, state=dict(checkpoint.state) | {FACTS_KEY: tuple(facts)})
    with pytest.raises(AccountModelCorruptionError):
        ready.model.publish(forged)
    with pytest.raises(AccountModelCorruptionError):
        _ = ready.model.ledger


def test_staging_never_mutates_the_published_kernel(ready):
    before = ready.model.ledger.balance
    plan = ready.model.stage_command(command("c1", intent("o1")))
    assert plan.approved
    assert ready.model.orders.get_order("o1") is None
    assert ready.model.ledger.get_funds_reservation("o1") is None
    assert ready.model.ledger.balance == before


def test_market_data_updates_mark_prices_only(ready):
    from qh_trader.core.objects import Tick

    meta = RecordMeta(
        event_time=NOW,
        available_at=NOW,
        ingested_at=NOW,
        trading_day=D1,
        source_id="fixture-md",
        source_version="1",
        ingest_seq=1,
    )
    tick = Tick(
        instrument=RB,
        meta=meta,
        last_price=Decimal("120"),
        bid_price=None,
        ask_price=None,
        bid_volume=None,
        ask_volume=None,
        cumulative_volume=1,
        cumulative_turnover=Decimal("1"),
        open_interest=1,
        pre_settlement_price=None,
        upper_limit_price=None,
        lower_limit_price=None,
        phase=__import__("qh_trader.core.constants", fromlist=["MarketPhase"]).MarketPhase.CONTINUOUS,
    )
    ready.fact(
        CanonicalEvent(
            event_id="md-1",
            kind=EventKind.MARKET_DATA,
            event_time=NOW,
            available_at=NOW,
            sequence=0,
            source_id="fixture-md",
            payload=tick,
        )
    )
    assert ready.model.mark_prices == {RB: Decimal("120")}
    assert ready.model.fact_count == 0


# ---------------------------------------------------------------------- 与模拟网关的闭环


def test_simulated_gateway_reports_flow_back_through_the_model(tmp_path):
    gateway = SimulatedGateway(ACCOUNT, D1, price_tick=Decimal("1"))
    with Harness(tmp_path / "trading.db", gateway=gateway) as harness:
        harness.make_ready()
        harness.submit(command("c1", intent("o1", price=100)))
        for event in gateway.drain_events():
            harness.service.enqueue(event)
        harness.service.run_once()
        order = harness.model.orders.get_order("o1")
        assert order.status == OrderStatus.ACCEPTED and order.send_state == SendState.CONFIRMED_REMOTE
        assert harness.model.risk.today_open_lots(RB, D1) == 1


# ---------------------------------------------------------------------- 代次围栏与心跳


def test_epoch_fence_blocks_stale_epoch_at_the_gateway_call():
    inner = RecordingGateway()
    authority = {"current": CONTROL}
    fenced = EpochFencedGateway(inner, lambda: authority["current"])
    assert isinstance(fenced, ExecutionPort)
    assert fenced.submit(intent("o1"), CONTROL).state == SendState.SENT_UNKNOWN
    authority["current"] = ControlEpoch("replacement", 2)
    result = fenced.submit(intent("o2"), CONTROL)
    assert result.state == SendState.NOT_SENT and fenced.fenced_calls == 1
    assert len(inner.calls) == 1
    authority["current"] = None
    assert fenced.cancel(
        OrderIdentity(account_id=ACCOUNT, exchange=Exchange.SHFE, client_order_id="o1"), CONTROL
    ).state == (SendState.NOT_SENT)


def test_fenced_gateway_inside_service_rejects_after_control_changes(tmp_path):
    path = tmp_path / "trading.db"
    inner = RecordingGateway()
    holder: dict = {}
    fenced = EpochFencedGateway(inner, lambda: holder["store"].control().epoch if holder["store"].control() else None)
    with Harness(path, gateway=fenced) as harness:
        holder["store"] = harness.store
        harness.make_ready()
        assert harness.submit(command("c1", intent("o1"))).status == CommandStatus.SENT_UNKNOWN
        takeover = command(
            "take", TakeoverRequest(controller_id="replacement", reason="fixture"), kind=CommandKind.TAKEOVER_REQUEST
        )
        harness.client.submit(takeover)

        class Isolation:
            def isolate(self, previous, request):
                return True

        harness.service._ready = False  # noqa: SLF001 - 接管属于停止中的实例
        new_control = harness.service.take_over("take", Isolation())
        assert new_control.epoch == 2
        stale = command("c2", intent("o2"))
        harness.client.submit(stale)
        harness.service.process_next_command()
        assert harness.client.get("c2").status == CommandStatus.REJECTED_STALE
        assert len(inner.calls) == 1


def test_heartbeat_file_is_atomic_and_uses_monotonic_clock(tmp_path):
    clock = {"mono": 10.0}
    beat = HeartbeatFile(
        tmp_path / "hb" / "execution.json",
        role="execution",
        instance_id="exec-1",
        monotonic=lambda: clock["mono"],
        wall_time=lambda: NOW,
    )
    first = beat.beat(control_epoch=1, ready=False)
    clock["mono"] += 1.5
    second = beat.beat(control_epoch=1, ready=True)
    assert (first.sequence, second.sequence) == (1, 2)
    read = read_heartbeat(tmp_path / "hb" / "execution.json")
    assert read is not None and read.beat_monotonic == 11.5 and read.ready and read.control_epoch == 1
    assert read.beat_wall == NOW and read.instance_id == "exec-1"
    assert read_heartbeat(tmp_path / "hb" / "missing.json") is None
    assert not list((tmp_path / "hb").glob("*.tmp"))


def test_heartbeat_thread_does_not_touch_the_trading_database(ready, tmp_path):
    stop = threading.Event()
    beat = HeartbeatFile(tmp_path / "execution.json", role="execution")

    def loop():
        while not stop.is_set():
            beat.beat(control_epoch=1, ready=True)
            stop.wait(0.01)

    worker = threading.Thread(target=loop, daemon=True)
    worker.start()
    try:
        for index in range(5):
            assert ready.submit(command(f"c{index}", intent(f"o{index}"))).status == CommandStatus.SENT_UNKNOWN
    finally:
        stop.set()
        worker.join(timeout=2)
    assert read_heartbeat(tmp_path / "execution.json").sequence >= 1
    assert (tmp_path / "trading.db-execution.lock").exists()


def test_advance_event_is_idempotent_after_restart(tmp_path):
    path = tmp_path / "trading.db"
    with Harness(path) as first:
        first.make_ready()
        first.submit(command("c1", intent("o1")))
        first.fact(trade_event("t1"))
        first.fact(settlement_event(D1, "110"))
        first.fact(advance_event(D1, D2))
        first.fact(replace(advance_event(D1, D2), event_id="advance-again"))
        assert first.model.ledger.balance == Decimal("1100")
        assert first.model.trading_day == D2
    with Harness(path) as second:
        assert second.model.ledger.balance == Decimal("1100")
        assert second.model.trading_day == D2
        assert second.model.positions.get_position(RB, PositionSide.LONG).pos_yd == 1
        assert second.model.risk.trading_day == D2


def test_position_query_diff_keeps_trading_closed(harness):
    harness.recovery.start_recovery(expected_trading_day=D1)
    harness.recovery.begin_reconciliation()
    harness.recovery.merge_order_query(harness.query("orders"))
    harness.recovery.merge_trade_query(harness.query("trades"))
    remote = Position(
        instrument=RB, side=PositionSide.LONG, hedge_flag="SPECULATION", pos_yd=1, pos_td=0, frozen_yd=0, frozen_td=0
    )
    harness.recovery.reconcile_positions(harness.query("positions", (remote,)))
    funds = AccountFunds(Decimal("1000"), Decimal("1000"), Decimal("0"), Decimal("1000"))
    harness.recovery.reconcile_funds(harness.query("funds", (funds,)), harness.model.ledger.balance)
    with pytest.raises(ExecutionNotReadyError):
        harness.service.enable_after_reconciliation()
    assert not harness.service.ready


def test_settlement_time_travel_is_rejected(ready):
    ready.submit(command("c1", intent("o1")))
    ready.fact(trade_event("t1"))
    ready.fact(settlement_event(D1, "110"))
    ready.fact(advance_event(D1, D2))
    ready.service.enqueue(advance_event(D2, D1 - timedelta(days=1)))
    with pytest.raises(AccountModelCorruptionError):
        ready.service.run_once()
    assert not ready.service.ready
