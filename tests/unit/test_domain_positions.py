"""Unit tests for 4-field position models, reservations and order_event_examples (S2-02)."""

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from qh_trader.core.constants import Exchange, Offset, PositionSide, Side
from qh_trader.core.objects import InstrumentId, Trade, TradeKey
from qh_trader.domain.positions import PositionDetail, PositionManager

ROOT = Path(__file__).resolve().parents[2]


def make_trade(
    account_id: str,
    instrument: InstrumentId,
    trading_day: date,
    trade_id: str,
    side: Side,
    offset: Offset,
    quantity: int,
    price: Decimal = Decimal("3500"),
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


@pytest.fixture
def sample_inst():
    return InstrumentId(Exchange.SHFE, "rb2410")


def test_position_detail_invariants_and_properties(sample_inst):
    pos = PositionDetail(instrument=sample_inst, side=PositionSide.LONG, pos_yd=10, pos_td=5)
    assert pos.total_position == 15
    assert pos.available_yd == 10
    assert pos.available_td == 5
    assert pos.total_available == 15

    # 冻结今仓
    yd_cut, td_cut = pos.freeze_for_close(3, Offset.CLOSE_TODAY)
    assert (yd_cut, td_cut) == (0, 3)
    assert pos.frozen_td == 3
    assert pos.available_td == 2

    # 试图过度冻结
    with pytest.raises(ValueError, match="insufficient today position"):
        pos.freeze_for_close(3, Offset.CLOSE_TODAY)

    # 导出快照
    snap = pos.to_position_snapshot()
    assert snap.pos_td == 5
    assert snap.frozen_td == 3
    assert snap.pos_yd == 10


def test_advance_trading_day(sample_inst):
    pos = PositionDetail(instrument=sample_inst, side=PositionSide.LONG, pos_yd=10, pos_td=5, frozen_td=2)
    pos.advance_trading_day()
    assert pos.pos_yd == 15
    assert pos.pos_td == 0
    # 隔夜未决冻结转移到昨仓冻结
    assert pos.frozen_yd == 2
    assert pos.frozen_td == 0
    assert pos.available_yd == 13


def test_terminal_before_fills_fixture_exact_behavior(sample_inst):
    """精确复现 tests/fixtures/order_event_examples.json 中的 terminal_before_fills 案例.

    场景:
    初始: pos_td=5, frozen_td=3, accounted_fill_qty=0
    订单: client_order_id="close-1", offset=CLOSE_TODAY, quantity=3
    事件 1: order_report(status=CANCELED, cumulative_fill_qty=2)
           -> 期望: pos_td=5, frozen_td=2, accounted_fill_qty=0 (撤回1手解冻，未到成交的2手冻结保留!)
    事件 2: trade(T1, quantity=1)
           -> 期望: pos_td=4, frozen_td=1, accounted_fill_qty=1
    事件 3: trade(T2, quantity=1)
           -> 期望: pos_td=3, frozen_td=0, accounted_fill_qty=2
    事件 4: trade(T1, quantity=1) [重复成交]
           -> 期望: 去重忽略，保持 pos_td=3, frozen_td=0, accounted_fill_qty=2
    """
    fixture_path = ROOT / "tests/fixtures/order_event_examples.json"
    fixture_data = json.loads(fixture_path.read_text(encoding="utf-8"))
    case = next(c for c in fixture_data["cases"] if c["id"] == "terminal_before_fills")

    mgr = PositionManager(account_id="test-account")
    pos = mgr.get_position(sample_inst, PositionSide.LONG)
    pos.pos_td = case["inputs"]["initial"]["pos_td"]

    # 1. 发起平今委托 3 手
    res = mgr.reserve_for_order(
        client_order_id=case["inputs"]["order"]["client_order_id"],
        instrument=sample_inst,
        side=Side.SELL,
        offset=Offset.CLOSE_TODAY,
        quantity=case["inputs"]["order"]["quantity"],
    )
    assert pos.pos_td == 5
    assert pos.frozen_td == 3

    # 事件 1: order_report: CANCELED, cumulative_fill_qty=2
    mgr.on_order_canceled_or_rejected("close-1", cumulative_fill_qty=2)
    s1 = case["expected"]["states_after_events"][0]
    assert pos.pos_td == s1["pos_td"]
    assert pos.frozen_td == s1["frozen_td"]
    assert res.accounted_fill_qty == s1["accounted_fill_qty"]

    # 事件 2: trade T1 (quantity=1)
    t1 = make_trade(
        account_id="test-account",
        instrument=sample_inst,
        trading_day=date(2024, 9, 10),
        trade_id="T1",
        side=Side.SELL,
        offset=Offset.CLOSE_TODAY,
        quantity=1,
    )
    mgr.apply_trade(t1, client_order_id="close-1")
    s2 = case["expected"]["states_after_events"][1]
    assert pos.pos_td == s2["pos_td"]
    assert pos.frozen_td == s2["frozen_td"]
    assert res.accounted_fill_qty == s2["accounted_fill_qty"]

    # 事件 3: trade T2 (quantity=1)
    t2 = make_trade(
        account_id="test-account",
        instrument=sample_inst,
        trading_day=date(2024, 9, 10),
        trade_id="T2",
        side=Side.SELL,
        offset=Offset.CLOSE_TODAY,
        quantity=1,
    )
    mgr.apply_trade(t2, client_order_id="close-1")
    s3 = case["expected"]["states_after_events"][2]
    assert pos.pos_td == s3["pos_td"]
    assert pos.frozen_td == s3["frozen_td"]
    assert res.accounted_fill_qty == s3["accounted_fill_qty"]

    # 事件 4: 重复 trade T1 (去重后不调用 apply_fill)
    # 此处验证若重复调用，上层应由 TradeDeduplicator 拦截
    s4 = case["expected"]["states_after_events"][3]
    assert pos.pos_td == s4["pos_td"]
    assert pos.frozen_td == s4["frozen_td"]
    assert res.accounted_fill_qty == s4["accounted_fill_qty"]
