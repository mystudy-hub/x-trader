"""Unit tests for Event Ledger, dual PnL views and §4.4 hand-calculation alignment (S2-03, A06, A22)."""

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from qh_trader.core.constants import Exchange, Offset, Side
from qh_trader.core.objects import InstrumentId, Trade, TradeKey
from qh_trader.domain.ledger import AccountLedger

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def sample_inst():
    return InstrumentId(Exchange.SHFE, "rb2410")


def make_trade(
    account_id: str,
    instrument: InstrumentId,
    trading_day: date,
    trade_id: str,
    side: Side,
    offset: Offset,
    quantity: int,
    price: Decimal,
) -> Trade:
    key = TradeKey(account_id, instrument.exchange, trading_day, trade_id)
    return Trade(
        account_id=account_id,
        instrument=instrument,
        trading_day=trading_day,
        trade_id=trade_id,
        side=side,
        offset=offset,
        quantity=quantity,
        price=price,
        event_time=datetime.now(timezone.utc),
        available_at=datetime.now(timezone.utc),
        deduplication_key=key,
    )


def test_cross_day_pnl_exact_hand_calculation(sample_inst):
    """规划 §4.4 与 tests/fixtures/ledger_examples.json 精确对账:

    输入: 初始资金 1000.00, 乘数 10, 无手续费.
          第 1 日: 100 开仓多头 1 手; 当日结算价 110.
          第 2 日: 108 平仓 1 手.
    预期:
          首日结算入账: +100.00 (balance -> 1100.00)
          次日盯市平仓: -20.00 (balance -> 1080.00)
          逐笔平仓盈亏: +80.00 (仅用于统计, 绝不重复计入余额)
          权益变化: +80.00 (最终资金 1080.00)
          最终持仓: 0
    """
    fixture_path = ROOT / "tests/fixtures/ledger_examples.json"
    fixture_data = json.loads(fixture_path.read_text(encoding="utf-8"))
    case = next(c for c in fixture_data["cases"] if c["id"] == "cross_day_pnl")
    inp = case["inputs"]
    exp = case["expected"]

    ledger = AccountLedger(
        account_id="test-acc",
        initial_capital=Decimal(inp["initial_cash"]),
    )

    day1 = date(2024, 9, 9)
    day2 = date(2024, 9, 10)
    mult = Decimal(str(inp["multiplier"]))

    # 1. 第 1 日开仓: 100 开多 1 手
    t_open = make_trade(
        account_id="test-acc",
        instrument=sample_inst,
        trading_day=day1,
        trade_id="T_OPEN",
        side=Side.BUY,
        offset=Offset.OPEN,
        quantity=int(inp["lots"]),
        price=Decimal(str(inp["open_price"])),
    )
    ledger.on_trade(t_open, commission=Decimal("0.0"), multiplier=mult)
    assert ledger.balance == Decimal(inp["initial_cash"])

    # 2. 第 1 日收盘结算: 结算价 110
    settle_pnl = ledger.settle_day(
        settlement_prices={sample_inst: Decimal(str(inp["settlement_price"]))},
        new_trading_day=day2,
    )
    # 首日结算入账 +100.00
    assert settle_pnl == Decimal(exp["day_one_settlement"])
    assert ledger.balance == Decimal(inp["initial_cash"]) + Decimal(exp["day_one_settlement"])

    # 3. 第 2 日平仓: 108 平仓 1 手
    t_close = make_trade(
        account_id="test-acc",
        instrument=sample_inst,
        trading_day=day2,
        trade_id="T_CLOSE",
        side=Side.SELL,
        offset=Offset.CLOSE_YESTERDAY,
        quantity=int(inp["lots"]),
        price=Decimal(str(inp["close_next_day"])),
    )
    close_record = ledger.on_trade(t_close, commission=Decimal("0.0"), multiplier=mult)
    assert close_record is not None

    # 次日盯市平仓盈亏: -20.00
    assert close_record.mtm_close_pnl == Decimal(exp["day_two_mtm_close_pnl"])
    # 逐笔交易平仓盈亏: +80.00
    assert close_record.trade_close_pnl == Decimal(exp["trade_close_pnl"])

    # 结存余额变为 1080.00
    assert ledger.balance == Decimal(exp["final_cash"])
    # 权益总变动为 +80.00
    assert (ledger.balance - Decimal(inp["initial_cash"])) == Decimal(exp["equity_change"])

    # 最终持仓归零
    pos_long, pos_short = ledger.position_manager.get_both_positions(sample_inst)
    assert pos_long.total_position == int(exp["final_position"])
    assert pos_short.total_position == 0


def test_funds_state_three_fields(sample_inst):
    """FR-LED-05: 验证资金三字段与开仓额度保守口径."""
    ledger = AccountLedger(account_id="test-acc", initial_capital=Decimal("100000.00"))
    mult = Decimal("10")

    # 开多 1 手 3000
    trade = make_trade(
        account_id="test-acc",
        instrument=sample_inst,
        trading_day=date(2024, 9, 9),
        trade_id="T1",
        side=Side.BUY,
        offset=Offset.OPEN,
        quantity=1,
        price=Decimal("3000"),
    )
    ledger.on_trade(trade, commission=Decimal("5.00"), multiplier=mult)
    assert ledger.balance == Decimal("99995.00")

    # 设定浮盈情景: 价格上涨到 3100 -> 浮盈 (3100 - 3000) * 1 * 10 = +1000
    # 保证金率 10% -> 保证金 3100 * 1 * 10 * 0.10 = 3100
    funds = ledger.get_funds_state(
        current_prices={sample_inst: Decimal("3100")},
        margin_rates={sample_inst: Decimal("0.10")},
    )
    assert funds.unrealized_pnl == Decimal("1000.00")
    assert funds.total_equity == Decimal("99995.00") + Decimal("1000.00")  # 100995.00
    assert funds.margin_used == Decimal("3100.00")

    # broker_available 允许包含浮盈
    assert funds.broker_available == funds.total_equity - funds.margin_used
    # available_for_new_trades 保守口径: 浮盈不增加开仓额度 (取 balance 与 total_equity 较小值)
    # min(99995, 100995) - 3100 = 99995 - 3100 = 96895
    assert funds.available_for_new_trades == Decimal("99995.00") - Decimal("3100.00")
