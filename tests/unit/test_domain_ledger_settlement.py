"""Ledger tests beyond the single hand example: multi-lot settlement, close-today attribution,
settlement versions, rounding, commissions and funds policy (S2-03, A06, A07, A08, A22)."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from qh_trader.core.constants import Exchange, Offset, PositionSide, Side
from qh_trader.core.objects import CommissionRule, InstrumentId, Trade, TradeKey
from qh_trader.domain.ledger import (
    AccountLedger,
    FundsPolicy,
    LedgerEntryKind,
    SettlementPendingError,
    SettlementVersionError,
)
from qh_trader.domain.rules import calculate_commission

ROOT = Path(__file__).resolve().parents[2]
D1, D2, D3 = date(2024, 9, 9), date(2024, 9, 10), date(2024, 9, 11)
M = Decimal("10")


@pytest.fixture
def inst():
    return InstrumentId(Exchange.SHFE, "rb2410")


def _t(inst: InstrumentId, day: date, tid: str, side: Side, off: Offset, qty: int, px: str) -> Trade:
    now = datetime.now(timezone.utc)
    return Trade(
        account_id="test-acc",
        instrument=inst,
        trading_day=day,
        trade_id=tid,
        side=side,
        offset=off,
        quantity=qty,
        price=Decimal(px),
        event_time=now,
        available_at=now,
        deduplication_key=TradeKey("test-acc", inst.exchange, day, tid),
    )


def test_multi_lot_settlement_rebases_each_lot_once(inst):
    """FR-LED-02: 昨仓按上一结算价、今仓按开仓价；结算后基准更新，绝不重复结算昨仓."""
    ledger = AccountLedger(account_id="test-acc", initial_capital=Decimal("10000"), trading_day=D1)
    ledger.on_trade(_t(inst, D1, "o1", Side.BUY, Offset.OPEN, 1, "100"), multiplier=M)
    assert ledger.settle_day({inst: Decimal("110")}, D2) == Decimal("100.00")
    ledger.on_trade(_t(inst, D2, "o2", Side.BUY, Offset.OPEN, 1, "105"), multiplier=M)
    # 昨仓 (120-110)*10 + 今仓 (120-105)*10 = 250
    assert ledger.settle_day({inst: Decimal("120")}, D3) == Decimal("250.00")
    assert ledger.balance == Decimal("10350.00")
    lots = ledger.get_instrument_ledger(inst).open_long_lots
    assert [(lot.price, lot.basis) for lot in lots] == [
        (Decimal("100"), Decimal("120")),
        (Decimal("105"), Decimal("120")),
    ]


def test_close_today_matches_today_lot_and_close_yesterday_matches_settled_lot(inst):
    ledger = AccountLedger(account_id="test-acc", initial_capital=Decimal("10000"), trading_day=D1)
    ledger.on_trade(_t(inst, D1, "o1", Side.BUY, Offset.OPEN, 1, "100"), multiplier=M)
    ledger.settle_day({inst: Decimal("105")}, D2)
    ledger.on_trade(_t(inst, D2, "o2", Side.BUY, Offset.OPEN, 1, "130"), multiplier=M)
    ct = ledger.on_trade(_t(inst, D2, "c1", Side.SELL, Offset.CLOSE_TODAY, 1, "128"), multiplier=M)
    assert (ct.mtm_close_pnl, ct.trade_close_pnl) == (Decimal("-20"), Decimal("-20"))
    assert ct.matched_lot_ids == ("o2",)
    cy = ledger.on_trade(_t(inst, D2, "c2", Side.SELL, Offset.CLOSE_YESTERDAY, 1, "128"), multiplier=M)
    assert (cy.mtm_close_pnl, cy.trade_close_pnl) == (Decimal("230"), Decimal("280"))
    assert cy.matched_lot_ids == ("o1",)
    assert ledger.balance == Decimal("10000") + 50 - 20 + 230
    assert ledger.realized_trade_pnl == Decimal("260")


def test_unified_close_matches_yesterday_first_then_today(inst):
    ledger = AccountLedger(account_id="test-acc", initial_capital=Decimal("10000"), trading_day=D1)
    ledger.on_trade(_t(inst, D1, "o1", Side.BUY, Offset.OPEN, 1, "100"), multiplier=M)
    ledger.settle_day({inst: Decimal("105")}, D2)
    ledger.on_trade(_t(inst, D2, "o2", Side.BUY, Offset.OPEN, 1, "130"), multiplier=M)
    rec = ledger.on_trade(_t(inst, D2, "c", Side.SELL, Offset.CLOSE, 2, "128"), multiplier=M)
    assert rec.matched_lot_ids == ("o1", "o2")
    assert rec.mtm_close_pnl == Decimal("230") + Decimal("-20")
    pos_l, _ = ledger.position_manager.get_both_positions(inst)
    assert pos_l.total_position == 0
    ledger.position_manager.verify_frozen_invariant()


def test_short_side_settlement_and_close(inst):
    ledger = AccountLedger(account_id="test-acc", initial_capital=Decimal("10000"), trading_day=D1)
    ledger.on_trade(_t(inst, D1, "s1", Side.SELL, Offset.OPEN, 2, "100"), multiplier=M)
    assert ledger.settle_day({inst: Decimal("90")}, D2) == Decimal("200.00")
    rec = ledger.on_trade(_t(inst, D2, "b1", Side.BUY, Offset.CLOSE_YESTERDAY, 2, "95"), multiplier=M)
    assert rec.mtm_close_pnl == Decimal("-100")
    assert rec.trade_close_pnl == Decimal("100")
    assert ledger.balance == Decimal("10100.00")


def test_settlement_pending_late_arrival_repeat_and_revision(inst):
    """A06: 结算价迟到 -> 待结算；重复同版本为空操作；修订以差额更正事件处理，只生效一次；旧版本拒绝."""
    ledger = AccountLedger(account_id="test-acc", initial_capital=Decimal("10000"), trading_day=D1)
    ledger.on_trade(_t(inst, D1, "o1", Side.BUY, Offset.OPEN, 1, "100"), multiplier=M)
    with pytest.raises(SettlementPendingError):
        ledger.settle_day({}, D2)
    assert ledger.current_trading_day == D1
    assert ledger.position_manager.get_position(inst, PositionSide.LONG).pos_td == 1

    assert ledger.settle_day({inst: Decimal("110")}, D2) == Decimal("100.00")
    assert ledger.settle_day({inst: Decimal("110")}, D2, trading_day=D1) == Decimal("0")
    assert ledger.balance == Decimal("10100.00")

    entry = ledger.revise_settlement(D1, inst, Decimal("112"), version="v2")
    assert entry.kind == LedgerEntryKind.SETTLEMENT_CORRECTION and entry.amount == Decimal("20.00")
    assert ledger.revise_settlement(D1, inst, Decimal("112"), version="v2") is None
    assert ledger.balance == Decimal("10120.00")
    with pytest.raises(SettlementVersionError):
        ledger.revise_settlement(D1, inst, Decimal("110"), version="v1")
    with pytest.raises(SettlementVersionError):
        ledger.revise_settlement(D1, inst, Decimal("111"), version="v2")
    # 修订后的基准用于次日平仓，已入余额的结算盈亏不再重复
    rec = ledger.on_trade(_t(inst, D2, "c", Side.SELL, Offset.CLOSE_YESTERDAY, 1, "115"), multiplier=M)
    assert rec.mtm_close_pnl == Decimal("30")
    assert ledger.balance == Decimal("10150.00")
    assert [e.version for e in ledger.entries if e.version] == ["v1", "v2"]
    assert ledger.strategy_pnl == Decimal("150.00")


def test_money_is_rounded_only_when_posted_and_cash_flows_are_excluded(inst):
    """FR-LED-04 / FR-LED-01: 盈亏全精度计算，入账时舍入到最小货币单位；外部现金流不计入策略收益."""
    ledger = AccountLedger(account_id="test-acc", initial_capital=Decimal("1000"), currency_unit=Decimal("0.01"))
    one = Decimal("1")
    ledger.on_trade(_t(inst, D1, "o", Side.BUY, Offset.OPEN, 3, "100.001"), multiplier=one)
    rec = ledger.on_trade(_t(inst, D1, "c", Side.SELL, Offset.CLOSE_TODAY, 3, "100.006"), multiplier=one)
    assert rec.mtm_close_pnl == Decimal("0.015")
    assert ledger.balance == Decimal("1000.02")
    assert ledger.entries[-1].amount == Decimal("0.02")
    ledger.transfer_cash(Decimal("500"), "deposit")
    assert ledger.balance == Decimal("1500.02")
    assert ledger.strategy_pnl == Decimal("0.02")


def test_commission_models_flow_into_ledger(inst):
    """A07: 按手 + 按金额组合费 5.00，平今与平昨费率不同，费用由规则计算后进入账本."""
    fixture = json.loads((ROOT / "tests/fixtures/ledger_examples.json").read_text(encoding="utf-8"))
    case = next(c for c in fixture["cases"] if c["id"] == "mixed_commission")["inputs"]
    rule = CommissionRule(
        Decimal(case["fee_per_lot"]), Decimal(case["fee_rate"]), Decimal(case["currency_unit"]), "ROUND_HALF_UP"
    )
    fee = calculate_commission(rule, Decimal(case["price"]), int(case["lots"]), Decimal(case["multiplier"]))
    assert fee == Decimal("5.00")

    ledger = AccountLedger(account_id="test-acc", initial_capital=Decimal("10000"), trading_day=D1)
    ledger.on_trade(_t(inst, D1, "o", Side.BUY, Offset.OPEN, 2, "1000"), commission=fee, multiplier=M)
    ct_rule = CommissionRule(Decimal("0"), Decimal("0.0002"), Decimal("0.01"), "ROUND_HALF_UP")
    ct_fee = calculate_commission(ct_rule, Decimal("1000"), 1, M)
    ledger.on_trade(_t(inst, D1, "c1", Side.SELL, Offset.CLOSE_TODAY, 1, "1000"), commission=ct_fee, multiplier=M)
    ledger.settle_day({inst: Decimal("1000")}, D2)
    cy_rule = CommissionRule(Decimal("0"), Decimal("0.0001"), Decimal("0.01"), "ROUND_HALF_UP")
    cy_fee = calculate_commission(cy_rule, Decimal("1000"), 1, M)
    ledger.on_trade(_t(inst, D2, "c2", Side.SELL, Offset.CLOSE_YESTERDAY, 1, "1000"), commission=cy_fee, multiplier=M)
    assert (ct_fee, cy_fee) == (Decimal("2.00"), Decimal("1.00"))
    assert ledger.total_commission == Decimal("8.00")
    assert ledger.balance == Decimal("9992.00")
    assert sum(1 for e in ledger.entries if e.kind == LedgerEntryKind.COMMISSION) == 3


def test_funds_reservations_and_floating_profit_policy(inst):
    """A08: 逐单资金预占计入冻结；浮盈使用权限按账户规则版本切换；保证金覆盖权益不与开仓额度相加."""
    conservative = AccountLedger(account_id="test-acc", initial_capital=Decimal("100000"), trading_day=D1)
    permissive = AccountLedger(
        account_id="test-acc",
        initial_capital=Decimal("100000"),
        trading_day=D1,
        funds_policy=FundsPolicy(version="broker-allows-floating", floating_profit_usable=True),
    )
    for ledger in (conservative, permissive):
        ledger.on_trade(_t(inst, D1, "o", Side.BUY, Offset.OPEN, 1, "3000"), multiplier=M)
        ledger.reserve_funds("pending", Decimal("1000"), Decimal("5"))
    prices, rates = {inst: Decimal("3100")}, {inst: Decimal("0.10")}
    c = conservative.get_funds_state(prices, rates)
    p = permissive.get_funds_state(prices, rates)
    assert c.frozen_margin == Decimal("1000") and c.frozen_fee == Decimal("5")
    assert c.unrealized_pnl == Decimal("1000")
    assert c.available_for_new_trades == Decimal("100000") - Decimal("3100") - Decimal("1005")
    assert p.available_for_new_trades == c.available_for_new_trades + Decimal("1000")
    assert c.margin_coverage_equity == p.margin_coverage_equity == Decimal("101000")
    assert c.funds_policy_version == "conservative-default"
    # 浮亏两种口径都扣减
    c2 = conservative.get_funds_state({inst: Decimal("2900")}, rates)
    p2 = permissive.get_funds_state({inst: Decimal("2900")}, rates)
    assert c2.available_for_new_trades == p2.available_for_new_trades
    assert c2.available_for_new_trades == Decimal("99000") - Decimal("2900") - Decimal("1005")
    conservative.release_funds("pending")
    assert conservative.frozen_margin == Decimal("0")
