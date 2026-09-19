"""Unit tests for HolidayRiskHook (S4-04, FR-RISK-06, A05)."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from qh_trader.core.constants import Exchange, Offset, OrderType, PositionSide, Side
from qh_trader.core.objects import (
    ControlEpoch,
    InstrumentId,
    OrderIntent,
    Position,
)
from qh_trader.domain.ledger import AccountFundsState
from qh_trader.domain.positions import PositionDetail
from qh_trader.domain.risk import (
    HolidayRiskHook,
    RiskManager,
    RiskViolationError,
)

RB_INST = InstrumentId(Exchange.SHFE, "rb2410")
EPOCH = ControlEpoch("ctrl-1", 1)


def make_dummy_funds() -> AccountFundsState:
    return AccountFundsState(
        balance=Decimal("1000000"),
        total_equity=Decimal("1000000"),
        margin_used=Decimal("0"),
        frozen_margin=Decimal("0"),
        frozen_fee=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        realized_mtm_pnl=Decimal("0"),
        realized_trade_pnl=Decimal("0"),
        total_commission=Decimal("0"),
        broker_available=Decimal("1000000"),
        available_for_new_trades=Decimal("1000000"),
        margin_coverage_equity=Decimal("1000000"),
        risk_ratio=Decimal("0"),
        funds_policy_version="v1",
    )


def test_holiday_risk_hook_blocks_open_but_allows_close() -> None:
    # 设 2024-10-01 为国庆长假开始日
    holidays = (date(2024, 10, 1),)
    hook = HolidayRiskHook(days_before_holiday=2, prevent_new_open=True)

    # 1. 节前窗口期内：2024-09-30 (距离 10-01 差 1 天)
    risk_mgr = RiskManager(
        "acc1",
        control=EPOCH,
        holiday_hook=hook,
        holiday_dates=holidays,
        trading_day=date(2024, 9, 30),
    )

    now = datetime(2024, 9, 30, 2, 0, tzinfo=timezone.utc)
    funds = make_dummy_funds()
    pos = PositionDetail(RB_INST, PositionSide.LONG, pos_yd=2)

    # 开仓买入 -> 应被长假钩子阻断
    open_order = OrderIntent(
        client_order_id="ord-open",
        account_id="acc1",
        strategy_id="strat-1",
        instrument=RB_INST,
        side=Side.BUY,
        offset=Offset.OPEN,
        quantity=1,
        order_type=OrderType.MARKET,
        created_at=now,
    )
    with pytest.raises(RiskViolationError, match="HolidayRiskHook: cannot open position"):
        risk_mgr.check_order(open_order, EPOCH, funds, pos, trading_day=date(2024, 9, 30))

    # 平仓卖出 -> 合法减仓，必须予以放行！
    close_order = OrderIntent(
        client_order_id="ord-close",
        account_id="acc1",
        strategy_id="strat-1",
        instrument=RB_INST,
        side=Side.SELL,
        offset=Offset.CLOSE_YESTERDAY,
        quantity=1,
        order_type=OrderType.MARKET,
        created_at=now,
    )
    # 不抛出异常即为通过
    risk_mgr.check_order(close_order, EPOCH, funds, pos, trading_day=date(2024, 9, 30))
