"""Unit tests for RecoveryCoordinator (A04) and DailyLifecycleManager (A26)."""

from datetime import date, datetime, timezone

import pytest

from qh_trader.core.constants import Exchange, Offset, OrderStatus, PositionSide, Side
from qh_trader.core.objects import InstrumentId, OrderIdentity, OrderUpdate, Position
from qh_trader.domain.lifecycle import DailyLifecycleManager, LifecyclePhase
from qh_trader.domain.orders import OrderManager
from qh_trader.domain.positions import PositionDetail
from qh_trader.domain.recovery import RecoveryCoordinator


@pytest.fixture
def sample_inst():
    return InstrumentId(Exchange.SHFE, "rb2410")


def test_recovery_coordinator_detects_external_order(sample_inst):
    """A04: 远端查询发现本地未知的外部委托，标记阻断性差异，禁止进入 READY."""
    mgr = OrderManager()
    recovery = RecoveryCoordinator(mgr)

    # 远端返回一条未知委托
    remote_update = OrderUpdate(
        identity=OrderIdentity(
            account_id="acc-1",
            exchange=Exchange.SHFE,
            exchange_order_id="ext-order-888",
        ),
        instrument=sample_inst,
        side=Side.BUY,
        offset=Offset.OPEN,
        status=OrderStatus.ACCEPTED,
        quantity=1,
        filled_quantity=0,
        event_time=datetime.now(timezone.utc),
        available_at=datetime.now(timezone.utc),
    )
    diffs = recovery.reconcile_orders([remote_update])
    assert len(diffs) == 1
    assert diffs[0].category == "order"
    assert "external order detected" in diffs[0].message
    assert recovery.can_enter_ready(diffs) is False


def test_recovery_coordinator_detects_position_mismatch(sample_inst):
    mgr = OrderManager()
    recovery = RecoveryCoordinator(mgr)

    local_positions = {
        (sample_inst, PositionSide.LONG): PositionDetail(
            instrument=sample_inst, side=PositionSide.LONG, pos_td=2, pos_yd=0
        )
    }
    # 远端持仓报告 0
    remote_positions = [
        Position(
            instrument=sample_inst,
            side=PositionSide.LONG,
            hedge_flag="SPECULATION",
            pos_yd=0,
            pos_td=0,
            frozen_yd=0,
            frozen_td=0,
        )
    ]
    diffs = recovery.reconcile_positions(local_positions, remote_positions)
    assert len(diffs) == 1
    assert "position mismatch" in diffs[0].message
    assert recovery.can_enter_ready(diffs) is False


def test_daily_lifecycle_semi_settlement_protection():
    """A26 / F10: 半结算异常保护，进入 SEMI_SETTLED_HOLD，严格禁止新增风险."""
    lifecycle = DailyLifecycleManager(current_trading_day=date(2024, 9, 9))
    lifecycle.on_reconciliation_passed()
    assert lifecycle.phase == LifecyclePhase.READY

    lifecycle.on_market_open()
    assert lifecycle.phase == LifecyclePhase.TRADING
    assert lifecycle.can_accept_new_risk() is True

    # 盘后检测到半结算 (持仓已切但资金未结转)
    lifecycle.on_semi_settlement_detected("positions rolled but cash not credited")
    assert lifecycle.phase == LifecyclePhase.SEMI_SETTLED_HOLD
    # 禁止新增风险
    assert lifecycle.can_accept_new_risk() is False

    # 官方结算到位
    lifecycle.on_settlement_confirmed(date(2024, 9, 9))
    assert lifecycle.phase == LifecyclePhase.SETTLED
    assert lifecycle.is_settlement_complete is True
