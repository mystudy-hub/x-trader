"""S4-06 多品种样本外与移仓归因的独立期望 (FR-CON-01/03/06/07, FR-VAL-01/02/03, A10)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from qh_trader.core.constants import Exchange, Offset, PositionSide
from qh_trader.core.objects import Bar, InstrumentId, RecordMeta
from qh_trader.data.calendar import project_product_calendar
from qh_trader.data.continuous import AdjustmentMethod
from qh_trader.data.dominant_contract import build_dominant_mappings
from qh_trader.data.product_registry import get_product_spec
from qh_trader.data.session_gate import CalendarSessionGate
from qh_trader.data.storage import ParquetDataStorage
from qh_trader.research.multi_product import (
    MultiProductRun,
    RollRecord,
    load_product_dataset,
    roll_attribution,
    run_product,
    slice_metrics,
)

CD = {"contract": "rb2501", "expiry": "rb2505"}
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
        high=price + Decimal("2"),
        low=price - Decimal("2"),
        close=price,
        volume=volume,
        turnover=Decimal("0"),
        open_interest=open_interest,
        includes_auction=False,
    )


def _session_template(tmp_path: Path, *, has_night: bool = True, night_close: str | None = "23:00:00") -> Path:
    payload = {
        "schema_version": 2,
        "version": "test-sessions-v1",
        "source_id": "test-sessions",
        "available_at": "2024-01-01T00:00:00+00:00",
        "coverage_start": DAYS[0].isoformat(),
        "coverage_end": DAYS[-1].isoformat(),
        "trading_days": [day.isoformat() for day in DAYS],
        "session_profiles": [
            {
                "exchange": "SHFE",
                "symbol": "rb2501",
                "product": "rb",
                "has_night": has_night,
                "night_close": night_close if has_night else None,
                "day_auction_style": "RE_AUCTION",
            }
        ],
        "assumptions": ["test fixture"],
    }
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_product_dataset_excludes_main_continuous_series(tmp_path: Path) -> None:
    """主连/连续序列 (rb0) 不是成交标的，绝不能进入可选合约集合 (FR-CON-01)."""
    storage = ParquetDataStorage(tmp_path)
    main = InstrumentId(Exchange.SHFE, "rb0")
    near = InstrumentId(Exchange.SHFE, "rb2501")
    storage.publish_batch(main, "1d", bars=[_bar(main, day, Decimal("3000"), open_interest=999999) for day in DAYS])
    for day in DAYS:
        storage.publish_batch(near, "1d", bars=[_bar(near, day, Decimal("3000"), open_interest=1000)])

    dataset = load_product_dataset("rb", storage=storage)

    assert dataset.contracts == (near,)
    assert all(str(instrument) != "SHFE.rb0" for instrument in dataset.contracts)
    assert dataset.dominant_on(DAYS[3]) == near


def test_dominant_switch_rolls_the_position_and_reports_spread(tmp_path: Path) -> None:
    """主力切换且有持仓时必须真实移仓：两腿成交、价差归因且不重复计入收益 (A10/FR-CON-06)."""
    storage = ParquetDataStorage(tmp_path)
    near = InstrumentId(Exchange.SHFE, "rb2501")
    far = InstrumentId(Exchange.SHFE, "rb2505")
    # 近月持仓量在前 5 日领先，远月自第 6 日起领先并连续 2 日确认
    for index, day in enumerate(DAYS):
        near_price = Decimal("3000") + Decimal(index)
        far_price = Decimal("3100") + Decimal(index)
        storage.publish_batch(
            near, "1d", bars=[_bar(near, day, near_price, open_interest=5000 if index < 5 else 100)]
        )
        storage.publish_batch(
            far, "1d", bars=[_bar(far, day, far_price, open_interest=100 if index < 5 else 5000)]
        )

    dataset = load_product_dataset("rb", storage=storage)
    calendar = project_product_calendar(
        _session_template(tmp_path), "rb", dataset.contracts, window=(DAYS[0], DAYS[-1])
    )
    run = run_product(
        dataset,
        account_id="acc-test",
        initial_capital=Decimal("400000"),
        fast_window=1,
        slow_window=2,
        order_size=1,
        calendar=calendar,
    )

    assert run.result.rejected_intents == ()
    assert len(run.roll_records) == 1
    record = run.roll_records[0]
    assert record.from_instrument == near
    assert record.to_instrument == far
    assert record.side == PositionSide.LONG
    assert record.quantity == 1
    # 多头移仓到更贵的远月：价差为正成本，且只作为归因项披露
    assert record.spread_cost > 0
    assert record.commission > 0
    assert record.exposure_calendar_days >= 1
    assert run.metrics.rollover_spread_pnl == record.spread_cost
    # 移仓后持仓转移到新主力合约，旧合约持仓清零
    assert run.result.equity_snapshots[-1].long_position == 1
    opened = [trade for trade in run.result.trades if trade.offset == Offset.OPEN]
    closed = [trade for trade in run.result.trades if trade.offset != Offset.OPEN]
    assert {trade.instrument for trade in opened} == {near, far}
    assert {trade.instrument for trade in closed} == {near}


def test_roll_spread_cost_sign_follows_position_side() -> None:
    """多头移仓到更贵合约是成本，空头移仓到更贵合约是收益 (符号口径固定)."""
    common = {
        "product": "rb",
        "from_instrument": InstrumentId(Exchange.SHFE, "rb2501"),
        "to_instrument": InstrumentId(Exchange.SHFE, "rb2505"),
        "quantity": 2,
        "from_price": Decimal("3000"),
        "to_price": Decimal("3010"),
        "multiplier": Decimal("10"),
        "commission": Decimal("6"),
        "start_leg1": None,
        "completed_at": None,
    }
    long_roll = RollRecord(side=PositionSide.LONG, **common)
    short_roll = RollRecord(side=PositionSide.SHORT, **common)
    assert long_roll.spread_cost == Decimal("200")
    assert short_roll.spread_cost == Decimal("-200")


def test_rollover_attribution_sums_per_product() -> None:
    record = RollRecord(
        product="rb",
        from_instrument=InstrumentId(Exchange.SHFE, "rb2501"),
        to_instrument=InstrumentId(Exchange.SHFE, "rb2505"),
        side=PositionSide.LONG,
        quantity=1,
        from_price=Decimal("3000"),
        to_price=Decimal("3010"),
        multiplier=Decimal("10"),
        commission=Decimal("3"),
        start_leg1=None,
        completed_at=None,
    )
    run = MultiProductRun(
        products={
            "rb": type("Run", (), {"roll_records": (record,), "metrics": None, "dataset": None, "result": None})()  # type: ignore[arg-type]
        },
        portfolio_equity=(),
        sample_start=DAYS[0],
        sample_end=DAYS[-1],
        train_end=DAYS[5],
    )
    summary = roll_attribution(run)["rb"]
    assert summary["count"] == 1
    assert summary["spread_cost"] == Decimal("100")
    assert summary["commission"] == Decimal("3")


def test_slice_metrics_split_is_disjoint_and_starts_from_slice_capital() -> None:
    """样本内外切片不重叠，且每段期初资金取该段首日权益，避免跨窗累计盈亏被重复计入 (FR-VAL-01)."""
    start = date(2024, 1, 2)
    equity = tuple((start + timedelta(days=index), Decimal(1000 + index * 10)) for index in range(10))
    run = MultiProductRun(
        products={},
        portfolio_equity=equity,
        sample_start=start,
        sample_end=equity[-1][0],
        train_end=start + timedelta(days=6),
        initial_capital=Decimal("1000"),
    )

    in_sample = slice_metrics(run, start=run.sample_start, end=run.train_end, label="in")
    holdout = slice_metrics(run, start=run.train_end, end=None, label="out")

    assert in_sample.trading_days == 6
    assert holdout.trading_days == 4
    assert in_sample.end < holdout.start
    assert in_sample.initial_capital == Decimal("1000")
    assert holdout.initial_capital == equity[6][1]
    assert holdout.final_equity == equity[-1][1]


def test_project_product_calendar_respects_product_session_shape(tmp_path: Path) -> None:
    """按品种投影日历：无夜盘品种不生成夜盘，夜盘收盘时间来自模板 (FR-CON-04/05)."""
    instruments = (InstrumentId(Exchange.SHFE, "rb2501"), InstrumentId(Exchange.SHFE, "rb2505"))
    calendar = project_product_calendar(
        _session_template(tmp_path), "rb", instruments, window=(DAYS[0], DAYS[-1])
    )
    gate = CalendarSessionGate(calendar)
    assert set(calendar.trading_days) == set(DAYS)
    # 首日没有更早的交易日证据，不生成夜盘
    first_day_ids = {session.session_id for session in calendar.sessions_for_day(instruments[0], DAYS[0])}
    assert first_day_ids
    assert not any(session_id.startswith("night_") for session_id in first_day_ids)
    later_ids = {session.session_id for session in calendar.sessions_for_day(instruments[0], DAYS[3])}
    assert "night_continuous" in later_ids
    assert gate.permissions_at(
        instruments[0], datetime(2024, 9, 5, 13, 30, tzinfo=timezone.utc)
    ) is not None

    no_night_template = _session_template(tmp_path, has_night=False, night_close=None)
    no_night_calendar = project_product_calendar(
        no_night_template, "rb", instruments, window=(DAYS[0], DAYS[-1])
    )
    assert all(
        not session.session_id.startswith("night_")
        for day in DAYS
        for session in no_night_calendar.sessions_for_day(instruments[0], day)
    )


def test_dataset_builder_uses_only_actual_contracts_for_the_resolver(tmp_path: Path) -> None:
    """主力映射只在真实合约之间产生，绝不把派生序列当作主力 (FR-CON-01)."""
    storage = ParquetDataStorage(tmp_path)
    contracts = [InstrumentId(Exchange.SHFE, "rb2501"), InstrumentId(Exchange.SHFE, "rb2505")]
    for day in DAYS:
        storage.publish_batch(contracts[0], "1d", bars=[_bar(contracts[0], day, Decimal("3000"), open_interest=100)])
        storage.publish_batch(contracts[1], "1d", bars=[_bar(contracts[1], day, Decimal("3100"), open_interest=900)])
    dataset = load_product_dataset("rb", storage=storage)
    resolver = build_dominant_mappings(get_product_spec("rb").product_id, dataset.bars, confirm_days=2)
    resolved = resolver.dominant(
        get_product_spec("rb").product_id, datetime(2024, 9, 9, 8, 0, tzinfo=timezone.utc)
    )
    assert resolved.value in contracts
    spec = get_product_spec("rb")
    assert spec.multiplier > 0
    assert AdjustmentMethod.DIFF.value == "DIFF"
