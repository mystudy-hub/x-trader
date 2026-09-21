"""S3 补充验收：执行参考价降级 (A21 / A25-07)、固定时刻执行策略 (FR-EXEC-02) 与数据质量标记 (A16)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from qh_trader.core.constants import (
    Exchange,
    ExecutionPolicy,
    MarketPhase,
    MissedExecutionPolicy,
    MissingRuleError,
    Offset,
    OrderStatus,
    PriceType,
    QualityFlag,
)
from qh_trader.core.objects import Bar, ExecutionReference, InstrumentId, Permissions, RecordMeta, Session
from qh_trader.core.ports import MarketDataPort
from qh_trader.data.calendar import CHINA_TZ, TradingCalendar
from qh_trader.data.session_gate import CalendarSessionGate
from qh_trader.engine.backtest_engine import BacktestEngine
from qh_trader.engine.base_engine import InstrumentEconomics
from qh_trader.gateway.simulated_gateway import SimulatedGateway
from qh_trader.strategy.base import StrategyBase, StrategyContext

RB = InstrumentId(Exchange.SHFE, "rb2410")
D1, D2 = date(2024, 9, 10), date(2024, 9, 11)
ECO = InstrumentEconomics(Decimal("10"), Decimal("1"), Decimal("5.0"), Decimal("0.1"), "test")


def bar(
    start: datetime,
    end: datetime,
    *,
    day: date,
    open_: str,
    session_id: str | None = "day",
    flags: QualityFlag = QualityFlag.OK,
    volume: int = 100,
    interval: str = "1h",
) -> Bar:
    op = Decimal(open_)
    meta = RecordMeta(
        event_time=end,
        available_at=end,
        ingested_at=end,
        trading_day=day,
        source_id="t",
        source_version="v1",
        ingest_seq=1,
        session_id=session_id,
        quality_flags=flags,
    )
    return Bar(
        instrument=RB,
        meta=meta,
        bar_start=start,
        bar_end=end,
        open_time=start,
        interval=interval,
        open=op,
        high=op + 20,
        low=op - 20,
        close=op + 5,
        volume=volume,
        turnover=Decimal("1"),
        open_interest=1,
        includes_auction=False,
    )


def at(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm), CHINA_TZ).astimezone(timezone.utc)


class BuyOnBar(StrategyBase):
    def __init__(self, strategy_id: str, context: StrategyContext, index: int, *, limit: int | None = None) -> None:
        super().__init__(strategy_id, context)
        self.index, self.n, self.limit = index, 0, limit

    def on_bar(self, b: Bar) -> None:
        self.n += 1
        if self.n == self.index:
            self.buy(b.instrument, 1, Offset.OPEN, limit_price_ticks=self.limit)


# ---------------------------------------------------------------------- 执行参考价 (A21 / A25-07)


class ReferencePort(MarketDataPort):
    """只对登记的 (session_id, reference_time) 返回观测；其余返回 None."""

    def __init__(self, observations: dict[tuple[str, datetime], Decimal]) -> None:
        self.observations = observations
        self.queries: list[tuple[str, datetime, PriceType, datetime]] = []

    def subscribe(self, instruments):  # pragma: no cover
        pass

    def bars(self, instrument, interval, until):  # pragma: no cover
        return ()

    def execution_reference(self, instrument, session_id, reference_time, price_type, known_at):
        self.queries.append((session_id, reference_time, price_type, known_at))
        price = self.observations.get((session_id, reference_time))
        if price is None:
            return None
        meta = RecordMeta(
            event_time=reference_time,
            available_at=reference_time,
            ingested_at=reference_time,
            trading_day=D1,
            source_id="ref",
            source_version="v1",
            ingest_seq=1,
            session_id=session_id,
        )
        return ExecutionReference(
            instrument=instrument,
            meta=meta,
            session_id=session_id,
            reference_time=reference_time,
            price_type=price_type,
            price=price,
            source_record_id="ref:1",
            resolution="1h",
        )

    def latest_tick(self, instrument, known_at):  # pragma: no cover
        return None

    def instrument_status(self, instrument, known_at):  # pragma: no cover
        return None


def hourly_bars(n: int = 3, session_id: str | None = "day") -> list[Bar]:
    return [
        bar(at(D1, 9 + i), at(D1, 10 + i), day=D1, open_=str(3000 + 10 * i), session_id=session_id) for i in range(n)
    ]


def test_execution_reference_price_is_used_for_open_candidates_and_queried_at_open_time() -> None:
    bars = hourly_bars()
    port = ReferencePort({("day", bars[1].open_time): Decimal("3012"), ("day", bars[2].open_time): Decimal("3025")})
    gw = SimulatedGateway("acc", D1, execution_reference_port=port, execution_price_type=PriceType.BAR_OPEN)
    eng = BacktestEngine(account_id="acc", gateway=gw, start_time=bars[0].bar_start, default_economics=ECO)
    eng.add_strategy(BuyOnBar("s", eng, 1))
    res = eng.run(bars)
    assert [(t.price, t.event_time) for t in res.trades] == [
        (Decimal("3012"), bars[1].open_time)
    ]  # 观测价而非 bar.open
    assert res.execution_degradations == ()
    assert all(known == ref for _, ref, _, known in port.queries)  # 只用开盘时点已可见的观测
    assert gw.execution_references_used >= 1


def test_missing_execution_reference_skips_open_candidate_without_backfilling() -> None:
    bars = hourly_bars()
    port = ReferencePort({("day", bars[2].open_time): Decimal("3020")})  # 第二根 Bar 没有开盘观测
    gw = SimulatedGateway("acc", D1, execution_reference_port=port)
    eng = BacktestEngine(account_id="acc", gateway=gw, start_time=bars[0].bar_start, default_economics=ECO)
    eng.add_strategy(BuyOnBar("s", eng, 1))
    res = eng.run(bars)
    assert [(t.price, t.event_time) for t in res.trades] == [(Decimal("3020"), bars[2].open_time)]  # 顺延，不回填
    assert len(res.execution_degradations) == 1
    degraded = res.execution_degradations[0]
    assert degraded.at == bars[1].open_time and degraded.action == "open_candidates_skipped"


def test_session_label_mismatch_is_a_degradation_not_a_silent_substitute() -> None:
    bars = hourly_bars(session_id="day_continuous")
    port = ReferencePort({("day", bars[1].open_time): Decimal("3012")})  # 观测登记在 'day'，Bar 标 'day_continuous'
    gw = SimulatedGateway("acc", D1, execution_reference_port=port)
    eng = BacktestEngine(account_id="acc", gateway=gw, start_time=bars[0].bar_start, default_economics=ECO)
    eng.add_strategy(BuyOnBar("s", eng, 1))
    res = eng.run(bars)
    assert res.total_trades == 0
    assert all("no visible BAR_OPEN observation" in d.reason for d in res.execution_degradations)


def test_strict_execution_reference_fails_explicitly() -> None:
    bars = hourly_bars()
    gw = SimulatedGateway("acc", D1, execution_reference_port=ReferencePort({}), strict_execution_reference=True)
    eng = BacktestEngine(account_id="acc", gateway=gw, start_time=bars[0].bar_start, default_economics=ECO)
    eng.add_strategy(BuyOnBar("s", eng, 1))
    with pytest.raises(MissingRuleError):
        eng.run(bars)


def test_limit_order_can_still_fill_in_continuous_trading_after_open_degradation() -> None:
    """A25-07：开盘候选缺观测时，限价单仍可按事前声明改用连续交易后的可观测执行 (Bar 结束估算)."""
    bars = hourly_bars()
    gw = SimulatedGateway("acc", D1, execution_reference_port=ReferencePort({}))
    eng = BacktestEngine(account_id="acc", gateway=gw, start_time=bars[0].bar_start, default_economics=ECO)
    eng.add_strategy(BuyOnBar("s", eng, 1, limit=3000))  # 第二根 Bar low = 2990 触价
    res = eng.run(bars)
    assert [(t.price, t.event_time) for t in res.trades] == [(Decimal("3000"), bars[1].bar_end)]


# ---------------------------------------------------------------------- NEXT_DAY_FIXED_TIME (FR-EXEC-02, A21)


def two_day_calendar() -> TradingCalendar:
    available = datetime(2024, 1, 1, tzinfo=timezone.utc)
    sessions = [
        Session(
            instrument=RB,
            session_id="day",
            trading_day=day,
            start=datetime.combine(day, time(9), CHINA_TZ),
            end=datetime.combine(day, time(15), CHINA_TZ),
            phase=MarketPhase.CONTINUOUS,
            permissions=Permissions(True, True, True),
            rule_version="v1",
            source_id="synthetic",
            available_at=available,
        )
        for day in (D1, D2)
    ]
    return TradingCalendar(
        sessions,
        trading_days=[D1, D2],
        coverage_start=D1,
        coverage_end=D2,
        version="v1",
        source_id="synthetic",
        available_at=available,
    )


def fixed_time_engine(bars: list[Bar], policy: MissedExecutionPolicy) -> tuple[BacktestEngine, SimulatedGateway]:
    gw = SimulatedGateway("acc", D1)
    eng = BacktestEngine(
        account_id="acc",
        gateway=gw,
        start_time=bars[0].bar_start,
        default_economics=ECO,
        session_gate=CalendarSessionGate(two_day_calendar()),
        execution_policy=ExecutionPolicy.NEXT_DAY_FIXED_TIME,
        fixed_time_before_close=timedelta(minutes=30),
        missed_execution=policy,
        trading_days=[D1, D2],
    )
    return eng, gw


def test_fixed_time_policy_requires_positive_offset() -> None:
    with pytest.raises(ValueError):
        BacktestEngine(
            account_id="acc",
            gateway=SimulatedGateway("acc", D1),
            default_economics=ECO,
            execution_policy=ExecutionPolicy.NEXT_DAY_FIXED_TIME,
        )


def test_fixed_time_policy_fills_at_bar_that_starts_exactly_at_target() -> None:
    d1_bar = bar(at(D1, 9), at(D1, 15), day=D1, open_="3000", interval="1d")
    d2_bars = [
        bar(
            at(D2, 9 + i // 2, 30 * (i % 2)),
            at(D2, 9 + (i + 1) // 2, 30 * ((i + 1) % 2)),
            day=D2,
            open_=str(3100 + i),
            interval="30m",
        )
        for i in range(12)
    ]
    bars = [d1_bar] + d2_bars
    eng, _ = fixed_time_engine(bars, MissedExecutionPolicy.CANCEL)
    eng.add_strategy(BuyOnBar("s", eng, 1))
    res = eng.run(bars)
    target = at(D2, 14, 30)
    assert [(t.price, t.event_time) for t in res.trades] == [(Decimal("3111"), target)]
    accepted = [e for e in res.events if e.kind.value == "ORDER_REPORT" and e.payload.status == OrderStatus.ACCEPTED]
    assert accepted[0].event_time == target
    assert res.missed_executions == ()


def test_fixed_time_policy_with_coarser_bars_is_a_real_miss_never_an_earlier_bar() -> None:
    d1_bar = bar(at(D1, 9), at(D1, 15), day=D1, open_="3000", interval="1d")
    d2_bars = [bar(at(D2, 9 + i), at(D2, 10 + i), day=D2, open_=str(3100 + i), interval="1h") for i in range(6)]
    bars = [d1_bar] + d2_bars
    eng, _ = fixed_time_engine(bars, MissedExecutionPolicy.CANCEL)
    eng.add_strategy(BuyOnBar("s", eng, 1))
    res = eng.run(bars)
    assert res.total_trades == 0  # 14:00 的 Bar 早于目标，15:00 没有 Bar；不能用更早或未完成的 Bar 替代
    assert len(res.missed_executions) == 1 and res.missed_executions[0].target_time == at(D2, 14, 30)
    assert res.rejected_intents[0].stage == "missed-execution"


# ---------------------------------------------------------------------- 数据质量标记 (A16)


def test_flagged_bars_are_not_matched_and_are_reported() -> None:
    bars = hourly_bars()
    bars[1] = bar(bars[1].bar_start, bars[1].bar_end, day=D1, open_="3010", flags=QualityFlag.INVALID)
    gw = SimulatedGateway("acc", D1)
    eng = BacktestEngine(account_id="acc", gateway=gw, start_time=bars[0].bar_start, default_economics=ECO)
    eng.add_strategy(BuyOnBar("s", eng, 1))
    res = eng.run(bars)
    assert [(t.price, t.event_time) for t in res.trades] == [(Decimal("3020"), bars[2].open_time)]  # 跳过无效 Bar
    assert [d.flags for d in res.degraded_bars] == ["INVALID"]
    assert res.quality_flag_counts == {"OK": 2, "INVALID": 1}


def test_strict_data_quality_fails_on_flagged_bar() -> None:
    bars = hourly_bars()
    bars[1] = bar(bars[1].bar_start, bars[1].bar_end, day=D1, open_="3010", flags=QualityFlag.MISSING)
    gw = SimulatedGateway("acc", D1)
    eng = BacktestEngine(
        account_id="acc", gateway=gw, start_time=bars[0].bar_start, default_economics=ECO, strict_data_quality=True
    )
    eng.add_strategy(BuyOnBar("s", eng, 1))
    with pytest.raises(MissingRuleError):
        eng.run(bars)


def test_synthetic_timing_flag_is_not_rejected_by_default() -> None:
    bars = [bar(b.bar_start, b.bar_end, day=D1, open_=str(b.open), flags=QualityFlag.SYNTHETIC) for b in hourly_bars()]
    gw = SimulatedGateway("acc", D1)
    eng = BacktestEngine(account_id="acc", gateway=gw, start_time=bars[0].bar_start, default_economics=ECO)
    eng.add_strategy(BuyOnBar("s", eng, 1))
    res = eng.run(bars)
    assert res.total_trades == 1 and res.degraded_bars == ()
