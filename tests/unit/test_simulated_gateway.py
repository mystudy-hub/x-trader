"""Unit tests for SimulatedGateway (S3-03, FR-MATCH-01~05, FR-EXEC-01~03)."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from qh_trader.core.constants import (
    EventKind,
    Exchange,
    LimitLiquidityScenario,
    Offset,
    OrderStatus,
    OrderType,
    QualityFlag,
    SendState,
    Side,
)
from qh_trader.core.objects import (
    Bar,
    ControlEpoch,
    InstrumentId,
    OrderIdentity,
    OrderIntent,
    OrderUpdate,
    RecordMeta,
    Trade,
)
from qh_trader.core.ports import ExecutionPort
from qh_trader.gateway.simulated_gateway import SimulatedGateway

RB_INST = InstrumentId(Exchange.SHFE, "rb2410")
EPOCH = ControlEpoch("controller-1", 1)
TRADING_DAY = date(2024, 9, 10)
BASE_TIME = datetime(2024, 9, 10, 1, 0, tzinfo=timezone.utc)
META = RecordMeta(
    event_time=BASE_TIME,
    available_at=BASE_TIME,
    ingested_at=BASE_TIME,
    trading_day=TRADING_DAY,
    source_id="test",
    source_version="v1",
    ingest_seq=1,
)


def make_bar(
    *,
    open_: str = "3000",
    high: str = "3050",
    low: str = "2980",
    close: str = "3020",
    volume: int = 100,
    open_time: datetime | None = None,
    bar_start: datetime | None = None,
    bar_end: datetime | None = None,
) -> Bar:
    start = bar_start or BASE_TIME
    end = bar_end or datetime(2024, 9, 10, 2, 0, tzinfo=timezone.utc)
    op_time = open_time or start
    meta = RecordMeta(
        event_time=end,
        available_at=end,
        ingested_at=end,
        trading_day=TRADING_DAY,
        source_id="test",
        source_version="v1",
        ingest_seq=1,
    )
    return Bar(
        instrument=RB_INST,
        meta=meta,
        bar_start=start,
        bar_end=end,
        open_time=op_time,
        interval="1h",
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
        turnover=Decimal("1000000"),
        open_interest=50000,
        includes_auction=False,
    )


def test_simulated_gateway_implements_execution_port() -> None:
    gw = SimulatedGateway("acc1", TRADING_DAY)
    assert isinstance(gw, ExecutionPort)
    caps = gw.capabilities()
    assert caps.value.profile_id == "simulated-gateway-profile"
    assert caps.value.values["close_today_support"].value is True


def test_order_submit_and_cancel() -> None:
    gw = SimulatedGateway("acc1", TRADING_DAY)
    order = OrderIntent(
        client_order_id="ord-1",
        account_id="acc1",
        strategy_id="strat-1",
        instrument=RB_INST,
        side=Side.BUY,
        offset=Offset.OPEN,
        quantity=5,
        order_type=OrderType.LIMIT,
        limit_price_ticks=3000,
        created_at=BASE_TIME,
    )
    res = gw.submit(order, EPOCH)
    assert res.state == SendState.SENT_UNKNOWN
    evts = gw.drain_events()
    assert len(evts) == 1
    assert evts[0].kind == EventKind.ORDER_REPORT
    assert evts[0].payload.status == OrderStatus.ACCEPTED

    # 撤单
    ref = OrderIdentity(account_id="acc1", exchange=Exchange.SHFE, client_order_id="ord-1")
    c_res = gw.cancel(ref, EPOCH)
    assert c_res.state == SendState.SENT_UNKNOWN
    c_evts = gw.drain_events()
    assert len(c_evts) == 1
    assert c_evts[0].payload.status == OrderStatus.CANCELLED


def test_zero_volume_no_fill() -> None:
    """A11 / FR-MATCH-01: 零成交量不成交."""
    gw = SimulatedGateway("acc1", TRADING_DAY)
    order = OrderIntent(
        client_order_id="ord-zero",
        account_id="acc1",
        strategy_id="strat-1",
        instrument=RB_INST,
        side=Side.BUY,
        offset=Offset.OPEN,
        quantity=5,
        order_type=OrderType.LIMIT,
        limit_price_ticks=3000,
        created_at=BASE_TIME,
    )
    gw.submit(order, EPOCH)
    gw.drain_events()

    bar = make_bar(volume=0)
    match_evts = gw.match_bar(bar)
    assert len(match_evts) == 0


def test_open_fill_with_slippage() -> None:
    """FR-MATCH-02 / FR-EXEC-02: 开盘成交加滑点."""
    gw = SimulatedGateway("acc1", TRADING_DAY, slippage_ticks=2, price_tick=Decimal("1"))
    order = OrderIntent(
        client_order_id="ord-open",
        account_id="acc1",
        strategy_id="strat-1",
        instrument=RB_INST,
        side=Side.BUY,
        offset=Offset.OPEN,
        quantity=3,
        order_type=OrderType.LIMIT,
        limit_price_ticks=3010,
        created_at=BASE_TIME,
    )
    gw.submit(order, EPOCH)
    gw.drain_events()

    bar = make_bar(open_="3000", volume=100)
    evts = gw.match_bar(bar)
    assert len(evts) == 2  # 1 Trade + 1 OrderUpdate
    trade_evt = [e for e in evts if e.kind == EventKind.TRADE_REPORT][0]
    trade: Trade = trade_evt.payload
    assert trade.quantity == 3
    # 3000 + 2 * 1 = 3002
    assert trade.price == Decimal("3002")

    ord_evt = [e for e in evts if e.kind == EventKind.ORDER_REPORT][0]
    ord_upd: OrderUpdate = ord_evt.payload
    assert ord_upd.status == OrderStatus.FILLED
    assert ord_upd.filled_quantity == 3


def test_participation_rate_budget() -> None:
    """FR-MATCH-04: 参与率共享预算."""
    # 参与率 0.1，bar.volume=50 -> 预算最多 5 手
    gw = SimulatedGateway("acc1", TRADING_DAY, participation_rate=Decimal("0.1"))
    order = OrderIntent(
        client_order_id="ord-part",
        account_id="acc1",
        strategy_id="strat-1",
        instrument=RB_INST,
        side=Side.BUY,
        offset=Offset.OPEN,
        quantity=10,
        order_type=OrderType.LIMIT,
        limit_price_ticks=3010,
        created_at=BASE_TIME,
    )
    gw.submit(order, EPOCH)
    gw.drain_events()

    bar = make_bar(open_="3000", volume=50)
    evts = gw.match_bar(bar)
    assert len(evts) == 2
    trade: Trade = [e for e in evts if e.kind == EventKind.TRADE_REPORT][0].payload
    assert trade.quantity == 5  # 只成交了 5 手
    ord_upd: OrderUpdate = [e for e in evts if e.kind == EventKind.ORDER_REPORT][0].payload
    assert ord_upd.status == OrderStatus.PARTIALLY_FILLED
    assert ord_upd.filled_quantity == 5


def test_limit_liquidity_scenarios() -> None:
    """FR-MATCH-03 / A12 / A20: 涨跌停情景."""
    # 1. DIRECTION_CONSERVATIVE: 涨停时买单不成交，卖单可成交
    gw_cons = SimulatedGateway(
        "acc1",
        TRADING_DAY,
        limit_liquidity_scenario=LimitLiquidityScenario.DIRECTION_CONSERVATIVE,
    )
    buy_order = OrderIntent(
        client_order_id="buy-1",
        account_id="acc1",
        strategy_id="strat-1",
        instrument=RB_INST,
        side=Side.BUY,
        offset=Offset.OPEN,
        quantity=2,
        order_type=OrderType.LIMIT,
        limit_price_ticks=3500,
        created_at=BASE_TIME,
    )
    sell_order = OrderIntent(
        client_order_id="sell-1",
        account_id="acc1",
        strategy_id="strat-1",
        instrument=RB_INST,
        side=Side.SELL,
        offset=Offset.OPEN,
        quantity=2,
        order_type=OrderType.LIMIT,
        limit_price_ticks=3300,
        created_at=BASE_TIME,
    )
    gw_cons.submit(buy_order, EPOCH)
    gw_cons.submit(sell_order, EPOCH)
    gw_cons.drain_events()

    # 涨停板收盘 3400
    bar_limit = make_bar(open_="3400", high="3400", low="3400", close="3400", volume=100)
    evts = gw_cons.match_bar(bar_limit, upper_limit=Decimal("3400"))
    # 买单不成交，卖单成交
    trades = [e.payload for e in evts if e.kind == EventKind.TRADE_REPORT]
    assert len(trades) == 1
    assert trades[0].side == Side.SELL

    # 2. TOUCH_LIMIT_NO_FILL: 触板无成交
    gw_nofill = SimulatedGateway(
        "acc1",
        TRADING_DAY,
        limit_liquidity_scenario=LimitLiquidityScenario.TOUCH_LIMIT_NO_FILL,
    )
    gw_nofill.submit(buy_order, EPOCH)
    gw_nofill.submit(sell_order, EPOCH)
    gw_nofill.drain_events()

    evts_nofill = gw_nofill.match_bar(bar_limit, upper_limit=Decimal("3400"))
    assert len(evts_nofill) == 0
