#!/usr/bin/env python
"""[脚本工具] 通达信研究行情有界探测与 S0 证据归档 (S1-12, FR-DATA-10, GAP-TDX-01).

默认只采样两页历史；截断、缺少日历及无法归属的逐笔均不作为核验通过。
证据仅写入 runs/s0，原始帧来自公开行情且不含任何账户或客户端私有配置。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.core.constants import Exchange, MarketPhase  # noqa: E402
from qh_trader.core.objects import InstrumentId  # noqa: E402
from qh_trader.data.calendar import TradingCalendar  # noqa: E402
from qh_trader.data.tdx_exhq import TDX_MARKET_MAP, TdxExHqClient, load_tdx_servers  # noqa: E402

CHINA_TZ = ZoneInfo("Asia/Shanghai")
CATEGORIES = {"5m": 0, "15m": 1, "30m": 2, "1h": 3, "1d": 4, "1w": 5, "1M": 6, "1m": 7}


def _json_default(value: object) -> str:
    if isinstance(value, (datetime, date, Decimal)) or hasattr(value, "isoformat"):
        return value.isoformat() if hasattr(value, "isoformat") else str(value)
    raise TypeError(f"unsupported evidence value: {type(value).__name__}")


def _error_chain(error: BaseException) -> list[dict[str, str]]:
    result = []
    visited: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in visited and len(result) < 8:
        visited.add(id(current))
        result.append({"error_type": type(current).__name__, "error": str(current)})
        current = current.__cause__
    return result


def evidence_target(root: Path, output: str | None) -> Path:
    """先检查真实目标路径，再建立目录或执行网络探测。"""
    root = root.resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = (root / (output or f"runs/s0/tdx_runtime_evidence_{stamp}.json")).resolve()
    if not target.is_relative_to(root / "runs" / "s0") or target.suffix.lower() != ".json":
        raise ValueError("TDX 证据须写入项目 runs/s0 内的 JSON 文件")
    return target


def parse_symbol(raw: str) -> tuple[int, str]:
    try:
        exchange, code = raw.split(".", 1)
        market = next(key for key, value in TDX_MARKET_MAP.items() if value == exchange)
    except (ValueError, StopIteration) as exc:
        raise ValueError("标的须为已登记市场及大写代码，例如 SHFE.RB2701") from exc
    if not code.isascii() or not code.isalnum() or code != code.upper() or len(code) > 9:
        raise ValueError("TDX 代码须为 1 至 9 位大写 ASCII 字母数字")
    return market, code


def collect_pages(fetch: Callable[[int, int], Sequence[Mapping[str, Any]]], *, max_pages: int, count: int):
    """仅在收到空页时判定到底；短页也继续请求，重复页显式拒绝。"""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    pages = []
    start = 0
    for _ in range(max_pages):
        batch = [dict(row) for row in fetch(start, count)]
        pages.append({"start": start, "requested": count, "returned": len(batch)})
        if len(batch) > count:
            raise ValueError("服务器返回条数超过请求上限")
        if not batch:
            return rows, {"complete": True, "termination": "empty_page", "pages": pages}
        fingerprint = hashlib.sha256(
            json.dumps(batch, sort_keys=True, default=_json_default).encode("utf-8")
        ).hexdigest()
        if fingerprint in seen:
            raise ValueError("分页重复，无法判定历史深度")
        seen.add(fingerprint)
        rows.extend(batch)
        start += len(batch)
    return rows, {"complete": False, "termination": "page_limit", "pages": pages}


def _fields(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"status": "unavailable", "reason": "未取得 K 线，不能空集通过字段断言"}
    invalid = [
        index
        for index, row in enumerate(rows)
        if type(row.get("volume")) is not int
        or row["volume"] < 0
        or type(row.get("open_interest")) is not int
        or row["open_interest"] < 0
        or "turnover" not in row
        or row["turnover"] is not None
        or row.get("settlement_price") is not None
    ]
    return {
        "status": "failed" if invalid else "passed",
        "sample_count": len(rows),
        "invalid_indices": invalid[:20],
        "volume_unit": "手（跨通道口径仍须三方核验）",
        "open_interest_unit": "手（跨通道口径仍须核验）",
        "turnover": None,
        "settlement_price": None,
        "quality_flags": ["TURNOVER_UNAVAILABLE"],
    }


def _history(client: Any, symbol: str, period: str, *, max_pages: int, count: int):
    market, code = parse_symbol(symbol)
    rows, pagination = collect_pages(
        lambda start, size: client.get_instrument_bars(CATEGORIES[period], market, code, start, size),
        max_pages=max_pages,
        count=count,
    )
    timestamps = [row["datetime"] for row in rows]
    if len(set(timestamps)) != len(timestamps):
        raise ValueError("历史 K 线跨页时间重复，深度不可确认")
    return rows, {
        "status": "observed" if rows else "unavailable",
        "symbol": symbol,
        "period": period,
        "record_count": len(rows),
        "earliest": min(timestamps) if timestamps else None,
        "latest": max(timestamps) if timestamps else None,
        "depth_is_lower_bound": not pagination["complete"],
        **pagination,
        "fields": _fields(rows),
        "samples": sorted(rows, key=lambda row: row["datetime"])[:2]
        + sorted(rows, key=lambda row: row["datetime"])[-2:],
    }


def _session_timestamp(value: datetime) -> datetime:
    return value.replace(tzinfo=CHINA_TZ) if value.tzinfo is None else value.astimezone(CHINA_TZ)


def compare_trading_day(
    client: Any,
    *,
    symbol: str,
    instrument: InstrumentId,
    trading_day: date,
    calendar: TradingCalendar,
    max_pages: int,
    count: int,
    price_tick: Decimal,
) -> dict[str, Any]:
    """只用显式 Session 给时间归属；不拿逐笔请求日期充当自然日或交易日。"""
    market, code = parse_symbol(symbol)
    if instrument.exchange.value != symbol.split(".", 1)[0] or instrument.symbol.upper() != code:
        raise ValueError("对拍日历合约必须与 TDX 目标代码一致（仅容许交易所代码大小写差异）")
    sessions = tuple(
        session
        for session in calendar.sessions_for_day(instrument, trading_day)
        if session.phase == MarketPhase.CONTINUOUS
    )
    if not sessions:
        raise ValueError("交易日没有已登记连续交易时段")
    daily, daily_depth = _history(client, symbol, "1d", max_pages=max_pages, count=count)
    minutes, minute_depth = _history(client, symbol, "5m", max_pages=max_pages, count=count)
    trades, trade_depth = collect_pages(
        lambda start, size: client.get_history_transaction_data(
            market, code, int(trading_day.strftime("%Y%m%d")), start, size
        ),
        max_pages=max_pages,
        count=count,
    )
    selected_daily = [row for row in daily if row["datetime"].date() == trading_day]
    expected_closes: set[datetime] = set()
    for session in sessions:
        at = session.start + timedelta(minutes=5)
        while at <= session.end:
            expected_closes.add(at)
            at += timedelta(minutes=5)
    selected_minutes = [
        row
        for row in minutes
        if any(session.start < _session_timestamp(row["datetime"]) <= session.end for session in sessions)
    ]
    actual_closes = {_session_timestamp(row["datetime"]) for row in selected_minutes}
    # 原生逐笔只有时钟；逐一在已登记时段对应的自然日期候选中求唯一归属。
    time_assignment_ok = True
    for row in trades:
        matches: set[datetime] = set()
        for session in sessions:
            local_start = session.start.astimezone(CHINA_TZ)
            local_end = session.end.astimezone(CHINA_TZ)
            candidate_day = local_start.date()
            while candidate_day <= local_end.date():
                candidate = datetime.combine(candidate_day, row["time"], tzinfo=CHINA_TZ)
                if session.start <= candidate <= session.end:
                    matches.add(candidate)
                candidate_day += timedelta(days=1)
        if len(matches) != 1:
            time_assignment_ok = False
            break
    complete = (
        len(selected_daily) == 1
        and bool(expected_closes)
        and actual_closes == expected_closes
        and len(actual_closes) == len(selected_minutes)
        and bool(trades)
        and trade_depth["complete"]
        and time_assignment_ok
    )
    result: dict[str, Any] = {
        "status": "unavailable",
        "symbol": symbol,
        "trading_day": trading_day,
        "calendar_version": calendar.version,
        "calendar_source": calendar.source_id,
        "daily_pagination": daily_depth["pages"],
        "minute_pagination": minute_depth["pages"],
        "trade_pagination": trade_depth,
        "minute_expected": len(expected_closes),
        "minute_observed": len(selected_minutes),
        "trades_observed": len(trades),
        "trade_time_assignment": "unique_session_match" if time_assignment_ok else "unavailable",
        "reason": "日线、完整 5m 时段或全量逐笔缺失/截断；不可断言三方一致",
        "volume": {"status": "unavailable"},
        "settlement_proxy": {"status": "unavailable", "official_settlement": False},
        "open_interest": {"status": "unavailable", "reason": "逐笔仅有增减仓，不含绝对持仓基数"},
    }
    if not complete:
        return result
    daily_row = selected_daily[0]
    daily_volume = daily_row["volume"]
    minute_volume = sum(row["volume"] for row in selected_minutes)
    trade_volume = sum(row["volume"] for row in trades)
    matched = daily_volume == minute_volume == trade_volume
    result["status"] = "observed"
    result.pop("reason")
    result["volume"] = {
        "status": "passed" if matched and trade_volume > 0 else "failed",
        "daily": daily_volume,
        "five_minute_total": minute_volume,
        "transaction_total": trade_volume,
    }
    last_minute = max(selected_minutes, key=lambda row: row["datetime"])
    result["open_interest"] = {
        "status": "passed" if last_minute["open_interest"] == daily_row["open_interest"] else "failed",
        "daily": daily_row["open_interest"],
        "last_five_minute": last_minute["open_interest"],
        "scope": "仅日线与最后一根 5m 对拍；逐笔绝对持仓不可得",
    }
    proxy = daily_row.get("price")
    if matched and trade_volume > 0 and proxy is not None:
        vwap = sum((row["price"] * row["volume"] for row in trades), Decimal(0)) / trade_volume
        delta = vwap - proxy
        result["settlement_proxy"] = {
            "status": "observed",
            "official_settlement": False,
            "proxy_price": proxy,
            "transaction_vwap": vwap,
            "vwap_minus_proxy": delta,
            "price_tick": price_tick,
            "within_one_tick": abs(delta) < price_tick,
            "scope": "单日差值不证明代理公式，也不构成官方结算核验",
        }
    return result


def run_probe(args: argparse.Namespace, *, client_factory: Callable[..., Any] = TdxExHqClient) -> dict[str, Any]:
    servers = load_tdx_servers(args.servers)
    wire_frames: list[dict[str, Any]] = []
    wire_size = 0

    def capture(frame: Mapping[str, Any]) -> None:
        nonlocal wire_size
        size = len(str(frame.get("request_hex", ""))) + len(str(frame.get("response_hex", "")))
        if len(wire_frames) < 32 and wire_size + size <= 2 * 1024 * 1024:
            wire_frames.append(dict(frame))
            wire_size += size

    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "tdx_runtime_probe",
        "source_id": "tdx_exhq",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "research_only",
        "closes_gaps": False,
        "missing_fields": ["turnover", "official_settlement_price"],
        "quality_flags": ["TURNOVER_UNAVAILABLE"],
        "limitations": [
            "公开非官方行情；授权、稳定性及完整历史待核验，不代表正式采购或阶段出口",
            "主连是未复权拼接，不能用作实际合约执行价格；原生 30m 不作为规范数据发布",
            "失败与截断如实留证，不连接 CTP 或执行服务",
        ],
        "settings": {
            "max_pages": args.max_pages,
            "page_size": args.page_size,
            "directory_pages": args.directory_pages,
            "timeout": args.timeout,
        },
        "servers_config_sha256": hashlib.sha256(args.servers.read_bytes()).hexdigest(),
        "nodes": [],
        "checks": {},
        "wire_capture": {"max_frames": 32, "max_hex_bytes": 2 * 1024 * 1024, "frames": wire_frames},
    }
    available = []
    for endpoint in servers:
        started = time.monotonic()
        client = client_factory([endpoint], timeout=args.timeout, retries=1, capture_callback=capture)
        node: dict[str, Any] = {"host": endpoint[0], "port": endpoint[1]}
        try:
            client.connect()
            node["status"] = "passed"
            available.append(endpoint)
        except Exception as exc:
            node.update(status="failed", error_type=type(exc).__name__, error=str(exc), errors=_error_chain(exc))
        finally:
            node["handshake_ms"] = round((time.monotonic() - started) * 1000, 3)
            client.close()
        report["nodes"].append(node)
    report["handshake_success_rate"] = len(available) / len(servers)
    checks = report["checks"]
    for name in ("markets", "directory", "history", "rollover", "expired_contract", "comparison"):
        checks[name] = {"status": "unavailable", "reason": "没有成功握手的节点"}
    if not available:
        return report
    client = client_factory(available, timeout=args.timeout, retries=1, capture_callback=capture)

    def attempt(name: str, action: Callable[[], Any]) -> Any:
        try:
            result = action()
            checks[name] = result
            return result
        except Exception as exc:
            checks[name] = {"status": "failed", "error_type": type(exc).__name__, "error": str(exc)}
            return None

    try:
        client.connect()
        attempt("markets", lambda: {"status": "observed", "records": client.get_markets()})

        def directory() -> dict[str, Any]:
            declared_count = client.get_instrument_count()
            rows, pagination = collect_pages(client.get_instruments, max_pages=args.directory_pages, count=1000)
            keys = [(row["market"], row["code"]) for row in rows]
            if len(keys) != len(set(keys)):
                raise ValueError("合约目录跨页重复，统计不可靠")
            complete = len(rows) == declared_count
            return {
                "status": "observed",
                "declared_count": declared_count,
                "observed_count": len(rows),
                "by_market": dict(sorted(Counter(str(row["market"]) for row in rows).items())),
                "complete": complete,
                "pagination": pagination,
                "notes": "分市场计数仅覆盖已取得目录；complete=false 时不可当作全市场统计",
                "samples": rows[:10],
            }

        attempt("directory", directory)
        history = []
        history_rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for symbol in args.symbols.split(","):
            for period in args.intervals.split(","):
                try:
                    rows, depth = _history(client, symbol, period, max_pages=args.max_pages, count=args.page_size)
                    history_rows[(symbol, period)] = rows
                    history.append(depth)
                except Exception as exc:
                    history.append({"status": "failed", "symbol": symbol, "period": period, "error": str(exc)})
        checks["history"] = {"status": "observed", "series": history}

        def rollover() -> dict[str, Any]:
            rows = history_rows.get(("SHFE.RBL8", "1d"))
            if rows is None:
                rows, _ = _history(client, "SHFE.RBL8", "1d", max_pages=args.max_pages, count=args.page_size)
            selected = sorted(
                (row for row in rows if row["datetime"].date() in (date(2026, 9, 1), date(2026, 9, 2))),
                key=lambda row: row["datetime"],
            )
            if len(selected) != 2:
                return {"status": "unavailable", "reason": "采样范围未包含完整 2026-09-01/02 主连记录"}
            previous, following = selected
            return {
                "status": "observed",
                "samples": selected,
                "next_open_minus_previous_close": following["open"] - previous["close"],
                "close_change": following["close"] - previous["close"],
                "open_interest_change": following["open_interest"] - previous["open_interest"],
                "rollover_contract_attribution": "pending",
                "notes": "如实计算价差；两根主连记录不足以证明切换合约或复权口径",
            }

        attempt("rollover", rollover)

        def expired() -> dict[str, Any]:
            rows = client.get_instrument_bars(4, 30, "RB2410", 0, args.page_size)
            return {
                "status": "observed",
                "symbol": "SHFE.RB2410",
                "period": "1d",
                "returned": len(rows),
                "empty": not rows,
                "scope": "只证明当前节点当前请求的返回；不推断所有已到期合约不可得",
            }

        attempt("expired_contract", expired)
        if args.calendar is None or args.comparison_day is None:
            checks["comparison"] = {
                "status": "unavailable",
                "reason": "须显式提供 --calendar 与 --comparison-day；禁止按自然日推断夜盘归属",
                "volume": {"status": "unavailable"},
                "settlement_proxy": {"status": "unavailable", "official_settlement": False},
                "open_interest": {"status": "unavailable"},
            }
        else:
            exchange, contract = args.calendar_symbol.split(".", 1)
            calendar = TradingCalendar.from_file(args.calendar)
            attempt(
                "comparison",
                lambda: compare_trading_day(
                    client,
                    symbol=args.comparison_symbol,
                    instrument=InstrumentId(Exchange(exchange), contract),
                    trading_day=args.comparison_day,
                    calendar=calendar,
                    max_pages=args.max_pages,
                    count=args.page_size,
                    price_tick=Decimal(args.price_tick),
                ),
            )
    except Exception as exc:
        checks["connection"] = {"status": "failed", "error_type": type(exc).__name__, "error": str(exc)}
    finally:
        client.close()
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--servers", type=Path, default=ROOT / "config/tdx_exhq_servers.yaml")
    parser.add_argument("--symbols", default="SHFE.RBL8,SHFE.RB2701")
    parser.add_argument("--intervals", default="1d,1m,30m", help="仅探测原生周期；30m 不能直接用于发布")
    parser.add_argument("--max-pages", type=int, default=2, help="每个序列最多页数；达到上限明确标记截断")
    parser.add_argument("--page-size", type=int, default=700)
    parser.add_argument("--directory-pages", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--comparison-symbol", default="SHFE.RB2701")
    parser.add_argument("--comparison-day", type=date.fromisoformat)
    parser.add_argument("--calendar", type=Path)
    parser.add_argument("--calendar-symbol", default="SHFE.rb2701", help="日历中的同一实际合约；保留大小写")
    parser.add_argument("--price-tick", default="1", help="逐笔均价代理差值的已知最小价格跳动")
    parser.add_argument("--output", help="证据 JSON 路径，仅允许项目 runs/s0 内")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        target = evidence_target(ROOT, args.output)
        if args.max_pages <= 0 or args.directory_pages <= 0 or not 1 <= args.page_size <= 700:
            raise ValueError("分页次数须为正数，page-size 须在 1 至 700")
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise ValueError("timeout 须为有限正数")
        tick = Decimal(args.price_tick)
        if not tick.is_finite() or tick <= 0:
            raise ValueError("price-tick 须为有限正数")
        for symbol in args.symbols.split(",") + [args.comparison_symbol]:
            parse_symbol(symbol)
        if any(period not in CATEGORIES for period in args.intervals.split(",")):
            raise ValueError("不支持的探测周期")
        if (args.calendar is None) != (args.comparison_day is None):
            raise ValueError("三方对拍必须同时提供 calendar 与 comparison-day")
        report = run_probe(args)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=_json_default) + "\n", encoding="utf-8"
        )
    except (OSError, ValueError, ArithmeticError) as exc:
        print(f"TDX 探测失败: {exc}", file=sys.stderr)
        return 2
    print(f"研究证据: {target.relative_to(ROOT).as_posix()}；未关闭任何数据缺口")
    for name, check in report["checks"].items():
        print(f"[{check['status']}] {name}")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=_json_default))
    failed = report["handshake_success_rate"] == 0 or any(
        value.get("status") == "failed" for value in report["checks"].values()
    )
    failed |= any(
        series.get("status") == "failed" or series.get("fields", {}).get("status") == "failed"
        for series in report["checks"].get("history", {}).get("series", [])
    )
    failed |= any(
        item.get("status") == "failed"
        for item in report["checks"].get("comparison", {}).values()
        if isinstance(item, dict)
    )
    return 1 if failed else 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
