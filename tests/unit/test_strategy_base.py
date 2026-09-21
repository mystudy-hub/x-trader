"""Unit tests for StrategyBase and DualMovingAverageStrategy (S3-04, FR-ORD-08)."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from qh_trader.core.constants import Exchange, Offset, OrderType, Side
from qh_trader.core.objects import Bar, InstrumentId, RecordMeta
from qh_trader.core.ports import StrategyPort
from qh_trader.strategy.base import StrategyContext
from qh_trader.strategy.examples.trend_following import DualMovingAverageStrategy

RB_INST = InstrumentId(Exchange.SHFE, "rb2410")
BASE_TIME = datetime(2024, 9, 10, 1, 0, tzinfo=timezone.utc)
TRADING_DAY = date(2024, 9, 10)


class MockStrategyContext(StrategyContext):
    def __init__(self) -> None:
        self.orders: list[dict] = []
        self.positions: dict[InstrumentId, int] = {}
        self._counter = 0

    def now(self) -> datetime:
        return BASE_TIME

    def send_order(
        self,
        instrument: InstrumentId,
        side: Side,
        offset: Offset,
        quantity: int,
        order_type: OrderType = OrderType.MARKET,
        limit_price_ticks: int | None = None,
        *,
        strategy_id: str | None = None,
    ) -> str:
        self._counter += 1
        cid = f"order-{self._counter}"
        self.orders.append(
            {
                "client_order_id": cid,
                "instrument": instrument,
                "side": side,
                "offset": offset,
                "quantity": quantity,
                "order_type": order_type,
                "limit_price_ticks": limit_price_ticks,
                "strategy_id": strategy_id,
            }
        )
        return cid

    def buy(
        self,
        instrument: InstrumentId,
        quantity: int,
        offset: Offset = Offset.OPEN,
        limit_price_ticks: int | None = None,
        *,
        strategy_id: str | None = None,
    ) -> str:
        order_type = OrderType.LIMIT if limit_price_ticks is not None else OrderType.MARKET
        return self.send_order(
            instrument, Side.BUY, offset, quantity, order_type, limit_price_ticks, strategy_id=strategy_id
        )

    def sell(
        self,
        instrument: InstrumentId,
        quantity: int,
        offset: Offset = Offset.CLOSE,
        limit_price_ticks: int | None = None,
        *,
        strategy_id: str | None = None,
    ) -> str:
        order_type = OrderType.LIMIT if limit_price_ticks is not None else OrderType.MARKET
        return self.send_order(
            instrument, Side.SELL, offset, quantity, order_type, limit_price_ticks, strategy_id=strategy_id
        )

    def cancel_order(self, client_order_id: str) -> None:
        pass

    def get_position(self, instrument: InstrumentId) -> int:
        return self.positions.get(instrument, 0)

    def is_order_active(self, client_order_id: str) -> bool:
        return False

    def schedule_timer(self, at: datetime, timer_id: str, payload: object = None) -> None:
        pass


def make_bar_with_close(close: str, seq: int) -> Bar:
    start = datetime(2024, 9, 10, 1 + (seq // 60), seq % 60, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    meta = RecordMeta(
        event_time=end,
        available_at=end,
        ingested_at=end,
        trading_day=TRADING_DAY,
        source_id="test",
        source_version="v1",
        ingest_seq=seq,
    )
    p = Decimal(close)
    return Bar(
        instrument=RB_INST,
        meta=meta,
        bar_start=start,
        bar_end=end,
        open_time=start,
        interval="1h",
        open=p,
        high=p + 1,
        low=p - 1,
        close=p,
        volume=100,
        turnover=Decimal("10000"),
        open_interest=5000,
        includes_auction=False,
    )


def test_dual_moving_average_crossover() -> None:
    ctx = MockStrategyContext()
    strat = DualMovingAverageStrategy("dma-test", ctx, RB_INST, fast_window=3, slow_window=5, order_size=2)
    strat.on_start()
    assert strat.is_active

    # 前 4 根 Bar，不足 5 根，不触发任何单
    for i, c in enumerate(["10", "10", "10", "10"]):
        strat.on_bar(make_bar_with_close(c, i + 1))
    assert len(ctx.orders) == 0

    # 第 5 根 Bar: "10" -> closes=[10,10,10,10,10]，fast=10, slow=10，记录初值，不触发
    strat.on_bar(make_bar_with_close("10", 5))
    assert len(ctx.orders) == 0

    # 第 6 根 Bar: "20" -> closes=[10,10,10,10,20]，fast=(10+10+20)/3=13.33, slow=(10*4+20)/5=12
    # 之前 fast-slow=0，现在 fast-slow=1.33 > 0 -> 金叉！触发开多 2 手
    strat.on_bar(make_bar_with_close("20", 6))
    assert len(ctx.orders) == 1
    ord0 = ctx.orders[0]
    assert ord0["side"] == Side.BUY
    assert ord0["offset"] == Offset.OPEN
    assert ord0["quantity"] == 2
    assert ord0["strategy_id"] == "dma-test"  # 意图带策略归因
    assert isinstance(strat, StrategyPort)
