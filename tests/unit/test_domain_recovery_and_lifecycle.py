"""Unit tests for RecoveryCoordinator (S2-08, A04) and DailyLifecycleManager (S2-09, A26)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from qh_trader.core.constants import Exchange, Offset, OrderStatus, OrderType, PositionSide, SendState, Side
from qh_trader.core.objects import AccountFunds, InstrumentId, LocalSendResult, OrderIntent
from qh_trader.domain.ledger import AccountLedger
from qh_trader.domain.lifecycle import (
    AccountSnapshot,
    DailyLifecycleManager,
    LifecyclePhase,
    SessionClosed,
    SettlementReady,
)
from qh_trader.domain.orders import OrderManager
from qh_trader.domain.recovery import DiffSeverity, RecoveryCoordinator, RecoveryPhase, RecoveryStateError
from qh_trader.infrastructure.memory_journal import MemoryJournal
from tests.fixtures.controllable_gateway import ACCOUNT, ControllableGateway

RB = InstrumentId(Exchange.SHFE, "rb2410")
TF = InstrumentId(Exchange.CFFEX, "T2412")
DAY = date(2024, 9, 10)
MULT = Decimal("10")


def intent(cid: str, side: Side, offset: Offset, qty: int) -> OrderIntent:
    return OrderIntent(
        client_order_id=cid,
        account_id=ACCOUNT,
        strategy_id="s1",
        instrument=RB,
        side=side,
        offset=offset,
        quantity=qty,
        order_type=OrderType.LIMIT,
        limit_price_ticks=3500,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def gw():
    return ControllableGateway()


@pytest.fixture
def stack():
    orders = OrderManager()
    ledger = AccountLedger(account_id=ACCOUNT, initial_capital=Decimal("100000"), trading_day=DAY)
    posted: list[str] = []

    def sink(trade, cid):
        ledger.on_trade(trade, multiplier=MULT, client_order_id=cid)
        posted.append(trade.trade_id)

    rec = RecoveryCoordinator(orders, ledger.position_manager, journal=MemoryJournal(ACCOUNT), trade_sink=sink)
    return orders, ledger, rec, posted


def submit(orders: OrderManager, ledger: AccountLedger, oi: OrderIntent, ref: str, gw: ControllableGateway):
    orders.create_order(oi)
    ledger.position_manager.reserve_for_order(oi.client_order_id, oi.instrument, oi.side, oi.offset, oi.quantity)
    orders.bind_session_identity(oi.client_order_id, gw.front_id, gw.session_id, ref)
    orders.get_order(oi.client_order_id).mark_submitting()
    orders.record_send_result(oi.client_order_id, LocalSendResult(SendState.SENT_UNKNOWN, 0, "sent"))


# ---------------------------------------------------------------------- 恢复协议
def test_recovery_protocol_phases_and_unknown_marking(stack, gw):
    orders, ledger, rec, _ = stack
    submit(orders, ledger, intent("o1", Side.BUY, Offset.OPEN, 1), "1", gw)
    assert rec.phase == RecoveryPhase.DISCONNECTED
    with pytest.raises(RecoveryStateError):
        rec.begin_reconciliation()
    report = rec.start_recovery(expected_trading_day=DAY)
    assert rec.phase == RecoveryPhase.RECOVERING
    assert report.unknown_orders == ["o1"]
    assert orders.get_order("o1").send_state == SendState.SENT_UNKNOWN
    rec.begin_reconciliation()
    assert rec.phase == RecoveryPhase.RECONCILING
    assert rec.can_enter_ready() is False  # 还没有任何完整查询


def test_replay_then_query_merge_does_not_double_book(stack, gw):
    orders, ledger, rec, posted = stack
    submit(orders, ledger, intent("o1", Side.BUY, Offset.OPEN, 2), "1", gw)
    ident = gw.identity(Exchange.SHFE, order_ref="1", exchange_order_id="SYS-1")
    fill_a = gw.trade_report(RB, Side.BUY, Offset.OPEN, 1, Decimal("3500"), identity=ident, trade_id="A")
    report = gw.order_report(RB, Side.BUY, Offset.OPEN, OrderStatus.PARTIALLY_FILLED, 2, 1, identity=ident)

    rec.start_recovery(expected_trading_day=DAY)
    assert rec.replay_events([report, fill_a]) == 2
    assert posted == ["A"]
    rec.begin_reconciliation()
    # 查询返回同一成交 A 与新成交 B；A 去重跳过，B 入账
    fill_b = gw.trade_report(RB, Side.BUY, Offset.OPEN, 1, Decimal("3500"), identity=ident, trade_id="B")
    rec.merge_trade_query(gw.query_result(gw.query_batch("q-t"), [fill_a.payload, fill_b.payload]))
    assert posted == ["A", "B"]
    filled = gw.order_report(RB, Side.BUY, Offset.OPEN, OrderStatus.FILLED, 2, 2, identity=ident)
    rec.merge_order_query(gw.query_result(gw.query_batch("q-o"), [filled.payload]))
    assert orders.get_order("o1").status == OrderStatus.FILLED
    assert ledger.position_manager.get_position(RB, PositionSide.LONG).pos_td == 2
    rec.reconcile_positions(gw.query_result(gw.query_batch("q-p"), [gw.position(RB, PositionSide.LONG, pos_td=2)]))
    assert rec.report.blocking == []
    assert rec.try_enter_ready() is True
    assert rec.phase == RecoveryPhase.READY


def test_incomplete_or_rate_limited_query_is_not_a_snapshot(stack, gw):
    orders, ledger, rec, _ = stack
    submit(orders, ledger, intent("o1", Side.BUY, Offset.OPEN, 1), "1", gw)
    rec.start_recovery(expected_trading_day=DAY)
    rec.begin_reconciliation()
    ident = gw.identity(Exchange.SHFE, order_ref="1")
    acc = gw.order_report(RB, Side.BUY, Offset.OPEN, OrderStatus.ACCEPTED, 1, 0, identity=ident)
    diffs = rec.merge_order_query(gw.rate_limited(gw.query_batch("q-o"), [acc.payload]))
    assert any(d.category == "query" and d.severity == DiffSeverity.BLOCKING for d in diffs)
    # 限流批次不完整：本地活动单未出现在结果里，也不能据此判定"查无此单"
    assert not any(d.message.startswith("local active order missing") for d in diffs)
    assert orders.get_order("o1").send_state == SendState.SENT_UNKNOWN
    assert rec.report.watermarks["orders"].complete is False
    rec.merge_trade_query(gw.query_result(gw.query_batch("q-t"), []))
    rec.reconcile_positions(gw.query_result(gw.query_batch("q-p"), []))
    assert rec.can_enter_ready() is False
    assert rec.try_enter_ready() is False


def test_mixed_trading_day_queries_cannot_be_published(stack, gw):
    orders, ledger, rec, _ = stack
    rec.start_recovery(expected_trading_day=DAY)
    rec.begin_reconciliation()
    rec.merge_order_query(gw.query_result(gw.query_batch("q-o"), []))
    rec.merge_trade_query(gw.query_result(gw.query_batch("q-t"), []))
    diffs = rec.reconcile_positions(gw.query_result(gw.query_batch("q-p", trading_day=DAY + timedelta(days=1)), []))
    assert any("mixed-day" in d.message for d in diffs)
    assert rec.can_enter_ready() is False


def test_local_active_order_missing_from_complete_query_only_escalates(stack, gw):
    orders, ledger, rec, _ = stack
    submit(orders, ledger, intent("c1", Side.SELL, Offset.OPEN, 1), "1", gw)
    ledger.reserve_funds("c1", Decimal("500"), Decimal("2"))
    rec.start_recovery(expected_trading_day=DAY)
    rec.begin_reconciliation()
    diffs = rec.merge_order_query(gw.query_result(gw.query_batch("q-o"), []))
    assert [d.message for d in diffs] == [
        "local active order missing from remote query; reservation kept, manual confirmation required"
    ]
    o = orders.get_order("c1")
    assert o.is_active and o.send_state == SendState.SENT_UNKNOWN and o.reconciliation_required
    assert ledger.frozen_margin == Decimal("500")
    assert rec.can_enter_ready() is False
    rec.resolve_diff(diffs[0], "broker confirmed never received; released manually")
    assert rec.report.blocking == []


def test_external_order_and_remote_only_position_are_blocking(stack, gw):
    orders, ledger, rec, _ = stack
    rec.start_recovery(expected_trading_day=DAY)
    rec.begin_reconciliation()
    ext = gw.order_report(
        RB,
        Side.BUY,
        Offset.OPEN,
        OrderStatus.ACCEPTED,
        1,
        0,
        identity=gw.identity(Exchange.SHFE, exchange_order_id="EXT-1"),
    )
    diffs = rec.merge_order_query(gw.query_result(gw.query_batch("q-o"), [ext.payload]))
    assert "external order detected" in diffs[0].message
    assert len(orders.external_orders) == 1
    diffs = rec.reconcile_positions(
        gw.query_result(gw.query_batch("q-p"), [gw.position(RB, PositionSide.SHORT, pos_yd=3)])
    )
    assert diffs[0].message == "remote position unknown to local system"
    assert diffs[0].severity == DiffSeverity.BLOCKING


def test_position_two_way_diff_and_frozen_hedge_fields(stack, gw):
    orders, ledger, rec, _ = stack
    pm = ledger.position_manager
    pm.get_position(RB, PositionSide.LONG).pos_td = 2
    pm.get_position(TF, PositionSide.SHORT).pos_yd = 1
    rec.start_recovery(expected_trading_day=DAY)
    rec.begin_reconciliation()
    remote = [
        gw.position(RB, PositionSide.LONG, pos_td=1, frozen_td=1),
        gw.position(TF, PositionSide.SHORT, pos_yd=1, hedge_flag="HEDGE"),
    ]
    diffs = rec.reconcile_positions(gw.query_result(gw.query_batch("q-p"), remote))
    ids = {d.identifier: d for d in diffs}
    assert ids[f"{TF}.SHORT.hedge_flag"].severity == DiffSeverity.BLOCKING
    assert ids[f"{RB}.LONG.pos_td"].local_value == 2 and ids[f"{RB}.LONG.pos_td"].remote_value == 1
    assert ids[f"{RB}.LONG.frozen"].severity == DiffSeverity.WARNING


def test_unlinked_trade_from_query_blocks_ready(stack, gw):
    orders, ledger, rec, posted = stack
    rec.start_recovery(expected_trading_day=DAY)
    rec.begin_reconciliation()
    orphan = gw.trade_report(RB, Side.BUY, Offset.OPEN, 1, Decimal("3500"), identity=None)
    diffs = rec.merge_trade_query(gw.query_result(gw.query_batch("q-t"), [orphan.payload]))
    assert diffs[0].category == "trade" and diffs[0].severity == DiffSeverity.BLOCKING
    assert posted == []
    assert len(orders.pending_unlinked_trades()) == 1


def test_funds_reconciliation_and_timeout_alarm(stack, gw):
    orders, ledger, rec, _ = stack
    rec.funds_tolerance = Decimal("0.01")
    deadline = datetime(2024, 9, 10, 1, 0, tzinfo=timezone.utc)
    rec.start_recovery(expected_trading_day=DAY, deadline=deadline)
    rec.begin_reconciliation()
    funds = AccountFunds(balance=Decimal("100000.02"), equity=None, margin=None, available_for_new_trades=None)
    diffs = rec.reconcile_funds(gw.query_result(gw.query_batch("q-f"), [funds]), ledger.balance)
    assert diffs[0].category == "funds" and diffs[0].severity == DiffSeverity.BLOCKING
    assert rec.check_timeout(deadline + timedelta(seconds=1)) is True
    assert rec.report.alarms


def test_ready_does_not_clear_existing_risk_halt(stack, gw):
    from qh_trader.domain.risk import RiskManager, RiskState

    orders, ledger, rec, _ = stack
    risk = RiskManager(account_id=ACCOUNT)
    risk.escalate("before disconnect", target=RiskState.HALTED)
    rec.start_recovery(expected_trading_day=DAY)
    rec.begin_reconciliation()
    rec.merge_order_query(gw.query_result(gw.query_batch("q-o"), []))
    rec.merge_trade_query(gw.query_result(gw.query_batch("q-t"), []))
    rec.reconcile_positions(gw.query_result(gw.query_batch("q-p"), []))
    assert rec.try_enter_ready() is True
    assert risk.risk_state == RiskState.HALTED


# ---------------------------------------------------------------------- 生命周期
def make_snapshot(
    batch: str, *, rolled: date, settled: date, complete: bool = True, day: date = DAY
) -> AccountSnapshot:
    return AccountSnapshot(
        trading_day=day,
        batch_id=batch,
        complete=complete,
        positions=(),
        balance=Decimal("1"),
        funds_settled_for=settled,
        positions_rolled_for=rolled,
        captured_at=datetime.now(timezone.utc),
    )


def test_login_does_not_resume_trading_and_gate_holds_past_open():
    lc = DailyLifecycleManager(current_trading_day=DAY)
    lc.on_login_success()
    assert lc.phase == LifecyclePhase.RECONCILING
    assert lc.can_accept_new_risk() is False
    assert lc.on_market_open(datetime(2024, 9, 10, 9, 0, tzinfo=timezone.utc)) is False
    assert lc.phase == LifecyclePhase.RECONCILING
    assert lc.gate_alarms
    assert lc.can_reduce_risk() is True  # 撤单 / 减仓仍可提交
    lc.on_reconciliation_passed()
    assert lc.on_market_open() is True
    assert lc.can_accept_new_risk() is True


def test_semi_settled_snapshot_never_overwrites_last_consistent():
    lc = DailyLifecycleManager(current_trading_day=DAY)
    lc.on_login_success()
    good = make_snapshot("b1", rolled=DAY, settled=DAY)
    assert lc.submit_query_snapshot(good) is True
    assert lc.last_consistent_snapshot is good
    semi = make_snapshot("b2", rolled=DAY + timedelta(days=1), settled=DAY)
    assert lc.submit_query_snapshot(semi) is False
    assert lc.last_consistent_snapshot is good
    assert lc.phase == LifecyclePhase.SEMI_SETTLED_HOLD
    assert lc.can_accept_new_risk() is False
    assert lc.can_reduce_risk() is True and lc.observation_active
    with pytest.raises(ValueError):
        lc.on_reconciliation_passed()
    incomplete = make_snapshot("b3", rolled=DAY, settled=DAY, complete=False)
    assert lc.submit_query_snapshot(incomplete) is False
    with pytest.raises(ValueError):
        lc.on_snapshot_verified(semi)
    verified = make_snapshot("b4", rolled=DAY + timedelta(days=1), settled=DAY + timedelta(days=1), day=DAY)
    lc.pending_snapshots.clear()
    lc.on_snapshot_verified(verified)
    assert lc.last_consistent_snapshot is verified
    assert lc.semi_settled_warning is False


def test_session_closed_per_instrument_and_settlement_ready_idempotent():
    lc = DailyLifecycleManager(current_trading_day=DAY, enabled_instruments={RB, TF})
    lc.on_login_success()
    lc.on_reconciliation_passed()
    lc.on_market_open()
    now = datetime(2024, 9, 10, 7, 0, tzinfo=timezone.utc)
    assert lc.on_session_closed(SessionClosed(RB, DAY, now, "cal-v1")) is False
    assert lc.phase == LifecyclePhase.TRADING  # 商品收盘不停国债
    assert lc.on_session_closed(SessionClosed(TF, DAY, now + timedelta(minutes=15), "cal-v1")) is True
    assert lc.phase == LifecyclePhase.POST_CLOSE
    lc.on_settlement_pending("prices not published")
    assert lc.phase == LifecyclePhase.SETTLEMENT_PENDING

    calls: list[str] = []
    ready = SettlementReady(DAY, "v1", {RB: Decimal("3450")}, "exchange-file", now)
    lc.on_settlement_ready(ready, settle=lambda ev: calls.append(ev.version))
    lc.on_settlement_ready(ready, settle=lambda ev: calls.append(ev.version))
    assert calls == ["v1"]
    assert lc.phase == LifecyclePhase.SETTLED and lc.has_completed("settle", DAY, "v1")
    # 修订版本是新的幂等键
    lc.on_settlement_ready(
        SettlementReady(DAY, "v2", {RB: Decimal("3451")}, "exchange-file", now),
        settle=lambda ev: calls.append(ev.version),
    )
    assert calls == ["v1", "v2"]


def test_advance_trading_day_requires_settlement_and_is_idempotent():
    lc = DailyLifecycleManager(current_trading_day=DAY)
    with pytest.raises(ValueError, match="settlement"):
        lc.advance_trading_day(DAY + timedelta(days=1))
    lc.on_settlement_confirmed(DAY, "v1")
    converted: list[date] = []
    ev = lc.advance_trading_day(DAY + timedelta(days=1), convert=lambda e: converted.append(e.new_trading_day))
    assert ev is not None and converted == [DAY + timedelta(days=1)]
    assert lc.phase == LifecyclePhase.INITIALIZING and lc.is_settlement_complete is False
    # 重复的日终任务：同一目标日不再转换
    assert (
        lc.advance_trading_day(DAY + timedelta(days=1), convert=lambda e: converted.append(e.new_trading_day)) is None
    )
    assert converted == [DAY + timedelta(days=1)]
    assert lc.has_completed("advance", DAY, "v1")


def test_ledger_settlement_is_idempotent_and_pending_blocks(gw):
    from qh_trader.domain.ledger import SettlementPendingError

    ledger = AccountLedger(account_id=ACCOUNT, initial_capital=Decimal("10000"), trading_day=DAY)
    ledger.on_trade(
        gw.trade_report(RB, Side.BUY, Offset.OPEN, 1, Decimal("100"), identity=None).payload, multiplier=MULT
    )
    with pytest.raises(SettlementPendingError):
        ledger.settle_day({}, DAY + timedelta(days=1))
    assert ledger.current_trading_day == DAY and ledger.settlement_pending[DAY] == (RB,)
    assert ledger.settle_day({RB: Decimal("110")}, DAY + timedelta(days=1)) == Decimal("100")
    assert ledger.settle_day({RB: Decimal("110")}, DAY + timedelta(days=1), trading_day=DAY) == Decimal("0")
    assert ledger.balance == Decimal("10100.00")
    pos = ledger.position_manager.get_position(RB, PositionSide.LONG)
    assert (pos.pos_yd, pos.pos_td) == (1, 0)
