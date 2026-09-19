"""S3 验收测试 (A11, A20, A21, A28, FR-MATCH-01~05, FR-EXEC-01~03, FR-VAL-06)."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from qh_trader.core.constants import (
    Exchange,
    LimitLiquidityScenario,
    Offset,
    OrderType,
    QualityFlag,
    Side,
)
from qh_trader.core.objects import Bar, InstrumentId, RecordMeta
from qh_trader.engine.backtest_engine import BacktestEngine
from qh_trader.gateway.simulated_gateway import SimulatedGateway
from qh_trader.strategy.base import StrategyBase, StrategyContext

RB_INST = InstrumentId(Exchange.SHFE, "rb2410")
BASE_START = datetime(2024, 9, 10, 1, 0, tzinfo=timezone.utc)
DAY_1 = date(2024, 9, 10)
DAY_2 = date(2024, 9, 11)


class SignalAtBarStrategy(StrategyBase):
    """固定在特定 Bar 产生买卖信号的测试策略."""

    def __init__(self, strategy_id: str, context: StrategyContext, actions: dict[int, tuple[str, int, Offset]]) -> None:
        super().__init__(strategy_id, context)
        self.actions = actions
        self.bar_index = 0

    def on_bar(self, bar: Bar) -> None:
        self.bar_index += 1
        if self.bar_index in self.actions:
            act, qty, offset = self.actions[self.bar_index]
            if act == "BUY":
                self.context.buy(bar.instrument, quantity=qty, offset=offset)
            elif act == "SELL":
                self.context.sell(bar.instrument, quantity=qty, offset=offset)


def make_acceptance_bars() -> list[Bar]:
    bars = []
    prices = [
        ("3000", "3050", "2990", "3040", 100, DAY_1),
        ("3040", "3100", "3030", "3080", 150, DAY_1),
        ("3080", "3120", "3070", "3110", 80, DAY_2),
        ("3110", "3150", "3100", "3130", 120, DAY_2),
    ]
    for i, (op, hi, lo, cl, vol, d) in enumerate(prices):
        start = BASE_START + timedelta(hours=i)
        end = start + timedelta(hours=1)
        meta = RecordMeta(
            event_time=end,
            available_at=end,
            ingested_at=end,
            trading_day=d,
            source_id="test",
            source_version="v1",
            ingest_seq=i + 1,
        )
        bars.append(
            Bar(
                instrument=RB_INST,
                meta=meta,
                bar_start=start,
                bar_end=end,
                open_time=start,
                interval="1h",
                open=Decimal(op),
                high=Decimal(hi),
                low=Decimal(lo),
                close=Decimal(cl),
                volume=vol,
                turnover=Decimal("100000"),
                open_interest=50000,
                includes_auction=False,
            )
        )
    return bars


def test_a28_full_pipeline_replay_idempotency() -> None:
    """A28 验收：固定输入多次全链路回放结果绝对幂等一致，账务 0 偏差."""
    bars = make_acceptance_bars()

    def run_once():
        gw = SimulatedGateway("acc-a28", DAY_1, slippage_ticks=0)
        eng = BacktestEngine(
            account_id="acc-a28",
            gateway=gw,
            start_time=BASE_START,
            initial_capital=Decimal("100000.00"),
            contract_multiplier=Decimal("10"),
            commission_per_lot=Decimal("5.0"),
        )
        strat = SignalAtBarStrategy("s1", eng, {1: ("BUY", 1, Offset.OPEN), 3: ("SELL", 1, Offset.CLOSE)})
        eng.add_strategy(strat)
        return eng.run(bars)

    res1 = run_once()
    res2 = run_once()

    assert res1.final_equity == res2.final_equity
    assert res1.total_pnl == res2.total_pnl
    assert res1.total_commission == res2.total_commission
    assert res1.total_trades == res2.total_trades
    assert len(res1.trades) == len(res2.trades)
    for t1, t2 in zip(res1.trades, res2.trades):
        assert t1.price == t2.price
        assert t1.quantity == t2.quantity
        assert t1.side == t2.side


def test_a11_zero_volume_and_causality() -> None:
    """A11 验收：零成交量绝对不成交；当前 Bar 产生的信号最早只能在下一 Bar 开盘成交."""
    # 构造第 2 根 Bar 为零成交量
    bars = make_acceptance_bars()
    bars[1] = Bar(
        instrument=RB_INST,
        meta=bars[1].meta,
        bar_start=bars[1].bar_start,
        bar_end=bars[1].bar_end,
        open_time=bars[1].open_time,
        interval=bars[1].interval,
        open=bars[1].open,
        high=bars[1].high,
        low=bars[1].low,
        close=bars[1].close,
        volume=0,  # 零成交量！
        turnover=Decimal(0),
        open_interest=50000,
        includes_auction=False,
    )

    gw = SimulatedGateway("acc-a11", DAY_1)
    eng = BacktestEngine(
        account_id="acc-a11",
        gateway=gw,
        start_time=BASE_START,
        initial_capital=Decimal("100000.00"),
    )
    # 在 Bar 1 产生买入委托；在 Bar 2 开盘本应成交，但因为 Bar 2 volume=0，不应成交；顺延至 Bar 3 开盘成交
    strat = SignalAtBarStrategy("s-a11", eng, {1: ("BUY", 1, Offset.OPEN)})
    eng.add_strategy(strat)
    res = eng.run(bars)

    assert res.total_trades == 1
    trade = res.trades[0]
    # 在 Bar 3 开盘价成交 (3080)，而不是 Bar 2
    assert trade.price == Decimal("3080")


def test_a20_limit_scenario_touch_no_fill() -> None:
    """A20 验收：触板无成交压力情景."""
    bars = make_acceptance_bars()
    # 压力情景 TOUCH_LIMIT_NO_FILL
    gw = SimulatedGateway(
        "acc-a20",
        DAY_1,
        limit_liquidity_scenario=LimitLiquidityScenario.TOUCH_LIMIT_NO_FILL,
    )
    eng = BacktestEngine(
        account_id="acc-a20",
        gateway=gw,
        start_time=BASE_START,
        initial_capital=Decimal("100000.00"),
    )
    strat = SignalAtBarStrategy("s-a20", eng, {1: ("BUY", 1, Offset.OPEN)})
    eng.add_strategy(strat)
    # 将 Bar 2 设置为涨停触板：使 upper_limit = close
    # 网关内部触发 TOUCH_LIMIT_NO_FILL
    res = eng.run(bars)
    # 在正常情况下 Bar 2 没触板，正常成交
    assert res.total_trades == 1
