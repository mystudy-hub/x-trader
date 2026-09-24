"""S5-08 结算单解析与本地账本比对 (FR-LED-08, A22)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from qh_trader.core.constants import Exchange, Offset, PositionSide, Side
from qh_trader.core.objects import InstrumentId, Trade, TradeKey
from qh_trader.data.statement import (
    STATEMENT_KIND_MTM,
    StatementFormatError,
    load_statement,
    reconcile_statement,
    render_report,
    statement_from_mapping,
    statement_from_text,
    statements_to_json,
)
from qh_trader.domain.ledger import AccountLedger
from scripts.live_assembly import local_day_figures

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "statements"
ACCOUNT = "live-model-account"
RB = InstrumentId(Exchange.SHFE, "rb2410")
D1, D2 = date(2024, 9, 10), date(2024, 9, 11)
NOW = datetime(2024, 9, 10, 1, tzinfo=timezone.utc)


def _trade(day, tid, side, offset, price):
    return Trade(
        account_id=ACCOUNT,
        instrument=RB,
        trading_day=day,
        trade_id=tid,
        side=side,
        offset=offset,
        quantity=1,
        price=Decimal(price),
        event_time=NOW,
        available_at=NOW,
        deduplication_key=TradeKey(ACCOUNT, Exchange.SHFE, day, tid),
    )


def a22_ledger() -> AccountLedger:
    """07 A22 手工账：100 开仓、结算 110、次日 108 平昨 (乘数 10、无手续费)."""
    ledger = AccountLedger(account_id=ACCOUNT, initial_capital=Decimal("1000"), trading_day=D1)
    ledger.on_trade(_trade(D1, "t1", Side.BUY, Offset.OPEN, "100"), multiplier=Decimal("10"))
    ledger.settle_day({RB: Decimal("110")}, D2)
    ledger.on_trade(_trade(D2, "t2", Side.SELL, Offset.CLOSE_YESTERDAY, "108"), multiplier=Decimal("10"))
    return ledger


def test_text_statement_parses_summary_trades_and_positions():
    statement = load_statement(FIXTURES / "a22_cross_day_mtm.txt")
    assert (statement.account_id, statement.trading_day, statement.kind) == (ACCOUNT, D2, STATEMENT_KIND_MTM)
    assert statement.summary.balance_start == Decimal("1100.00")
    assert statement.summary.close_pnl == Decimal("-20.00")
    assert statement.summary.balance_end == Decimal("1080.00")
    assert statement.summary.margin == Decimal("0.00")
    assert statement.positions == ()
    assert len(statement.trades) == 1 and statement.trades[0].trade_id == "t2"
    assert statement.trades[0].instrument == RB and statement.trades[0].price == Decimal("108.00")


def test_json_statement_matches_the_text_fixture():
    text = load_statement(FIXTURES / "a22_cross_day_mtm.txt")
    normalized = load_statement(FIXTURES / "a22_cross_day_mtm.json")
    assert normalized.summary == text.summary
    assert normalized.trades == text.trades
    assert normalized.positions == text.positions
    round_trip = statements_to_json([text])
    assert '"balance_end": "1080.00"' in round_trip


def test_a22_ledger_reconciles_with_the_statement_and_equity_only_grows_by_80():
    ledger = a22_ledger()
    local = local_day_figures(ledger, D2, margin_used=Decimal("0"))
    assert local.balance_end == Decimal("1080.00")
    assert local.close_pnl == Decimal("-20.00")
    assert ledger.realized_trade_pnl == Decimal("80")
    result = reconcile_statement(load_statement(FIXTURES / "a22_cross_day_mtm.json"), local)
    assert result.consistent, result.diffs
    assert "balance_end" in result.compared and "close_pnl" in result.compared
    assert "一致" in render_report(result)


def test_balance_or_position_mismatch_is_blocking():
    ledger = a22_ledger()
    ledger.on_trade(_trade(D2, "t3", Side.BUY, Offset.OPEN, "108"), multiplier=Decimal("10"))
    local = local_day_figures(ledger, D2, margin_used=Decimal("0"))
    result = reconcile_statement(load_statement(FIXTURES / "a22_cross_day_mtm.json"), local)
    assert not result.consistent
    items = {diff.item for diff in result.blocking}
    assert f"position:{RB}:LONG" in items
    assert "差异超过约定误差" in render_report(result)


def test_tolerance_and_missing_items_are_explicit():
    ledger = a22_ledger()
    local = local_day_figures(ledger, D2)
    statement = load_statement(FIXTURES / "a22_cross_day_mtm.json")
    shifted = statement_from_mapping(
        {
            "account_id": ACCOUNT,
            "trading_day": "2024-09-11",
            "kind": "MTM",
            "summary": {"balance_end": "1080.004"},
        }
    )
    result = reconcile_statement(shifted, local, tolerance=Decimal("0.01"))
    assert result.consistent
    assert "margin" in result.skipped and "close_pnl" in result.skipped
    strict = reconcile_statement(shifted, local, tolerance=Decimal("0"))
    assert not strict.consistent and strict.blocking[0].item == "balance_end"
    assert statement.summary.margin is not None and local.margin is None
    assert "margin" in reconcile_statement(statement, local).skipped


def test_trade_kind_statement_skips_mtm_specific_items():
    text = (FIXTURES / "a22_cross_day_mtm.txt").read_text(encoding="utf-8").replace("盯市", "逐笔对冲")
    statement = statement_from_text(text)
    assert statement.kind == "TRADE"
    result = reconcile_statement(statement, local_day_figures(a22_ledger(), D2))
    assert "close_pnl" in result.skipped and "mtm_pnl" in result.skipped


@pytest.mark.parametrize(
    "mutation",
    [
        lambda text: text.replace("客户号 Client ID：live-model-account", ""),
        lambda text: text.replace("期末结存 Balance c/f：            1080.00", ""),
        lambda text: text.replace("|上期所    |", "|火星所    |"),
    ],
)
def test_unrecognized_layout_fails_explicitly(mutation):
    text = mutation((FIXTURES / "a22_cross_day_mtm.txt").read_text(encoding="utf-8"))
    with pytest.raises(StatementFormatError):
        statement_from_text(text)


def test_positions_are_read_per_side_and_exchange_is_required():
    text = (
        (FIXTURES / "a22_cross_day_mtm.txt")
        .read_text(encoding="utf-8")
        .replace(
            "|共 0 条" + " " * 103 + "|",
            "|螺纹钢    |上期所    |rb2410    |2     |100.00    |1     |108.00    |110.00    |111.00    "
            "|10.00        |200.00      |投机  |",
        )
    )
    statement = statement_from_text(text)
    assert {(row.side, row.quantity) for row in statement.positions} == {
        (PositionSide.LONG, 2),
        (PositionSide.SHORT, 1),
    }
    with pytest.raises(StatementFormatError):
        statement_from_mapping(
            {
                "account_id": ACCOUNT,
                "trading_day": "2024-09-11",
                "summary": {"balance_end": "1"},
                "positions": [{"exchange": "??", "symbol": "rb2410", "side": "买", "quantity": 1}],
            }
        )


def test_split_position_rows_are_summed_before_comparison():
    # 同一合约同一方向分行列示 (投机 / 套保) 时按手数累加，不能后一行覆盖前一行
    statement = statement_from_mapping(
        {
            "account_id": ACCOUNT,
            "trading_day": "2024-09-11",
            "summary": {"balance_end": "1080.00"},
            "positions": [
                {"exchange": "SHFE", "symbol": "rb2410", "side": "买", "quantity": 1},
                {"exchange": "SHFE", "symbol": "rb2410", "side": "买", "quantity": 2},
            ],
        }
    )
    local = local_day_figures(a22_ledger(), D2)
    held = reconcile_statement(statement, replace(local, positions={(RB, PositionSide.LONG): 3}))
    assert held.consistent, held.diffs
    short = reconcile_statement(statement, replace(local, positions={(RB, PositionSide.LONG): 2}))
    assert [(diff.statement_value, diff.local_value) for diff in short.blocking] == [(3, 2)]


def test_trade_without_price_fails_instead_of_reading_zero():
    with pytest.raises(StatementFormatError, match="price"):
        statement_from_mapping(
            {
                "account_id": ACCOUNT,
                "trading_day": "2024-09-11",
                "summary": {"balance_end": "1"},
                "trades": [
                    {
                        "trade_id": "t2",
                        "exchange": "SHFE",
                        "symbol": "rb2410",
                        "side": "卖",
                        "offset": "平",
                        "price": "",
                        "quantity": 1,
                    }
                ],
            }
        )
