"""Unit tests for ExchangeLimits, RiskManager, self-trade prevention and control epochs.

Covers: S2-05, S2-06, S2-10, A08, A09, A19, A23.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from qh_trader.core.constants import Exchange, Offset, OrderType, PositionSide, Side
from qh_trader.core.objects import InstrumentId, OrderIntent
from qh_trader.domain.ledger import AccountFundsState
from qh_trader.domain.limits import ExchangeLimits, LimitViolationError
from qh_trader.domain.orders import Order
from qh_trader.domain.positions import PositionDetail
from qh_trader.domain.risk import (
    EpochViolationError,
    RiskManager,
    RiskState,
    RiskViolationError,
)


@pytest.fixture
def sample_inst():
    return InstrumentId(Exchange.SHFE, "rb2410")


@pytest.fixture
def dummy_funds():
    return AccountFundsState(
        balance=Decimal("100000"),
        total_equity=Decimal("100000"),
        margin_used=Decimal("10000"),
        frozen_margin=Decimal("0"),
        frozen_fee=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        realized_mtm_pnl=Decimal("0"),
        realized_trade_pnl=Decimal("0"),
        total_commission=Decimal("0"),
        broker_available=Decimal("90000"),
        available_for_new_trades=Decimal("90000"),
        margin_coverage_equity=Decimal("100000"),
        risk_ratio=Decimal("0.10"),
    )


def test_exchange_limits_daily_open_and_position(sample_inst):
    limits = ExchangeLimits(
        max_open_lots_per_day={"SHFE.rb2410": 10},
        max_position_lots={"SHFE.rb2410": 20},
    )
    order = OrderIntent(
        client_order_id="ord-open",
        account_id="acc-1",
        strategy_id="strat-1",
        instrument=sample_inst,
        side=Side.BUY,
        offset=Offset.OPEN,
        quantity=6,
        order_type=OrderType.LIMIT,
        limit_price_ticks=3500,
        created_at=datetime.now(timezone.utc),
    )
    # 当前已开 5 手，再开 6 手 -> 11 > 10 超限
    with pytest.raises(LimitViolationError, match="daily open limit exceeded"):
        limits.check_order(order, current_open_lots_today=5, current_holding_lots=0)

    # 当前持仓 16 手，再开 6 手 -> 22 > 20 超限
    with pytest.raises(LimitViolationError, match="position limit exceeded"):
        limits.check_order(order, current_open_lots_today=0, current_holding_lots=16)


def test_risk_manager_control_epoch_violation(sample_inst, dummy_funds):
    risk = RiskManager(account_id="acc-1", initial_epoch=5)
    pos = PositionDetail(instrument=sample_inst, side=PositionSide.LONG)

    order = OrderIntent(
        client_order_id="ord-epoch",
        account_id="acc-1",
        strategy_id="strat-1",
        instrument=sample_inst,
        side=Side.BUY,
        offset=Offset.OPEN,
        quantity=1,
        order_type=OrderType.LIMIT,
        limit_price_ticks=3500,
        created_at=datetime.now(timezone.utc),
    )
    # 使用旧代次 4 发单 -> 必须被拒绝 (A23)
    with pytest.raises(EpochViolationError, match="command epoch expired"):
        risk.check_order(order, command_epoch=4, funds=dummy_funds, current_pos=pos)


def test_risk_manager_self_trade_prevention(sample_inst, dummy_funds):
    risk = RiskManager(account_id="acc-1")
    pos = PositionDetail(instrument=sample_inst, side=PositionSide.LONG)

    # 既有活动挂单: 卖单价格 3500
    sell_intent = OrderIntent(
        client_order_id="existing-sell",
        account_id="acc-1",
        strategy_id="strat-1",
        instrument=sample_inst,
        side=Side.SELL,
        offset=Offset.OPEN,
        quantity=1,
        order_type=OrderType.LIMIT,
        limit_price_ticks=3500,
        created_at=datetime.now(timezone.utc),
    )
    active_sell = Order(intent=sell_intent)

    # 新意图: 买单价格 3500 (交叉自成交)
    buy_intent = OrderIntent(
        client_order_id="new-buy",
        account_id="acc-1",
        strategy_id="strat-1",
        instrument=sample_inst,
        side=Side.BUY,
        offset=Offset.OPEN,
        quantity=1,
        order_type=OrderType.LIMIT,
        limit_price_ticks=3500,
        created_at=datetime.now(timezone.utc),
    )
    with pytest.raises(RiskViolationError, match="self-trade prevented"):
        risk.check_order(
            buy_intent,
            command_epoch=1,
            funds=dummy_funds,
            current_pos=pos,
            active_orders=[active_sell],
        )


def test_risk_manager_circuit_breaker_reduce_only(sample_inst, dummy_funds):
    risk = RiskManager(account_id="acc-1")
    risk.trigger_circuit_breaker(RiskState.REDUCE_ONLY, reason="market crash")
    pos = PositionDetail(instrument=sample_inst, side=PositionSide.LONG, pos_td=5)

    # 1. 开仓单被拒绝
    open_intent = OrderIntent(
        client_order_id="ord-open",
        account_id="acc-1",
        strategy_id="strat-1",
        instrument=sample_inst,
        side=Side.BUY,
        offset=Offset.OPEN,
        quantity=1,
        order_type=OrderType.LIMIT,
        limit_price_ticks=3500,
        created_at=datetime.now(timezone.utc),
    )
    with pytest.raises(RiskViolationError, match="open orders are blocked"):
        risk.check_order(open_intent, command_epoch=1, funds=dummy_funds, current_pos=pos)

    # 2. 合法平仓减仓单不能被阻断 (FR-RISK-05)
    close_intent = OrderIntent(
        client_order_id="ord-close",
        account_id="acc-1",
        strategy_id="strat-1",
        instrument=sample_inst,
        side=Side.SELL,
        offset=Offset.CLOSE_TODAY,
        quantity=1,
        order_type=OrderType.LIMIT,
        limit_price_ticks=3500,
        created_at=datetime.now(timezone.utc),
    )
    # 不抛出异常说明顺利通过
    risk.check_order(close_intent, command_epoch=1, funds=dummy_funds, current_pos=pos)
