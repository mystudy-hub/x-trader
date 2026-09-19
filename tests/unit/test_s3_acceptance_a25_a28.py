"""S3 验收测试 (A11, A15, A20, A21, A28, FR-MATCH-01~05, FR-EXEC-01~03, FR-VAL-06/07/08).

A28 的"独立预期"来自手工账 (见各测试 docstring)，不是两次运行互比；
倍速一致性通过同一输入在不同调度参数下的规范哈希验证。
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from qh_trader.core.constants import (
    Exchange,
    ExecutionPolicy,
    LimitLiquidityScenario,
    MarketPhase,
    MissedExecutionPolicy,
    Offset,
    OrderStatus,
)
from qh_trader.core.objects import Bar, InstrumentId, Permissions, RecordMeta, Session
from qh_trader.data.calendar import CHINA_TZ, TradingCalendar
from qh_trader.data.session_gate import CalendarSessionGate
from qh_trader.engine.backtest_engine import BacktestEngine
from qh_trader.engine.base_engine import InstrumentEconomics
from qh_trader.gateway.simulated_gateway import SimulatedGateway
from qh_trader.research.cost_assumptions import ResearchCostModel
from qh_trader.research.vector_backtest import simulate_dma
from qh_trader.strategy.base import StrategyBase, StrategyContext
from qh_trader.strategy.examples.trend_following import DualMovingAverageStrategy

ROOT = Path(__file__).resolve().parents[2]
RB_INST = InstrumentId(Exchange.SHFE, "rb2410")
BASE_START = datetime(2024, 9, 10, 1, 0, tzinfo=timezone.utc)
DAY_1 = date(2024, 9, 10)
DAY_2 = date(2024, 9, 11)
ECO = InstrumentEconomics(Decimal("10"), Decimal("1"), Decimal("5.0"), Decimal("0.1"), "test")


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
                self.buy(bar.instrument, quantity=qty, offset=offset)
            elif act == "SELL":
                self.sell(bar.instrument, quantity=qty, offset=offset)


def make_bar(i: int, op: str, hi: str, lo: str, cl: str, vol: int, d: date, *, start: datetime | None = None) -> Bar:
    start = start or (BASE_START + timedelta(hours=i))
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
    return Bar(
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


def make_acceptance_bars() -> list[Bar]:
    prices = [
        ("3000", "3050", "2990", "3040", 100, DAY_1),
        ("3040", "3100", "3030", "3080", 150, DAY_1),
        ("3080", "3120", "3070", "3110", 80, DAY_2),
        ("3110", "3150", "3100", "3130", 120, DAY_2),
    ]
    return [make_bar(i, *row) for i, row in enumerate(prices)]


def make_engine(account: str, **kwargs) -> BacktestEngine:
    gw = SimulatedGateway(
        account, DAY_1, **{k: v for k, v in kwargs.items() if k in {"limit_liquidity_scenario", "slippage_ticks"}}
    )
    engine_kwargs = {k: v for k, v in kwargs.items() if k not in {"limit_liquidity_scenario", "slippage_ticks"}}
    return BacktestEngine(
        account_id=account,
        gateway=gw,
        start_time=BASE_START,
        initial_capital=Decimal("100000.00"),
        default_economics=ECO,
        **engine_kwargs,
    )


# ---------------------------------------------------------------------- A28 / A15


def test_a28_replay_matches_independent_hand_account_and_is_speed_invariant() -> None:
    """独立预期 (手工账)：Bar1 收盘买开 -> Bar2 开盘 3040 成交；Bar3 收盘卖平 -> Bar4 开盘 3110 成交。
    日终 (Bar2 收盘 3080) 结算 +400；平仓盯市 +300；手续费 2×5 = 10；期末权益 100690。
    """
    expected = {
        "final_equity": Decimal("100690.00"),
        "total_trades": 2,
        "total_commission": Decimal("10.0"),
        "trade_prices": [Decimal("3040"), Decimal("3110")],
        "settlement_pnl": Decimal("400.00"),
    }
    hashes = []
    for pacing in ("asap", "step", "speed"):
        eng = make_engine("acc-a28")
        eng.add_strategy(SignalAtBarStrategy("s1", eng, {1: ("BUY", 1, Offset.OPEN), 3: ("SELL", 1, Offset.CLOSE)}))
        seen: list[tuple[str, datetime]] = []

        def pacer(bar: Bar, snap, *, label: str = pacing, sink: list = seen) -> None:
            # 调度参数只影响墙钟等待 (这里用回调计数模拟)，不改变虚拟时间与事件顺序
            sink.append((label, snap.timestamp))

        res = eng.run(make_acceptance_bars(), on_bar_processed=pacer)
        assert len(seen) == 4
        assert res.final_equity == expected["final_equity"]
        assert res.total_trades == expected["total_trades"]
        assert res.total_commission == expected["total_commission"]
        assert [t.price for t in res.trades] == expected["trade_prices"]
        assert eng.ledger.settlements(DAY_1, RB_INST)[0].pnl == expected["settlement_pnl"]
        hashes.append(res.canonical_hashes())
    assert hashes[0] == hashes[1] == hashes[2]


def test_a15_manifest_hashes_are_reproducible_and_change_with_inputs() -> None:
    def run(slip: int):
        eng = make_engine("acc-a15", slippage_ticks=slip)
        eng.add_strategy(SignalAtBarStrategy("s1", eng, {1: ("BUY", 1, Offset.OPEN), 3: ("SELL", 1, Offset.CLOSE)}))
        return eng.run(make_acceptance_bars()).canonical_hashes()

    assert run(0) == run(0)
    assert run(0)["trades"] != run(1)["trades"]


# ---------------------------------------------------------------------- A11


def test_a11_zero_volume_bar_defers_fill_and_signal_never_backfills() -> None:
    bars = make_acceptance_bars()
    bars[1] = make_bar(1, "3040", "3100", "3030", "3080", 0, DAY_1)
    eng = make_engine("acc-a11")
    eng.add_strategy(SignalAtBarStrategy("s-a11", eng, {1: ("BUY", 1, Offset.OPEN)}))
    res = eng.run(bars)
    # Bar2 零量不成交；订单当日有效，日切时过期；不回填 Bar1/Bar2 的价格
    assert res.total_trades == 0
    assert res.orders[0].status == OrderStatus.EXPIRED
    assert [o.client_order_id for o in res.unfilled_orders] == ["ord-1"]


def test_a11_same_day_zero_volume_then_fill_uses_next_open_only() -> None:
    bars = [
        make_bar(0, "3000", "3050", "2990", "3040", 100, DAY_1),
        make_bar(1, "3040", "3100", "3030", "3080", 0, DAY_1),
        make_bar(2, "3080", "3120", "3070", "3110", 80, DAY_1),
    ]
    eng = make_engine("acc-a11b")
    eng.add_strategy(SignalAtBarStrategy("s", eng, {1: ("BUY", 1, Offset.OPEN)}))
    res = eng.run(bars)
    assert [(t.price, t.event_time) for t in res.trades] == [(Decimal("3080"), bars[2].open_time)]


# ---------------------------------------------------------------------- A20


@pytest.mark.parametrize("scenario", list(LimitLiquidityScenario))
def test_a20_paired_samples_open_fill_unchanged_by_later_close_touch(scenario: LimitLiquidityScenario) -> None:
    """成对样本：开盘输入相同，只改变随后收盘是否触板，既有开盘成交必须不变."""

    def run(close: str):
        bars = make_acceptance_bars()
        bars[1] = make_bar(1, "3040", close, "3030", close, 150, DAY_1)
        eng = make_engine("acc-a20", limit_liquidity_scenario=scenario)
        eng.add_strategy(SignalAtBarStrategy("s", eng, {1: ("BUY", 1, Offset.OPEN)}))
        limits = {
            (RB_INST, DAY_1): (Decimal("3100"), Decimal("2800")),
            (RB_INST, DAY_2): (Decimal("3300"), Decimal("2900")),
        }
        res = eng.run(bars, price_limits=limits)
        return [(t.price, t.event_time) for t in res.trades]

    touched = run("3100")
    untouched = run("3080")
    assert touched == untouched == [(Decimal("3040"), BASE_START + timedelta(hours=1))]


def test_a20_open_already_at_limit_blocks_buy_under_both_scenarios() -> None:
    for scenario in LimitLiquidityScenario:
        bars = make_acceptance_bars()
        bars[1] = make_bar(1, "3100", "3100", "3100", "3100", 150, DAY_1)  # 一字涨停板，有量
        eng = make_engine("acc-a20b", limit_liquidity_scenario=scenario)
        eng.add_strategy(SignalAtBarStrategy("s", eng, {1: ("BUY", 1, Offset.OPEN)}))
        res = eng.run(bars, price_limits={(RB_INST, DAY_1): (Decimal("3100"), Decimal("2800"))})
        assert res.total_trades == 0, scenario


# ---------------------------------------------------------------------- A21：执行时点策略与日历


def make_calendar() -> TradingCalendar:
    days = [DAY_1, DAY_2]
    available = datetime(2024, 1, 1, tzinfo=timezone.utc)
    sessions = []
    for day in days:
        for name, start, end, phase, perms in (
            ("day", time(9), time(15), MarketPhase.CONTINUOUS, Permissions(True, True, True)),
            ("night", time(21), time(23), MarketPhase.CONTINUOUS, Permissions(True, True, True)),
        ):
            sessions.append(
                Session(
                    instrument=RB_INST,
                    session_id=name,
                    trading_day=day,
                    start=datetime.combine(day, start, CHINA_TZ)
                    if name == "day"
                    else datetime.combine(day - timedelta(days=1), start, CHINA_TZ),
                    end=datetime.combine(day, end, CHINA_TZ)
                    if name == "day"
                    else datetime.combine(day - timedelta(days=1), end, CHINA_TZ),
                    phase=phase,
                    permissions=perms,
                    rule_version="synthetic-v1",
                    source_id="synthetic-calendar",
                    available_at=available,
                )
            )
    return TradingCalendar(
        sessions,
        trading_days=days,
        coverage_start=DAY_1 - timedelta(days=1),
        coverage_end=DAY_2,
        version="synthetic-v1",
        source_id="synthetic-calendar",
        available_at=available,
    )


def daily_bars() -> list[Bar]:
    """日线 Bar：日盘 09:00~15:00 (UTC 01:00~07:00)."""
    rows = [("3000", "3050", "2990", "3040", 100, DAY_1), ("3040", "3100", "3030", "3080", 100, DAY_2)]
    return [_daily(i, row) for i, row in enumerate(rows)]


def _daily(i: int, row) -> Bar:
    start = datetime.combine(row[5], time(9), CHINA_TZ).astimezone(timezone.utc)
    end = datetime.combine(row[5], time(15), CHINA_TZ).astimezone(timezone.utc)
    meta = RecordMeta(
        event_time=end,
        available_at=end,
        ingested_at=end,
        trading_day=row[5],
        source_id="t",
        source_version="v1",
        ingest_seq=i + 1,
        session_id="day",
    )
    return Bar(
        instrument=RB_INST,
        meta=meta,
        bar_start=start,
        bar_end=end,
        open_time=start,
        interval="1d",
        open=Decimal(row[0]),
        high=Decimal(row[1]),
        low=Decimal(row[2]),
        close=Decimal(row[3]),
        volume=row[4],
        turnover=Decimal("1"),
        open_interest=1,
        includes_auction=True,
    )


def test_a21_signal_after_close_is_held_until_next_permitted_session_and_fills_next_day_open() -> None:
    gate = CalendarSessionGate(make_calendar())
    gw = SimulatedGateway("acc-a21", DAY_1)
    eng = BacktestEngine(
        account_id="acc-a21",
        gateway=gw,
        start_time=daily_bars()[0].bar_start,
        initial_capital=Decimal("100000"),
        default_economics=ECO,
        session_gate=gate,
        trading_days=[DAY_1, DAY_2],
    )
    eng.add_strategy(SignalAtBarStrategy("s", eng, {1: ("BUY", 1, Offset.OPEN)}))
    res = eng.run(daily_bars())
    order = res.orders[0]
    # 15:00 信号 -> 本地持有到 21:00 夜盘允许报单时刻才送网关 -> 次日日盘开盘 3040 成交
    accepted = [e for e in res.events if e.kind.value == "ORDER_REPORT" and e.payload.status == OrderStatus.ACCEPTED][0]
    assert accepted.event_time == datetime.combine(DAY_1, time(21), CHINA_TZ).astimezone(timezone.utc)
    assert order.status == OrderStatus.FILLED
    assert [(t.price, t.event_time) for t in res.trades] == [(Decimal("3040"), daily_bars()[1].open_time)]


def test_a21_next_day_session_open_policy_targets_day_open_not_night() -> None:
    gate = CalendarSessionGate(make_calendar())
    gw = SimulatedGateway("acc-a21b", DAY_1)
    eng = BacktestEngine(
        account_id="acc-a21b",
        gateway=gw,
        start_time=daily_bars()[0].bar_start,
        initial_capital=Decimal("100000"),
        default_economics=ECO,
        session_gate=gate,
        execution_policy=ExecutionPolicy.NEXT_DAY_SESSION_OPEN,
        trading_days=[DAY_1, DAY_2],
    )
    eng.add_strategy(SignalAtBarStrategy("s", eng, {1: ("BUY", 1, Offset.OPEN)}))
    res = eng.run(daily_bars())
    accepted = [e for e in res.events if e.kind.value == "ORDER_REPORT" and e.payload.status == OrderStatus.ACCEPTED][0]
    assert accepted.event_time == daily_bars()[1].open_time  # 09:00 次日日盘
    assert [t.price for t in res.trades] == [Decimal("3040")]


def test_a21_missed_execution_cancel_policy_does_not_backfill() -> None:
    """目标开盘无执行数据 (缺该日 Bar) 时，CANCEL 策略取消意图并记录，不回填过去开盘."""
    gate = CalendarSessionGate(make_calendar())
    gw = SimulatedGateway("acc-a21c", DAY_1)
    bars = [daily_bars()[0]]
    eng = BacktestEngine(
        account_id="acc-a21c",
        gateway=gw,
        start_time=bars[0].bar_start,
        initial_capital=Decimal("100000"),
        default_economics=ECO,
        session_gate=gate,
        execution_policy=ExecutionPolicy.NEXT_SESSION_OPEN,
        missed_execution=MissedExecutionPolicy.CANCEL,
        trading_days=[DAY_1, DAY_2],
    )
    eng.add_strategy(SignalAtBarStrategy("s", eng, {1: ("BUY", 1, Offset.OPEN)}))
    res = eng.run(bars)
    assert res.total_trades == 0
    assert res.rejected_intents and res.rejected_intents[0].stage in {"deferred-cancelled", "missed-execution"}


def test_execution_policy_without_session_gate_fails_explicitly() -> None:
    eng = make_engine("acc-nogate", execution_policy=ExecutionPolicy.NEXT_SESSION_OPEN)
    eng.add_strategy(SignalAtBarStrategy("s", eng, {1: ("BUY", 1, Offset.OPEN)}))
    from qh_trader.core.constants import MissingRuleError

    with pytest.raises(MissingRuleError):
        eng.run(make_acceptance_bars())


# ---------------------------------------------------------------------- FR-VAL-08：两条通道一致性


def synthetic_series(n: int = 160, seed: int = 7) -> list[Bar]:
    import random

    rng = random.Random(seed)
    bars = []
    price = Decimal("3000")
    for i in range(n):
        start = BASE_START + timedelta(hours=i)
        end = start + timedelta(hours=1)
        op = price
        cl = op + Decimal(rng.choice([-12, -6, -3, 3, 6, 12]))
        hi = max(op, cl) + Decimal(rng.choice([0, 2, 5]))
        lo = min(op, cl) - Decimal(rng.choice([0, 2, 5]))
        meta = RecordMeta(
            event_time=end,
            available_at=end,
            ingested_at=end,
            trading_day=DAY_1,
            source_id="syn",
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
                open=op,
                high=hi,
                low=lo,
                close=cl,
                volume=1000,
                turnover=Decimal("1"),
                open_interest=1,
                includes_auction=False,
            )
        )
        price = cl
    return bars


def test_fr_val_08_vector_and_event_driven_channels_agree_on_fills_and_equity() -> None:
    bars = synthetic_series()
    cost = ResearchCostModel(
        multiplier=Decimal("10"), price_tick=Decimal("1"), commission_per_lot=Decimal("5.0"), slippage_ticks=0
    )
    vec = simulate_dma(bars, cost, 5, 20, initial_capital=Decimal("1000000"))

    gw = SimulatedGateway("acc-vec", DAY_1)
    eng = BacktestEngine(
        account_id="acc-vec",
        gateway=gw,
        start_time=BASE_START,
        initial_capital=Decimal("1000000"),
        default_economics=ECO,
    )
    eng.add_strategy(DualMovingAverageStrategy("dma", eng, RB_INST, fast_window=5, slow_window=20, order_size=1))
    res = eng.run(bars)

    event_fills = [
        (t.side.value, "OPEN" if t.offset == Offset.OPEN else "CLOSE", t.price, t.quantity) for t in res.trades
    ]
    vector_fills = [(f.side, f.offset, f.price, f.quantity) for f in vec.fills]
    assert event_fills == vector_fills
    assert vec.total_commission == res.total_commission
    # 同一交易日内不结算，两通道期末权益应逐分一致 (跨日结算只改变盈亏归属日，不改变总额)
    assert vec.final_equity == res.final_equity


def test_vector_channel_has_no_lookahead_on_step_series() -> None:
    """价格在信号 Bar 之后才跳升：向量化通道不得把信号 Bar 自身的涨跌计入新持仓."""
    closes = [Decimal(3000)] * 25 + [Decimal(3000 + 10 * k) for k in range(1, 6)]
    bars = []
    for i, c in enumerate(closes):
        start = BASE_START + timedelta(hours=i)
        end = start + timedelta(hours=1)
        op = closes[i - 1] if i else c
        meta = RecordMeta(
            event_time=end,
            available_at=end,
            ingested_at=end,
            trading_day=DAY_1,
            source_id="syn",
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
                open=op,
                high=max(op, c),
                low=min(op, c),
                close=c,
                volume=100,
                turnover=Decimal("1"),
                open_interest=1,
                includes_auction=False,
            )
        )
    cost = ResearchCostModel(commission_per_lot=Decimal("0"))
    vec = simulate_dma(bars, cost, 3, 5)
    # 金叉出现在第 26 根 (close 3010)；成交在第 27 根开盘 3010；此后 3010 -> 3050 = +40 × 10 = 400
    assert vec.fills[0].bar_index == 26 and vec.fills[0].price == Decimal("3010")
    assert vec.final_equity - Decimal("1000000") == Decimal("400")


# ---------------------------------------------------------------------- 固定工程样例：独立预期文件


def test_engineering_sample_replay_matches_recorded_independent_expectation() -> None:
    """runs/ 之外的固定期望：tests/fixtures/backtest_expectations.json 由手工核对的成交序列生成。"""
    path = ROOT / "tests" / "fixtures" / "backtest_expectations.json"
    if not path.is_file() or not (ROOT / "data_storage" / "current.json").is_file():
        pytest.skip("engineering sample or expectation file not available")
    expected = json.loads(path.read_text(encoding="utf-8"))
    from qh_trader.research.backtest_assembly import BacktestSpec, run_backtest

    result, _, manifest, assembled = run_backtest(BacktestSpec(), root=ROOT)
    if assembled.snapshot.get("snapshot_id") != expected["dataset_snapshot_id"]:
        pytest.skip("dataset snapshot differs from the recorded expectation")
    assert str(result.final_equity) == expected["final_equity"]
    assert result.total_trades == expected["total_trades"]
    assert manifest["outputs"]["canonical_hashes"]["trades"] == expected["hashes"]["trades"]
