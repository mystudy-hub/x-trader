#!/usr/bin/env python
"""[Scripts 层] 显式假设下的通达信螺纹钢 30m EMA 研究；不放宽实盘预热门禁。

读取 download_data.py 的带哈希原始归档，将交易日标签的夜盘重排到前一交易日。
仅使用最近连续、逐分钟完整且与日线量价交叉核对通过的区间；不填补缺失分钟。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.core.constants import MarketPhase, PriceType  # noqa: E402
from qh_trader.data.calendar import CHINA_TZ, TradingCalendar  # noqa: E402
from qh_trader.data.contracts import ContractResolver  # noqa: E402
from qh_trader.data.execution_reference import derive_execution_references  # noqa: E402
from qh_trader.data.schemas import (  # noqa: E402
    BarTiming,
    convert_minute_records_to_bars,
    parse_time,
    validate_ohlc_records,
)
from qh_trader.data.session_templates import SessionProfile, build_sessions  # noqa: E402
from qh_trader.data.storage import ParquetDataStorage  # noqa: E402
from qh_trader.data.tdx_aggregate import aggregate_tdx_minutes  # noqa: E402
from qh_trader.data.validation import report_json, validate_dataset  # noqa: E402
from scripts.validate_strategy import (  # noqa: E402
    load_validation_config,
    parameters_from_config,
    run_partition,
    split_samples,
)

ASSUMPTIONS = (
    "成交额原始缺失，保留 TURNOVER_UNAVAILABLE；不用于信号和成交计算。",
    "分钟时间为结束标签；21:00 后的来源日期解释为交易日，夜盘重排到前一交易日自然日期。",
    "交易日取同源日线；螺纹钢常规时段及节前无夜盘规则使用研究模板，未完成官方历史日历核验。",
    "日线与分钟线为同源交叉核对，并非独立数据源认证；目录参数采用已归档柜台查询。",
    "完整 30m 桶在各连续时段起点对齐；10:00—10:15 的短尾不参与信号及撮合，不跨休市拼接。",
    "假设 Bar 闭合时可见，下一 Bar 开盘价在开盘时可用；瞬时可成交量未知，使用 Bar 成交量参与率。",
    "每日最后收盘价模拟结算，不将通达信均价代理标为官方结算价。",
    "手续费、保证金、滑点为研究配置；缺少历史涨跌停价，不能完整模拟封板与无法退出风险。",
    "Bar 止损在闭合后确认、下一可执行价退出；期末持仓按收盘估值，不强行虚构平仓。",
    "分钟聚合极值可能窄于日线极值；保留并披露差异，不用日线极值回填分钟，ATR/止损可能受影响。",
)
MINUTE = timedelta(minutes=1)


def save_json(path: Path, value) -> None:
    path.write_text(report_json(value), encoding="utf-8")


def load_raw(path: Path, symbol: str, interval: str) -> dict:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != path.stem:
        raise ValueError(f"raw archive hash mismatch: {path}")
    payload = json.loads(raw)
    if (
        (payload.get("source_id"), payload.get("instrument"), payload.get("interval"))
        != (
            "tdx_exhq",
            symbol,
            interval,
        )
        or payload.get("error")
        or payload.get("status") != "raw_observation"
    ):
        raise ValueError("raw source, contract, interval or fetch status does not match")
    if not payload.get("records"):
        raise ValueError("raw archive contains no observations")
    validate_ohlc_records(
        payload["records"],
        "date" if interval == "1d" else "datetime",
        source_timezone="Asia/Shanghai",
        require_turnover=False,
    )
    return payload


def normalize_minutes(records: list[dict], trading_days: list[date]) -> tuple[list[dict], int]:
    """来源交易日保持不变，只重排夜盘自然时刻；首日无前序证据则不使用。"""
    previous = dict(zip(trading_days[1:], trading_days[:-1], strict=True))
    normalized = []
    skipped = 0
    for record in records:
        stamp = parse_time(record["datetime"]).astimezone(CHINA_TZ)
        day = stamp.date()
        if day not in trading_days:
            raise ValueError("minute source label is outside daily trading dates")
        if day == trading_days[0]:
            skipped += 1
            continue
        if stamp.time() >= time(21):
            stamp = datetime.combine(previous[day], stamp.time(), CHINA_TZ)
        normalized.append(
            {
                **record,
                "source_datetime": record["datetime"],
                "trading_day": day,
                "datetime": stamp,
                "bar_start": stamp - MINUTE,
                "bar_end": stamp,
            }
        )
    normalized.sort(key=lambda row: row["datetime"])
    return normalized, skipped


def inspect_days(records: list[dict], daily: list[dict], calendar: TradingCalendar, instrument) -> list[dict]:
    grouped = defaultdict(list)
    for row in records:
        grouped[row["trading_day"]].append(row)
    reports = []
    for source in daily[1:]:
        day = date.fromisoformat(source["date"])
        rows = grouped[day]
        expected = set()
        for session in calendar.sessions_for_day(instrument, day):
            if session.phase != MarketPhase.CONTINUOUS or not session.permissions.match:
                continue
            stamp = session.start + MINUTE
            while stamp <= session.end:
                expected.add(stamp)
                stamp += MINUTE
        actual = {row["datetime"] for row in rows}
        checks = {"volume": False, "open": False, "high": False, "low": False, "close": False, "open_interest": False}
        extrema_differences = {}
        within_daily_range = False
        if rows:
            calculated = {
                "volume": sum(row["volume"] for row in rows),
                "open": rows[0]["open"],
                "close": rows[-1]["close"],
                "high": max(Decimal(row["high"]) for row in rows),
                "low": min(Decimal(row["low"]) for row in rows),
                "open_interest": rows[-1]["open_interest"],
            }
            checks = {key: Decimal(str(value)) == Decimal(str(source[key])) for key, value in calculated.items()}
            extrema_differences = {
                key: str(Decimal(str(calculated[key])) - Decimal(str(source[key]))) for key in ("high", "low")
            }
            within_daily_range = calculated["high"] <= Decimal(source["high"]) and calculated["low"] >= Decimal(
                source["low"]
            )
        reports.append(
            {
                "trading_day": str(day),
                "minute_count": len(rows),
                "expected_minutes": len(expected),
                "missing_minutes": len(expected - actual),
                "outside_session_minutes": len(actual - expected),
                "duplicate_minutes": len(rows) - len(actual),
                "daily_checks": checks,
                "minute_minus_daily_extrema": extrema_differences,
                "complete": actual == expected
                and len(rows) == len(actual)
                and within_daily_range
                and all(checks[key] for key in ("volume", "open", "close", "open_interest")),
            }
        )
    return reports


def latest_complete_days(reports: list[dict]) -> list[date]:
    selected = []
    for report in reversed(reports):
        if not report["complete"]:
            break
        selected.append(date.fromisoformat(report["trading_day"]))
    if not selected:
        raise ValueError("latest trading day failed minute coverage or daily cross-check")
    return list(reversed(selected))


def check_published_quality(quality: dict, calendar: TradingCalendar, instrument) -> None:
    """只豁免完整 30m 聚合必然遗漏的 15m 短尾，其他覆盖/时间/价格错误仍阻断。"""
    for issue in quality["issues"]:
        if issue["severity"] == "error" and issue["code"] != "coverage_gaps":
            raise ValueError(f"research dataset validation failed: {issue['code']}")
    for gap in quality["summary"]["coverage"]["gaps"]:
        day = date.fromisoformat(str(gap["trading_day"]))
        sessions = calendar.sessions_for_day(instrument, day)
        matches = [session for session in sessions if session.session_id == gap["session_id"]]
        if len(matches) != 1:
            raise ValueError("ambiguous gap session")
        session = matches[0]
        duration = timedelta(minutes=30)
        expected_start = session.start + ((session.end - session.start) // duration) * duration
        if not (
            gap["kind"] == "missing"
            and parse_time(gap["start"]) == expected_start
            and parse_time(gap["end"]) == session.end
            and timedelta() < session.end - expected_start < duration
        ):
            raise ValueError("unexplained coverage gap remains in research data")


def prepare(args, run_dir: Path) -> tuple[dict, tuple, dict]:
    if not args.symbol.startswith("SHFE.rb"):
        raise ValueError("this research timing profile only supports SHFE rb actual contracts")
    minute = load_raw(args.minute_raw, args.symbol, "1m")
    daily = load_raw(args.daily_raw, args.symbol, "1d")
    catalog = ContractResolver.from_file(args.catalog)
    instrument, _, _ = catalog.resolve(args.symbol)
    days = [date.fromisoformat(row["date"]) for row in daily["records"]]
    config = load_validation_config(args.config)
    config["symbol"] = args.symbol
    params = parameters_from_config(config, "A")
    # 模型起点是回测时钟假设，不冒充公告发布时间；采集时间单独记录。
    model_available = datetime.combine(days[0] - timedelta(days=7), time(), timezone.utc)
    version = "tdx-rb-research-" + args.minute_raw.stem[:12]
    source = "research_assumption:rb_schedule_and_tdx_daily_dates"
    profile = SessionProfile(instrument, "rb", True, time(23), "RE_AUCTION", source, version, model_available)
    calendar = TradingCalendar(
        build_sessions((profile,), tuple(days)),
        trading_days=days,
        coverage_start=days[0],
        coverage_end=days[-1],
        version=version,
        source_id=source,
        available_at=model_available,
    )
    normalized, skipped = normalize_minutes(minute["records"], days)
    day_reports = inspect_days(normalized, daily["records"], calendar, instrument)
    save_json(run_dir / "daily_quality.json", day_reports)
    selected_days = latest_complete_days(day_reports)
    selected = [row for row in normalized if row["trading_day"] in selected_days]
    rows = aggregate_tdx_minutes(selected, instrument=instrument, calendar=calendar, period=30)
    timings = {}
    for row in rows:
        end, start = row["bar_end"], row["bar_start"]
        timings[end.isoformat()] = BarTiming(
            trading_day=row["trading_day"],
            bar_start=start,
            bar_end=end,
            open_time=start,
            available_at=end,
            session_id=row["session_id"],
            includes_auction=False,
            open_available_at=start,
            price_types=(PriceType.BAR_OPEN,),
            evidence_ref=str(run_dir / "daily_quality.json"),
            time_assumption="; ".join(ASSUMPTIONS[1:6]),
        )
    bars = tuple(
        convert_minute_records_to_bars(
            rows,
            instrument,
            "30m",
            timings=timings,
            calendar=calendar,
            source_id="tdx_exhq",
            source_version=args.minute_raw.stem,
            source_timezone="Asia/Shanghai",
            ingested_at=parse_time(minute["ingested_at"]),
            require_turnover=False,
        )
    )
    if len(bars) <= params.warmup_bars + 20:
        raise ValueError(f"only {len(bars)} complete bars; insufficient after {params.warmup_bars} warmup bars")
    measured_days = sorted({bar.meta.trading_day for bar in bars[params.warmup_bars :]})
    if len(measured_days) < 4:
        raise ValueError("need at least four evaluation trading days after warmup")
    split_day = measured_days[min(len(measured_days) - 1, int(len(measured_days) * 0.7))]
    config["research"]["split_date"] = str(split_day)
    # 用日期和数据质量定样本，先落盘再运行任何策略；之后不按收益调整。
    calendar_path = run_dir / "calendar.json"
    save_json(
        calendar_path,
        {
            "schema_version": 2,
            "version": version,
            "source_id": source,
            "available_at": model_available,
            "coverage_start": days[0],
            "coverage_end": days[-1],
            "trading_days": days,
            "assumptions": ASSUMPTIONS,
            "session_profiles": [
                {
                    "exchange": "SHFE",
                    "symbol": instrument.symbol,
                    "product": "rb",
                    "has_night": True,
                    "night_close": "23:00",
                    "day_auction_style": "RE_AUCTION",
                }
            ],
        },
    )
    timing_path = run_dir / "timings.json"
    save_json(timing_path, {"schema_version": 1, "bar_timings": {key: asdict(v) for key, v in timings.items()}})
    refs = {
        "minute_raw": str(args.minute_raw),
        "minute_sha256": args.minute_raw.stem,
        "daily_raw": str(args.daily_raw),
        "daily_sha256": args.daily_raw.stem,
        "catalog": str(args.catalog),
        "catalog_sha256": hashlib.sha256(args.catalog.read_bytes()).hexdigest(),
    }
    storage = ParquetDataStorage(run_dir / "dataset")
    snapshot = storage.publish_batch(
        instrument,
        "30m",
        bars=bars,
        execution_references=derive_execution_references(bars, {value.bar_start: value for value in timings.values()}),
        merge_existing=False,
        provenance={"research_only": True, "assumptions": ASSUMPTIONS, "input_refs": refs},
    )
    quality = json.loads(
        report_json(
            validate_dataset(
                storage,
                instrument,
                "30m",
                catalog=catalog,
                calendar=calendar,
                timings=timings,
                snapshot_id=snapshot.snapshot_id,
                mode="research",
            ).as_dict()
        )
    )
    save_json(run_dir / "data_validation.json", quality)
    check_published_quality(quality, calendar, instrument)
    config["data"].update(
        storage_dir=str(storage.root_dir),
        snapshot_id=snapshot.snapshot_id,
        catalog_path=str(args.catalog.resolve()),
        calendar_path=str(calendar_path),
        timings_path=str(timing_path),
    )
    config["research"]["assumptions"] = list(ASSUMPTIONS)
    (run_dir / "experiment.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    with (run_dir / "bars_30m.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    info = {
        "input_refs": refs,
        "raw_minute_count": len(minute["records"]),
        "raw_daily_count": len(days),
        "raw_start": days[0],
        "raw_end": days[-1],
        "first_day_excluded_minutes": skipped,
        "selection": "latest contiguous complete-minute days passing daily open/close/volume/OI and price bounds",
        "selected_start": selected_days[0],
        "selected_end": selected_days[-1],
        "selected_trading_days": len(selected_days),
        "selected_minutes": len(selected),
        "bars_30m": len(bars),
        "warmup_bars": params.warmup_bars,
        "split_date": split_day,
        "split_rule": "first 70% of post-warmup trading days in-sample; remainder held out",
        "snapshot_id": snapshot.snapshot_id,
        "assumptions": ASSUMPTIONS,
        "missing_turnover": True,
        "settlement_mode": "daily_last_close",
        "costs": {
            key: config["research"][key]
            for key in ("initial_capital", "commission_per_lot", "margin_ratio", "slippage_ticks", "participation_rate")
        },
        "max_lots": params.max_lots,
        "selected_daily_extrema_differences": [
            row
            for row in day_reports
            if date.fromisoformat(row["trading_day"]) in selected_days
            and not (row["daily_checks"]["high"] and row["daily_checks"]["low"])
        ],
    }
    save_json(run_dir / "experiment_plan.json", info)
    return config, bars, info


def write_report(run_dir: Path, report: dict) -> None:
    data = report["data"]
    lines = [
        "# 通达信 30 分钟 EMA 研究回测",
        "",
        f"合约：{report['symbol']}；状态：{report['status']}。",
        "",
        f"原始分钟线 {data['raw_minute_count']:,} 根；采用 {data['selected_start']} 至 {data['selected_end']}，"
        f"{data['selected_trading_days']} 个交易日，{data['bars_30m']} 根完整 30m Bar。",
        f"每段预热 {data['warmup_bars']} 根；样本外自 {data['split_date']} 开始，两段各自空仓起步。",
        "",
        "| 模式 | 区间 | 净盈亏（元） | 收益率 | 最大回撤 | 成交笔数 | 平仓笔数 | 胜率 | 期末净持仓 |",
        "| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for key, value in report["results"].items():
        s = value["summary"]
        win_rate = f"{Decimal(s['win_rate_pct']):.2f}%" if s["closed_trades"] else "—"
        lines.append(
            f"| {key} | {s['sample_start']}—{s['sample_end']} | {Decimal(s['total_pnl']):,.2f} | "
            f"{Decimal(s['total_return_pct']):.4f}% | {Decimal(s['max_drawdown_pct']):.4f}% | "
            f"{s['total_trades']} | {s['closed_trades']} | {win_rate} | {value['open_position']} |"
        )
    costs = data["costs"]
    difference_count = len(data["selected_daily_extrema_differences"])
    lines += [
        "",
        f"初始资金 {Decimal(costs['initial_capital']):,.0f} 元、最多 {data['max_lots']} 手；"
        f"手续费每次成交每手 {costs['commission_per_lot']} 元、滑点 {costs['slippage_ticks']} 跳、"
        f"保证金 {Decimal(costs['margin_ratio']) * 100:g}%、参与率 {Decimal(costs['participation_rate']) * 100:g}%。",
        "实际配置见 experiment.yaml；收益包含期末未平仓估值。短样本及小交易数不足以判断策略长期有效性。",
        f"所选区间有 {difference_count} 天的分钟极值与日线不完全一致，逐日差额见 experiment_plan.json。",
        "",
        "## 数据与成交假设",
        "",
    ]
    lines += [f"- {value}" for value in ASSUMPTIONS]
    lines += [
        "",
        "## 可复核工件",
        "",
        "- experiment_plan.json：运行前固定的区间、样本切分、哈希及假设。",
        "- daily_quality.json：逐日完整性和同源量价交叉核对，早期失败数据保留原始归档。",
        "- data_validation.json：包含短尾覆盖缺口等原始校验结果；本研究只豁免已声明的短尾。",
        "- bars_30m.csv：实际用于计算的 30 分钟行情。",
        "- 各结果目录：run_manifest.json、权益曲线、成交与策略决策记录。",
        "",
    ]
    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research", action="store_true", help="explicitly accept the documented research assumptions")
    parser.add_argument("--minute-raw", type=Path, required=True)
    parser.add_argument("--daily-raw", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--symbol", default="SHFE.rb2701")
    parser.add_argument("--config", type=Path, default=ROOT / "config/strategy_validation.yaml")
    parser.add_argument("--output", type=Path, default=ROOT / "runs/tdx_ema_research")
    args = parser.parse_args(argv)
    if not args.research:
        parser.error("--research is required; these outputs do not qualify for strict validation or live warmup")
    run_dir = args.output.resolve() / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    run_dir.mkdir(parents=True, exist_ok=False)
    report = {"status": "blocked", "symbol": args.symbol, "results": {}, "issues": []}
    try:
        config, bars, info = prepare(args, run_dir)
        report["data"] = info
        code_dir = run_dir / "code"
        code_dir.mkdir()
        for source in (Path(__file__), ROOT / "scripts/validate_strategy.py", ROOT / "qh_trader/data/tdx_exhq.py"):
            (code_dir / source.name).write_bytes(source.read_bytes())
        partitions = split_samples(bars, date.fromisoformat(config["research"]["split_date"]), info["warmup_bars"])
        for mode in ("A", "B"):
            for name, (warmup, measured) in partitions.items():
                key = f"{mode}_{name}"
                report["results"][key] = run_partition(
                    config,
                    mode,
                    warmup,
                    measured,
                    root=ROOT,
                    symbol=args.symbol,
                    snapshot_id=info["snapshot_id"],
                    output=run_dir / key,
                    research_assumptions=ASSUMPTIONS,
                    use_official_settlement=False,
                )
        report["status"] = "completed_research_only"
        write_report(run_dir, report)
    except (OSError, ValueError, TypeError, KeyError, LookupError) as exc:
        report["issues"].append(f"{type(exc).__name__}: {exc}")
    save_json(run_dir / "summary.json", report)
    print(report_json({"status": report["status"], "issues": report["issues"], "output": str(run_dir)}))
    return 0 if report["status"] == "completed_research_only" else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
