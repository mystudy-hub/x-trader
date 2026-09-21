"""A10 补充验收：纯价差切换、成对样本、部分成交、第二腿失败、本地拒单不冻结、长假钩子 (B1/B2/B3/M6/M8)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from qh_trader.core.constants import Exchange, Offset
from qh_trader.core.objects import Bar, InstrumentId, RecordMeta
from qh_trader.data.calendar import project_product_calendar
from qh_trader.data.continuous import AdjustmentReference
from qh_trader.data.storage import ParquetDataStorage
from qh_trader.domain.limits import ExchangeLimits, LimitKind, LimitRule, LimitSource
from qh_trader.domain.rollover import RollState
from qh_trader.engine.backtest_engine import BacktestEngine
from qh_trader.engine.base_engine import InstrumentEconomics
from qh_trader.gateway.simulated_gateway import SimulatedGateway
from qh_trader.research.multi_product import (
    RollAwareTrendStrategy,
    _exposure_trading_days,
    load_product_dataset,
    run_product,
)

NEAR = InstrumentId(Exchange.SHFE, "rb2501")
FAR = InstrumentId(Exchange.SHFE, "rb2505")
DAYS = [date(2024, 9, 2) + timedelta(days=index) for index in range(10)]


def _bar(instrument: InstrumentId, day: date, price: Decimal, *, open_interest: int, volume: int = 500) -> Bar:
    start = datetime(day.year, day.month, day.day, 1, 0, tzinfo=timezone.utc)
    end = datetime(day.year, day.month, day.day, 7, 0, tzinfo=timezone.utc)
    meta = RecordMeta(
        event_time=end,
        available_at=end,
        ingested_at=end,
        trading_day=day,
        session_id="day",
        source_id="test",
        source_version="v1",
        ingest_seq=1,
    )
    return Bar(
        instrument=instrument,
        meta=meta,
        bar_start=start,
        bar_end=end,
        open_time=start,
        interval="1d",
        open=price,
        high=price + 2,
        low=price - 2,
        close=price,
        volume=volume,
        turnover=Decimal(0),
        open_interest=open_interest,
        includes_auction=False,
    )


def _template(tmp_path: Path, days: list[date] = DAYS) -> Path:
    payload = {
        "schema_version": 2,
        "version": "test-sessions-v1",
        "source_id": "test-sessions",
        "available_at": "2024-01-01T00:00:00+00:00",
        "coverage_start": days[0].isoformat(),
        "coverage_end": days[-1].isoformat(),
        "trading_days": [d.isoformat() for d in days],
        "session_profiles": [
            {
                "exchange": "SHFE",
                "symbol": "rb2501",
                "product": "rb",
                "has_night": True,
                "night_close": "23:00:00",
                "day_auction_style": "RE_AUCTION",
            }
        ],
        "night_session_exceptions": [],
    }
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _publish(storage: ParquetDataStorage, cross_index: int, near_px, far_px, days: list[date] = DAYS) -> None:
    for index, day in enumerate(days):
        storage.publish_batch(
            NEAR,
            "1d",
            bars=[_bar(NEAR, day, Decimal(near_px(index)), open_interest=5000 if index < cross_index else 100)],
        )
        storage.publish_batch(
            FAR, "1d", bars=[_bar(FAR, day, Decimal(far_px(index)), open_interest=100 if index < cross_index else 5000)]
        )


def _run(
    tmp_path: Path, storage: ParquetDataStorage, *, days: list[date] = DAYS, fast: int = 2, slow: int = 4, **kwargs
):
    template = _template(tmp_path, days)
    dataset = load_product_dataset("rb", storage=storage, session_template=template, contract_catalog=None)
    calendar = project_product_calendar(template, "rb", dataset.contracts, window=(days[0], days[-1]))
    return dataset, run_product(
        dataset,
        account_id="acc",
        initial_capital=Decimal("400000"),
        fast_window=fast,
        slow_window=slow,
        order_size=1,
        calendar=calendar,
        **kwargs,
    )


# ---------------------------------------------------------------------- A10：纯价差切换


def test_pure_spread_switch_produces_no_signal_no_trade_and_no_ledger_pnl(tmp_path: Path) -> None:
    """新旧合约各自价格恒定、只有 100 点价差：连续序列必须平直，策略不得交易，账本不得有盈亏 (A10 / FR-CON-03)."""
    storage = ParquetDataStorage(tmp_path)
    _publish(storage, 5, lambda i: 3000, lambda i: 3100)
    dataset, run = _run(tmp_path, storage)
    assert len({c.adjusted_close for c in dataset.continuous}) == 1
    assert all(
        c.adjustment_reference in (AdjustmentReference.SAME_CONTRACT, AdjustmentReference.PREV_DAY_BOTH)
        for c in dataset.continuous
    )
    assert dataset.degraded_days == frozenset()
    assert run.result.trades == ()
    assert run.result.total_pnl == 0 and run.result.total_commission == 0


def test_dominant_switch_effective_next_session_and_past_decisions_invariant(tmp_path: Path) -> None:
    """成对样本：只有未来交叉日不同，既往映射与连续序列必须一致；切换在确认日收盘后的下一时段生效 (FR-CON-02)."""
    storage_a, storage_b = ParquetDataStorage(tmp_path / "a"), ParquetDataStorage(tmp_path / "b")
    _publish(storage_a, 5, lambda i: 3000 + i, lambda i: 3050 + i)
    _publish(storage_b, 8, lambda i: 3000 + i, lambda i: 3050 + i)
    template = _template(tmp_path)
    ds_a = load_product_dataset("rb", storage=storage_a, session_template=template, contract_catalog=None)
    ds_b = load_product_dataset("rb", storage=storage_b, session_template=template, contract_catalog=None)
    probe = [datetime(d.year, d.month, d.day, 7, 0, tzinfo=timezone.utc) for d in DAYS[:6]]
    assert [ds_a.dominant_on(d.date(), at=d) for d in probe] == [ds_b.dominant_on(d.date(), at=d) for d in probe]
    assert [c.adjusted_close for c in ds_a.continuous[:6]] == [c.adjusted_close for c in ds_b.continuous[:6]]
    first_switch = ds_a.resolver.entries[0]
    # 第 6 日 (index 5) 交叉、第 7 日确认 -> 第 7 日 15:00 决策，第 8 日夜盘 (第 7 日 21:00) 生效
    assert first_switch.trading_day == DAYS[6]
    assert first_switch.effective_to > first_switch.decision_time
    assert ds_a.resolver.entries[1].effective_basis == "session_gate"


# ---------------------------------------------------------------------- 部分成交、第二腿失败、本地拒单


def test_rollover_with_partial_fills_hedges_batch_by_batch(tmp_path: Path) -> None:
    # 每根 Bar 预算 1 手且对成交的反应只能在下一 Bar 生效：2 手两腿分批需要多根 Bar，样本取 16 天
    days = [date(2024, 9, 2) + timedelta(days=index) for index in range(16)]
    storage = ParquetDataStorage(tmp_path)
    _publish(storage, 4, lambda i: 3000 + 5 * i, lambda i: 3050 + 5 * i, days=days)
    template = _template(tmp_path, days)
    dataset = load_product_dataset("rb", storage=storage, session_template=template, contract_catalog=None)
    calendar = project_product_calendar(template, "rb", dataset.contracts, window=(days[0], days[-1]))
    from qh_trader.data.session_gate import CalendarSessionGate

    gate = CalendarSessionGate(calendar)
    # 每根 Bar 预算 1 手 (volume 500 × 0.002)，2 手移仓必须分两批
    gw = SimulatedGateway("acc", days[0], participation_rate=Decimal("0.002"), session_gate=gate)
    eng = BacktestEngine(
        account_id="acc",
        gateway=gw,
        start_time=dataset.bars[0].bar_start,
        initial_capital=Decimal("400000"),
        session_gate=gate,
        trading_days=days,
    )
    for c in dataset.contracts:
        eng.register_instrument(
            c, InstrumentEconomics(Decimal("10"), Decimal("1"), Decimal("1.5"), Decimal("0.1"), "t")
        )
    strat = RollAwareTrendStrategy(
        strategy_id="s", context=eng, dataset=dataset, fast_window=2, slow_window=4, order_size=2, account_id="acc"
    )
    eng.add_strategy(strat)
    res = eng.run(dataset.bars)
    closes = [t for t in res.trades if t.instrument == NEAR and t.offset != Offset.OPEN]
    opens_far = [t for t in res.trades if t.instrument == FAR and t.offset == Offset.OPEN]
    assert sum(t.quantity for t in closes) == 2 and sum(t.quantity for t in opens_far) == 2
    assert len(closes) == 2 and len(opens_far) == 2  # 每根 Bar 只能成交 1 手 -> 两腿各分两批
    assert len(strat.roll_records) == 1 and strat.roll_records[0].quantity == 2
    assert strat.incomplete_rolls == []
    assert res.equity_snapshots[-1].long_position == 2
    # 第一批新腿对第一批旧腿成交做出反应：只能持有到下一根 Bar 开盘成交 (分批对冲，不等旧腿全部平完)
    assert opens_far[0].event_time <= closes[1].event_time
    record = strat.roll_records[0]
    assert record.leg1_filled_at is not None and record.completed_at is not None
    assert _exposure_trading_days(record, days) >= 1


def test_second_leg_rejection_pauses_retries_then_reports_incomplete_roll(tmp_path: Path) -> None:
    storage = ParquetDataStorage(tmp_path)
    _publish(storage, 4, lambda i: 3000 + 5 * i, lambda i: 3050 + 5 * i)
    limits = ExchangeLimits(
        [
            LimitRule(
                kind=LimitKind.NO_OPEN_FROM,
                scope="SHFE.rb2505",
                value=date(2024, 1, 1),
                source=LimitSource.EXCHANGE,
                effective_from=date(2024, 1, 1),
                evidence_ref="probe",
            )
        ]
    )
    template = _template(tmp_path)
    dataset = load_product_dataset("rb", storage=storage, session_template=template, contract_catalog=None)
    calendar = project_product_calendar(template, "rb", dataset.contracts, window=(DAYS[0], DAYS[-1]))
    from qh_trader.data.session_gate import CalendarSessionGate

    gate = CalendarSessionGate(calendar)
    gw = SimulatedGateway("acc", DAYS[0], session_gate=gate)
    eng = BacktestEngine(
        account_id="acc",
        gateway=gw,
        start_time=dataset.bars[0].bar_start,
        initial_capital=Decimal("400000"),
        session_gate=gate,
        trading_days=DAYS,
        exchange_limits=limits,
    )
    for c in dataset.contracts:
        eng.register_instrument(
            c, InstrumentEconomics(Decimal("10"), Decimal("1"), Decimal("1.5"), Decimal("0.1"), "t")
        )
    strat = RollAwareTrendStrategy(
        strategy_id="s",
        context=eng,
        dataset=dataset,
        fast_window=2,
        slow_window=4,
        order_size=1,
        account_id="acc",
        max_leg_retries=1,
    )
    eng.add_strategy(strat)
    res = eng.run(dataset.bars)
    strat.unfinished_roll(dataset.bars[-1].bar_end)
    # 第一腿平旧仓成交，第二腿开新仓被交易所限额拒绝 -> PAUSED -> 重试 1 次再被拒 -> FAILED，并报告剩余暴露与真实持仓
    assert len(strat.incomplete_rolls) == 1
    item = strat.incomplete_rolls[0]
    assert (item.state, item.leg1_filled_qty, item.leg2_filled_qty, item.remaining_exposure_qty) == (
        RollState.FAILED.value,
        1,
        0,
        1,
    )
    assert item.retries == 1 and "retry limit" in (item.reason or "")
    assert (item.remaining_position_from, item.remaining_position_to) == (0, 0)
    assert strat._roll_task is None  # noqa: SLF001 - 策略不再被卡住
    assert sum(1 for r in res.rejected_intents if r.stage == "exchange-limit") >= 2
    assert strat.roll_records == []


def test_local_rejection_does_not_freeze_the_strategy(tmp_path: Path) -> None:
    storage = ParquetDataStorage(tmp_path)
    prices = [3000, 3010, 3020, 3030, 2990, 2950, 2900, 2950, 3000, 3050]
    for i, day in enumerate(DAYS):
        storage.publish_batch(NEAR, "1d", bars=[_bar(NEAR, day, Decimal(prices[i]), open_interest=1000)])
    template = _template(tmp_path)
    dataset = load_product_dataset("rb", storage=storage, session_template=template, contract_catalog=None)
    calendar = project_product_calendar(template, "rb", dataset.contracts, window=(DAYS[0], DAYS[-1]))
    from qh_trader.data.session_gate import CalendarSessionGate

    gate = CalendarSessionGate(calendar)
    gw = SimulatedGateway("acc", DAYS[0], session_gate=gate)
    eng = BacktestEngine(
        account_id="acc",
        gateway=gw,
        start_time=dataset.bars[0].bar_start,
        initial_capital=Decimal("2000"),
        session_gate=gate,
        trading_days=DAYS,
    )
    eng.register_instrument(NEAR, InstrumentEconomics(Decimal("10"), Decimal("1"), Decimal("5"), Decimal("0.1"), "t"))
    strat = RollAwareTrendStrategy(
        strategy_id="s", context=eng, dataset=dataset, fast_window=1, slow_window=2, order_size=1, account_id="acc"
    )
    eng.add_strategy(strat)
    res = eng.run(dataset.bars)
    # 资金不足每次都被拒，但每个信号日都重新尝试，而不是第一次被拒后永久停摆
    assert len(res.rejected_intents) >= 3
    assert strat._pending == set()  # noqa: SLF001


# ---------------------------------------------------------------------- M6：长假钩子接入


def test_holiday_hook_blocks_the_open_that_would_fill_on_the_eve_and_is_reported(tmp_path: Path) -> None:
    # 9-02..9-13 去掉 9-05 (周四) => 法定假日；9-04 为节前最后交易日
    days = [d for d in (date(2024, 9, 2) + timedelta(days=i) for i in range(12)) if d != date(2024, 9, 5)]
    storage = ParquetDataStorage(tmp_path)
    # 9-03 收盘上穿 -> 开多意图在 9-03 收盘产生、9-04 (节前最后交易日) 开盘成交：这才是钩子要拦的那笔；
    # 9-04 收盘产生的意图要到节后 9-06 才送出，不在窗口内
    prices = [3000, 3010, 3020, 3030, 3040, 3050, 3060, 3070, 3080, 3090, 3100]
    for i, day in enumerate(days):
        storage.publish_batch(NEAR, "1d", bars=[_bar(NEAR, day, Decimal(prices[i]), open_interest=1000)])
    dataset, run = _run(tmp_path, storage, days=days, fast=1, slow=2, holiday_days_before=1)
    assert dataset.calendar is not None and dataset.calendar.holiday_starts() == (date(2024, 9, 5),)
    eve_rejections = [r for r in run.result.rejected_intents if "HolidayRiskHook" in r.reason]
    assert eve_rejections and all(r.at.astimezone(timezone.utc).date() == date(2024, 9, 3) for r in eve_rejections)
    assert run.holiday_rejections == len(eve_rejections)
    eves = {date(2024, 9, 4)}
    assert all(t.trading_day not in eves for t in run.result.trades if t.offset == Offset.OPEN)
    # 节前最后交易日收盘的意图在节后首个交易日 9-06 开盘成交 (策略没有被拒单卡住，也没有持新仓过节)
    assert run.result.trades and run.result.trades[0].trading_day == date(2024, 9, 6)
