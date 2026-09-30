#!/usr/bin/env python
"""[Scripts 层] EMA 策略的只读数据预检与固定时间切分验证 (S3-04/S5-05, FR-VAL-07).

没有可信 30m 数据时保存缺项报告并退出；不会改写规范数据、读取账户凭证或发出柜台订单。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.analysis.performance import calculate_performance  # noqa: E402
from qh_trader.analysis.visualizer import format_performance_summary  # noqa: E402
from qh_trader.core.constants import MarketPhase, QualityFlag  # noqa: E402
from qh_trader.core.objects import Bar  # noqa: E402
from qh_trader.data.calendar import CHINA_TZ, TradingCalendar  # noqa: E402
from qh_trader.data.contracts import ContractResolver  # noqa: E402
from qh_trader.data.downloader import load_import_metadata  # noqa: E402
from qh_trader.data.storage import ParquetDataStorage  # noqa: E402
from qh_trader.data.validation import file_reference, report_json, validate_dataset  # noqa: E402
from qh_trader.research.backtest_assembly import (  # noqa: E402
    BacktestSpec,
    assemble,
    build_manifest,
    run_assembled,
    write_run_artifacts,
)
from qh_trader.strategy.examples.ema_trend import EmaTrendParameters, EmaTrendStrategy  # noqa: E402

DECIMAL_PARAMETERS = {
    "pullback_tolerance_atr",
    "flat_threshold_atr",
    "near_ema_atr",
    "stop_atr",
    "breakeven_atr",
    "trailing_atr",
    "risk_fraction",
}


def load_validation_config(path: Path) -> dict:
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise ValueError("strategy validation requires schema_version 1")
    for name in ("strategy", "data", "research"):
        if not isinstance(config.get(name), dict):
            raise ValueError(f"configuration requires {name}")
    parameters_from_config(config, "A")
    if config["strategy"].get("modes") != ["A", "B"]:
        raise ValueError("the first validation compares modes A and B independently")
    return config


def parameters_from_config(config: dict, mode: str) -> EmaTrendParameters:
    values = {key: value for key, value in config["strategy"].items() if key != "modes"}
    for key in DECIMAL_PARAMETERS:
        if key in values:
            values[key] = Decimal(str(values[key]))
    params = EmaTrendParameters(mode=mode, **values)
    if params.entry_interval != "30m" or params.max_lots != 1:
        raise ValueError("the authorized first experiment requires 30m bars and maximum one lot")
    return params


def inspect_data(
    config: dict, *, root: Path = ROOT, symbol: str | None = None, for_execution: bool = True
) -> tuple[dict, tuple[Bar, ...]]:
    """检查固定快照，返回原始规范 Bar；任何阻断必须由调用方尊重。"""
    params = parameters_from_config(config, "A")
    data = config["data"]
    symbol = symbol or config.get("symbol")
    report = {"symbol": symbol, "interval": params.entry_interval, "issues": [], "ready": False}
    issues = report["issues"]
    storage = ParquetDataStorage(root / data["storage_dir"])
    snapshot = storage.capture_snapshot(data.get("snapshot_id"))
    report["snapshot_id"] = snapshot.snapshot_id
    report["available_30m_datasets"] = sorted(
        key for key in snapshot.datasets if key.startswith("bar/") and key.endswith("/30m")
    )
    if not symbol:
        issues.append("symbol_required: select an actual active contract using the broker preflight")
        return report, ()
    catalog_path = root / data["catalog_path"]
    catalog = ContractResolver.from_file(catalog_path)
    instrument, _, _ = catalog.resolve(symbol)
    bars = tuple(storage.read_bars(instrument, params.entry_interval, snapshot=snapshot))
    report["bar_count"] = len(bars)
    report["required_warmup_bars"] = params.warmup_bars
    report["input_refs"] = {"catalog": file_reference(catalog_path, root), "manifest": snapshot.snapshot_id}
    if not bars:
        issues.append("bars_missing: no canonical 30m bars for the selected actual contract")
    elif len(bars) < params.warmup_bars:
        issues.append("warmup_missing: insufficient completed bars for EMA200 and trend filters")
    if bars:
        report["start_day"] = str(bars[0].meta.trading_day)
        report["end_day"] = str(bars[-1].meta.trading_day)
        report["quality_flag_counts"] = dict(Counter(str(int(bar.meta.quality_flags)) for bar in bars))
        if any(bar.meta.quality_flags != QualityFlag.OK for bar in bars):
            issues.append("data_quality: synthetic, assumed, incomplete or degraded bars are not accepted")
    missing = [name for name in ("calendar_path", "timings_path") if not data.get(name)]
    issues.extend(f"{name}_required: supply versioned contract/session and source timing evidence" for name in missing)
    if bars and not missing:
        calendar_path, timings_path = (root / data[name] for name in ("calendar_path", "timings_path"))
        calendar = TradingCalendar.from_file(calendar_path)
        timings, _ = load_import_metadata(timings_path)
        quality = validate_dataset(
            storage,
            instrument,
            params.entry_interval,
            calendar=calendar,
            catalog=catalog,
            timings=timings,
            mode="research",
            snapshot_id=snapshot.snapshot_id,
        )
        report["quality_report"] = quality.as_dict()
        report["input_refs"].update(
            calendar=file_reference(calendar_path, root), timings=file_reference(timings_path, root)
        )
        warmup_ignored = {"execution_price_missing_or_ambiguous", "execution_requirements_missing"}
        errors = sorted(
            {
                issue.code
                for issue in quality.issues
                if (issue.severity == "error" or issue.code == "synthetic_or_assumed")
                and (for_execution or issue.code not in warmup_ignored)
            }
        )
        issues.extend(f"data_validation: {code}" for code in errors)
        settlement_days = {
            r.meta.trading_day for r in storage.read_settlements(instrument, snapshot=snapshot) if r.is_final
        }
        if for_execution and {bar.meta.trading_day for bar in bars} - settlement_days:
            issues.append("final_settlement_missing: no substitution of closes for final settlements")
    report["ready"] = not issues
    return report, bars


def load_warmup_bars(
    config: dict,
    *,
    root: Path = ROOT,
    symbol: str | None = None,
    known_at: datetime,
) -> tuple[Bar, ...]:
    """仿真入口复用：仅返回此刻可见且通过同一严格预检的历史预热 Bar。"""
    if known_at.tzinfo is None:
        raise ValueError("known_at must include timezone")
    report, bars = inspect_data(config, root=root, symbol=symbol, for_execution=False)
    if not report["ready"]:
        raise ValueError("; ".join(report["issues"]))
    count = parameters_from_config(config, "A").warmup_bars
    completed = tuple(bar for bar in bars if bar.bar_end <= known_at)
    if len(completed) < count:
        raise ValueError("warmup_missing: too few bars were available at the requested start time")
    available = completed[-count:]
    if any(bar.meta.available_at > known_at for bar in available):
        raise ValueError("warmup_stale: the latest completed window contains unavailable bars")
    calendar = TradingCalendar.from_file(root / config["data"]["calendar_path"])
    if calendar.coverage_end is None or known_at.astimezone(CHINA_TZ).date() > calendar.coverage_end:
        raise ValueError("warmup_stale: calendar cannot verify continuity through the requested start time")
    last_end = available[-1].bar_end
    for day in sorted(calendar.trading_days):
        if day < available[-1].meta.trading_day:
            continue
        for session in calendar.sessions_for_day(available[-1].instrument, day, known_at=known_at):
            if session.phase != MarketPhase.CONTINUOUS or not session.permissions.match:
                continue
            missing_start, missing_end = max(last_end, session.start), min(known_at, session.end)
            if missing_end > missing_start and (
                missing_end - missing_start >= timedelta(minutes=30) or session.end <= known_at
            ):
                raise ValueError("warmup_stale: completed trading intervals are missing before startup")
    return available


def split_samples(bars: tuple[Bar, ...], split_day: date, warmup_bars: int) -> dict:
    """按交易日预先切分；两段独立空仓，预热不计绩效且全部早于其评分首根 Bar。"""
    first_test = next((i for i, bar in enumerate(bars) if bar.meta.trading_day >= split_day), len(bars))
    if first_test <= warmup_bars or len(bars) - first_test < 2:
        raise ValueError("sample_split: require training after warmup and at least two out-of-sample bars")
    partitions = {
        "in_sample": (bars[:warmup_bars], bars[warmup_bars:first_test]),
        "out_of_sample": (bars[first_test - warmup_bars : first_test], bars[first_test:]),
    }
    for warmup, measured in partitions.values():
        if any(bar.bar_end > measured[0].bar_start or bar.meta.available_at > measured[0].bar_start for bar in warmup):
            raise ValueError("sample_split: warmup contains information unavailable before evaluation starts")
    return partitions


def run_partition(
    config: dict, mode: str, warmup, measured, *, root: Path, symbol: str, snapshot_id: str, output: Path
):
    data, research = config["data"], config["research"]
    params = parameters_from_config(config, mode)
    spec = BacktestSpec(
        symbol=symbol,
        interval=params.entry_interval,
        storage_dir=data["storage_dir"],
        catalog_path=data["catalog_path"],
        calendar_path=data["calendar_path"],
        snapshot_id=snapshot_id,
        initial_capital=Decimal(str(research["initial_capital"])),
        commission_per_lot=Decimal(str(research["commission_per_lot"])),
        margin_ratio=Decimal(str(research["margin_ratio"])),
        slippage_ticks=int(research["slippage_ticks"]),
        participation_rate=Decimal(str(research["participation_rate"])),
        strict_execution_reference=True,
        strict_data_quality=True,
        use_official_settlement=True,
        annual_trading_days=int(research["annual_trading_days"]),
    )
    assembled = assemble(spec, root=root, bars=measured)
    engine = assembled.engine
    # 复用装配的账户内核、撮合和快照；默认示例策略未开始运行，替换为用户指定的策略。
    engine.strategies.clear()
    economics = engine.economics(assembled.instrument)
    strategy = EmaTrendStrategy(
        f"ema-{mode}",
        engine,
        assembled.instrument,
        parameters=params,
        multiplier=economics.multiplier,
        price_tick=economics.price_tick,
        equity_provider=lambda: (
            engine.ledger.get_funds_state(
                current_prices=dict(engine._mark_prices),  # noqa: SLF001 - 装配层读取已发布账本视图
                margin_rates={assembled.instrument: spec.margin_ratio},
            ).total_equity
        ),
    )
    engine.add_strategy(strategy)
    strategy.warmup(warmup)
    result = run_assembled(assembled)
    metrics = calculate_performance(result, annual_trading_days=spec.annual_trading_days, rf_rate=spec.risk_free_rate)
    # 清单中的策略名称必须与实际运行一致；参数由 extra 完整存档。
    assembled = replace(assembled, spec=replace(spec, strategy_name="ema_trend"))
    manifest = build_manifest(
        assembled,
        result,
        metrics,
        root=root,
        extra={
            "strategy_parameters": asdict(params),
            "warmup_bars": len(warmup),
            "warmup_start": warmup[0].bar_start,
            "warmup_end": warmup[-1].bar_end,
            "split_date": research["split_date"],
            "independent_flat_start": True,
            "stop_execution": "bar observation then next executable price; no guaranteed stop fill",
            "cost_status": "research assumptions; counter rates not verified",
        },
    )
    # 参数与预热也参与实验身份，避免不同策略共用原装配的相同 run_id。
    digest = hashlib.sha256(
        report_json({"inputs": manifest["inputs"], "extra": manifest["extra"]}).encode()
    ).hexdigest()
    manifest["input_digest"], manifest["run_id"] = digest, digest[:16]
    path = write_run_artifacts(output, manifest, result, format_performance_summary(metrics))
    (path / "decisions.json").write_text(report_json(strategy.decisions), encoding="utf-8")
    return {"path": str(path), "summary": manifest["outputs"]["summary"], "canonical_hashes": result.canonical_hashes()}


def validate_strategy(config_path: Path, output_dir: Path, *, root: Path = ROOT, symbol: str | None = None) -> dict:
    config = load_validation_config(config_path)
    report = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "blocked",
        "evidence_kind": "local_research",
        "config": config,
        "issues": [],
        "results": {},
        "artifacts": [],
    }
    run_dir = output_dir / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    run_dir.mkdir(parents=True, exist_ok=False)
    try:
        data_report, bars = inspect_data(config, root=root, symbol=symbol)
        report["data"] = data_report
        report["issues"].extend(data_report["issues"])
        split_day = config["research"].get("split_date")
        if not split_day:
            report["issues"].append("split_date_required: predeclare the time boundary before evaluation")
        if not report["issues"]:
            partitions = split_samples(
                bars, date.fromisoformat(str(split_day)), parameters_from_config(config, "A").warmup_bars
            )
            for mode in config["strategy"]["modes"]:
                for name, (warmup, measured) in partitions.items():
                    key = f"{mode}_{name}"
                    report["results"][key] = run_partition(
                        config,
                        mode,
                        warmup,
                        measured,
                        root=root,
                        symbol=data_report["symbol"],
                        snapshot_id=data_report["snapshot_id"],
                        output=run_dir / key,
                    )
            report["status"] = "completed_research_only"
    except (OSError, ValueError, TypeError, KeyError, LookupError) as exc:
        report["issues"].append(f"{type(exc).__name__}: {exc}")
    summary_path = run_dir / "validation_summary.json"
    report["artifacts"].append(str(summary_path))
    summary_path.write_text(report_json(report), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config/strategy_validation.yaml")
    parser.add_argument("--symbol", help="Actual contract confirmed by the broker preflight")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "runs/strategy_validation")
    args = parser.parse_args(argv)
    report = validate_strategy(args.config, args.output_dir, symbol=args.symbol)
    print(json.dumps({key: report[key] for key in ("status", "issues", "artifacts")}, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "completed_research_only" else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
