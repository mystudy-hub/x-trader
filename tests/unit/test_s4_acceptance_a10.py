"""S4 验收测试 (A10, FR-CON-02~07, FR-RISK-06, FR-VAL-03)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from qh_trader.core.constants import (
    Exchange,
    Offset,
    PositionSide,
    Side,
)
from qh_trader.core.objects import (
    Bar,
    ControlEpoch,
    InstrumentId,
    ProductId,
    RecordMeta,
    Trade,
    TradeKey,
)
from qh_trader.data.continuous import (
    AdjustmentMethod,
    ContinuousSeriesBuilder,
)
from qh_trader.data.dominant_contract import (
    build_dominant_mappings,
)
from qh_trader.domain.rollover import (
    LegOrderPolicy,
    RollManager,
    RollState,
)

PROD_RB = ProductId(Exchange.SHFE, "rb")
RB2410 = InstrumentId(Exchange.SHFE, "rb2410")
RB2501 = InstrumentId(Exchange.SHFE, "rb2501")
EPOCH = ControlEpoch("ctrl-1", 1)
BASE_TIME = datetime(2024, 8, 1, 0, 0, tzinfo=timezone.utc)


def test_a10_cross_day_order_and_spread_invariants() -> None:
    """A10: 验证主力切换交叉日时序、无虚假动量、两腿执行与状态机."""
    # 1. 连续确认机制推导映射
    day0 = BASE_TIME.date()
    day1 = (BASE_TIME + timedelta(days=1)).date()
    day2 = (BASE_TIME + timedelta(days=2)).date()

    def make_bar(inst, day_idx, oi, vol, cl):
        t_start = BASE_TIME + timedelta(days=day_idx)
        t_end = t_start + timedelta(hours=7)
        meta = RecordMeta(
            event_time=t_end,
            available_at=t_end,
            ingested_at=t_end,
            trading_day=t_start.date(),
            source_id="test",
            source_version="v1",
            ingest_seq=day_idx + 1,
        )
        return Bar(
            instrument=inst,
            meta=meta,
            bar_start=t_start,
            bar_end=t_end,
            open_time=t_start,
            interval="1d",
            open=Decimal(cl),
            high=Decimal(cl) + 10,
            low=Decimal(cl) - 10,
            close=Decimal(cl),
            volume=vol,
            turnover=Decimal("100000"),
            open_interest=oi,
            includes_auction=False,
        )

    # 模拟数据：Day 0~1 rb2410 oi 领先；Day 1 开始 rb2501 超过，第 2 天确认切换
    bars = [
        make_bar(RB2410, 0, 2000, 100, "3000"),
        make_bar(RB2501, 0, 1000, 50, "3200"),
        make_bar(RB2410, 1, 1500, 80, "3010"),
        make_bar(RB2501, 1, 1800, 120, "3210"),
        make_bar(RB2410, 2, 1200, 60, "3020"),
        make_bar(RB2501, 2, 2200, 200, "3230"),
    ]
    resolver = build_dominant_mappings(PROD_RB, bars, confirm_days=2)
    # Day 0 & Day 1 生效为 rb2410
    assert resolver.dominant(PROD_RB, BASE_TIME + timedelta(hours=2)).value == RB2410
    # Day 2 结束后切换生效为 rb2501
    assert resolver.dominant(PROD_RB, BASE_TIME + timedelta(days=2, hours=8)).value == RB2501

    # 2. 连续序列计算：切换日价差不计入虚假动量
    builder = ContinuousSeriesBuilder(resolver, method=AdjustmentMethod.DIFF)
    series = builder.build_series({
        RB2410: [b for b in bars if b.instrument == RB2410],
        RB2501: [b for b in bars if b.instrument == RB2501],
    })
    # 验证收益率均基于同合约自身变动，绝不包含 +200 点价差
    for c_bar in series:
        assert abs(c_bar.single_day_return) < Decimal("0.05")

    # 3. 移仓执行两腿状态机 (OPEN_FIRST 先开后平)
    roll_mgr = RollManager("acc1")
    task = roll_mgr.create_roll_task(
        product=PROD_RB,
        from_instrument=RB2410,
        to_instrument=RB2501,
        position_side=PositionSide.LONG,
        quantity=3,
        batch_size=3,
        policy=LegOrderPolicy.OPEN_FIRST,
    )
    # 先开新仓 (BUY OPEN)
    ord1 = roll_mgr.plan_next_order(task, BASE_TIME)
    assert ord1 is not None
    assert ord1.instrument == RB2501
    assert ord1.offset == Offset.OPEN
    assert ord1.side == Side.BUY

    # 新仓成交 3 手
    t1 = Trade(
        account_id="acc1",
        instrument=RB2501,
        trading_day=day0,
        trade_id="t-open-1",
        side=Side.BUY,
        offset=Offset.OPEN,
        quantity=3,
        price=Decimal("3200"),
        event_time=BASE_TIME,
        available_at=BASE_TIME,
        deduplication_key=TradeKey("acc1", Exchange.SHFE, day0, "t-open-1"),
    )
    roll_mgr.on_trade(t1, ord1.client_order_id)
    assert task.state == RollState.LEG_1_FILLED

    # 紧接着后平旧仓 (SELL CLOSE)
    ord2 = roll_mgr.plan_next_order(task, BASE_TIME)
    assert ord2 is not None
    assert ord2.instrument == RB2410
    assert ord2.offset == Offset.CLOSE
    assert ord2.side == Side.SELL

    t2 = Trade(
        account_id="acc1",
        instrument=RB2410,
        trading_day=day0,
        trade_id="t-close-1",
        side=Side.SELL,
        offset=Offset.CLOSE,
        quantity=3,
        price=Decimal("3000"),
        event_time=BASE_TIME,
        available_at=BASE_TIME,
        deduplication_key=TradeKey("acc1", Exchange.SHFE, day0, "t-close-1"),
    )
    roll_mgr.on_trade(t2, ord2.client_order_id)
    assert task.state == RollState.COMPLETED
    assert task.remaining_exposure_qty == 0
