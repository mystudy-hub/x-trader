"""策略验证入口：数据不降级、时间切分与预热隔离、事件回测清单完整。"""

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from qh_trader.core.constants import Exchange, PriceType, QualityFlag
from qh_trader.core.objects import Bar, ExecutionReference, InstrumentId, RecordMeta, Settlement
from qh_trader.data.storage import ParquetDataStorage
from scripts.validate_strategy import (
    ROOT,
    inspect_data,
    load_validation_config,
    load_warmup_bars,
    split_samples,
    validate_strategy,
)


@pytest.fixture
def fixture_dataset(tmp_path):
    """纯测试数据，明确人工构造；生产入口不会创建或重写任何行情。"""
    config = load_validation_config(ROOT / "config/strategy_validation.yaml")
    config["symbol"] = "SHFE.rb2410"
    config["strategy"].update(
        fast_period=2,
        slow_period=3,
        trend_period=5,
        atr_period=2,
        breakout_lookback=3,
        structure_lookback=2,
        slope_lookback=2,
    )
    config["data"].update(
        storage_dir="storage", catalog_path="catalog.json", calendar_path="calendar.json", timings_path="timings.json"
    )
    config["research"]["split_date"] = "2024-01-04"
    start = datetime(2024, 1, 2, 1, tzinfo=timezone.utc)
    known = "2023-01-01T00:00:00+00:00"
    catalog = {
        "schema_version": 1,
        "catalog_version": "test",
        "entries": [
            {
                "exchange": "SHFE",
                "symbol": "rb2410",
                "product": "rb",
                "aliases": [],
                "delivery_year": 2024,
                "delivery_month": 10,
                "multiplier": "10",
                "price_tick": "1",
                "listed_on": "2023-10-17",
                "last_trading_day": "2024-10-15",
                "source_id": "fixture",
                "available_at": known,
            }
        ],
    }
    instrument = InstrumentId(Exchange.SHFE, "rb2410")
    calendar = {
        "schema_version": 1,
        "version": "test",
        "source_id": "fixture",
        "available_at": known,
        "coverage_start": "2024-01-02",
        "coverage_end": "2024-01-04",
        "trading_days": [],
        "sessions": [],
    }
    timings = {"schema_version": 1, "bar_timings": {}}
    bars, references, settlements = [], [], []
    for i in range(60):
        opened = start + timedelta(days=i // 20, minutes=30 * (i % 20))
        closed = opened + timedelta(minutes=30)
        meta = RecordMeta(
            event_time=closed,
            available_at=closed,
            ingested_at=closed,
            trading_day=opened.date(),
            source_id="fixture",
            source_version="test",
            ingest_seq=i + 1,
            session_id="day",
        )
        bar = Bar(
            instrument=instrument,
            meta=meta,
            bar_start=opened,
            bar_end=closed,
            open_time=opened,
            interval="30m",
            open=Decimal(3000 + i),
            high=Decimal(3002 + i),
            low=Decimal(2985 + i),
            close=Decimal(3001 + i),
            volume=100,
            turnover=Decimal(100000),
            open_interest=1000,
            includes_auction=False,
        )
        bars.append(bar)
        references.append(
            ExecutionReference(
                instrument=instrument,
                meta=replace(meta, event_time=opened, available_at=opened),
                session_id="day",
                reference_time=opened,
                price_type=PriceType.BAR_OPEN,
                price=bar.open,
                source_record_id=f"fixture-{i}",
                resolution="30m",
                available_volume=100,
            )
        )
        timings["bar_timings"][str(i)] = {
            "trading_day": str(opened.date()),
            "bar_start": opened.isoformat(),
            "bar_end": closed.isoformat(),
            "open_time": opened.isoformat(),
            "available_at": closed.isoformat(),
            "open_available_at": opened.isoformat(),
            "price_types": ["BAR_OPEN"],
            "session_id": "day",
            "includes_auction": False,
            "evidence_ref": "fixture",
        }
        if i % 20 == 0:
            calendar["trading_days"].append(str(opened.date()))
            calendar["sessions"].append(
                {
                    "exchange": "SHFE",
                    "symbol": "rb2410",
                    "session_id": "day",
                    "trading_day": str(opened.date()),
                    "start": opened.isoformat(),
                    "end": (opened + timedelta(hours=10)).isoformat(),
                    "phase": "CONTINUOUS",
                    "permissions": {"submit": True, "cancel": True, "match": True},
                    "available_at": known,
                }
            )
        if i % 20 == 19:
            settlements.append(
                Settlement(
                    instrument=instrument,
                    meta=meta,
                    settlement_price=bar.close,
                    pre_settlement_price=bar.close,
                    published_at=closed,
                    is_final=True,
                )
            )
    for name, value in (("catalog.json", catalog), ("calendar.json", calendar), ("timings.json", timings)):
        (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")
    storage = ParquetDataStorage(tmp_path / "storage")
    storage.publish_batch(instrument, "30m", bars=bars, execution_references=references, settlements=settlements)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return config, path, tuple(bars), storage


def test_missing_30m_never_borrows_daily_or_hourly(tmp_path):
    config = load_validation_config(ROOT / "config/strategy_validation.yaml")
    config["data"]["storage_dir"] = str(tmp_path)
    report, bars = inspect_data(config, symbol="SHFE.rb2701")
    assert not bars and not report["ready"]
    assert any("bars_missing" in issue for issue in report["issues"])


def test_split_uses_earlier_warmup_and_does_not_share_evaluation_bars(fixture_dataset):
    _, _, bars, _ = fixture_dataset
    pieces = split_samples(bars, date(2024, 1, 4), 15)
    warm_train, train = pieces["in_sample"]
    warm_test, test = pieces["out_of_sample"]
    assert len(warm_train) == len(warm_test) == 15
    assert train[-1].meta.trading_day < test[0].meta.trading_day
    assert warm_test[-1].bar_end <= test[0].bar_start
    assert all(bar.meta.available_at <= test[0].bar_start for bar in warm_test)


def test_future_warmup_and_synthetic_history_are_refused(fixture_dataset, tmp_path):
    config, _, bars, storage = fixture_dataset
    with pytest.raises(ValueError, match="available"):
        load_warmup_bars(config, root=tmp_path, known_at=bars[0].bar_start)
    with pytest.raises(ValueError, match="warmup_stale"):
        load_warmup_bars(config, root=tmp_path, known_at=bars[-1].bar_end + timedelta(days=1))
    damaged = [replace(bar, meta=replace(bar.meta, quality_flags=QualityFlag.SYNTHETIC)) for bar in bars]
    storage.save_bars(damaged, bars[0].instrument, "30m")
    with pytest.raises(ValueError, match="data_quality"):
        load_warmup_bars(config, root=tmp_path, known_at=bars[-1].bar_end)


def test_valid_fixture_runs_a_and_b_independently_with_manifests(fixture_dataset, tmp_path):
    _, path, _, storage = fixture_dataset
    before = storage.capture_snapshot().snapshot_id
    report = validate_strategy(path, tmp_path / "runs", root=tmp_path)
    assert report["status"] == "completed_research_only", report["issues"]
    assert set(report["results"]) == {"A_in_sample", "A_out_of_sample", "B_in_sample", "B_out_of_sample"}
    for entry in report["results"].values():
        manifest = json.loads((Path(entry["path"]) / "run_manifest.json").read_text(encoding="utf-8"))
        assert manifest["inputs"]["spec"]["strategy_name"] == "ema_trend"
        assert manifest["inputs"]["data"]["dataset_snapshot"]["snapshot_id"] == before
        assert manifest["extra"]["warmup_bars"] == 15
        assert manifest["extra"]["independent_flat_start"]
    assert storage.capture_snapshot().snapshot_id == before


def test_bad_split_writes_blocked_report_without_backtest(fixture_dataset, tmp_path):
    config, path, _, _ = fixture_dataset
    config["research"]["split_date"] = "2024-01-02"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    report = validate_strategy(path, tmp_path / "runs", root=tmp_path)
    assert report["status"] == "blocked"
    assert not report["results"]
    assert any("sample_split" in issue for issue in report["issues"])
    assert Path(report["artifacts"][0]).is_file()


def test_research_assumptions_do_not_relax_live_warmup(fixture_dataset, tmp_path):
    config, _, bars, storage = fixture_dataset
    config["research"]["assumptions"] = ["allow missing turnover for offline research"]
    flagged = [replace(bar, meta=replace(bar.meta, quality_flags=QualityFlag.TURNOVER_UNAVAILABLE)) for bar in bars]
    storage.save_bars(flagged, bars[0].instrument, "30m")
    with pytest.raises(ValueError, match="data_quality"):
        load_warmup_bars(config, root=tmp_path, known_at=bars[-1].bar_end)


@pytest.mark.parametrize("delayed_index", [50, 59])
def test_warmup_requires_latest_completed_window_without_skipping_unavailable_bars(
    fixture_dataset, tmp_path, delayed_index
):
    config, _, bars, storage = fixture_dataset
    known_at = bars[-1].bar_end
    delayed_at = known_at + timedelta(minutes=1)
    changed = list(bars)
    changed[delayed_index] = replace(
        bars[delayed_index],
        meta=replace(bars[delayed_index].meta, available_at=delayed_at, ingested_at=delayed_at),
    )
    timings_path = tmp_path / "timings.json"
    timings = json.loads(timings_path.read_text())
    timings["bar_timings"][str(delayed_index)]["available_at"] = delayed_at.isoformat()
    timings_path.write_text(json.dumps(timings))
    storage.save_bars(changed, bars[0].instrument, "30m")
    # 快照本身完整且来源声明一致，但截至启动时刻的最近窗口有一根尚不可见。
    report, _ = inspect_data(config, root=tmp_path, for_execution=False)
    assert report["ready"], report["issues"]
    with pytest.raises(ValueError, match="warmup_stale"):
        load_warmup_bars(config, root=tmp_path, known_at=known_at)
    assert len(load_warmup_bars(config, root=tmp_path, known_at=delayed_at)) == 15
