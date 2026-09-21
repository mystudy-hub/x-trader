#!/usr/bin/env python
"""[脚本工具] 构建 S4 测试品种组合的版本化 Session 模板文件 (S4-05, FR-CAL-08).

输入：已入库实际合约的交易日集合（union）+ `product_registry` 的品种属性。
输出：`config/sessions_s4_2024v2.json`（schema_version 2 紧凑模板：交易序列 + 逐合约时段模板 +
      公告例外），由 `TradingCalendar.from_file` / `project_product_calendar` 按需展开为显式 Session。

交易日来自可观测行情而不是推算的节假日表。夜盘取消规则 (A05/A25-05)：相邻交易日之间若存在
非交易的工作日，视为法定假日间隔，前一交易日晚不生成夜盘；只隔周末仍有夜盘。交易所公告的
额外例外 (临时取消夜盘、节后竞价安排) 写入 `night_session_exceptions`，归档后再填。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.core.constants import Exchange  # noqa: E402
from qh_trader.core.objects import InstrumentId  # noqa: E402
from qh_trader.data.product_registry import registered_products  # noqa: E402
from qh_trader.data.storage import ParquetDataStorage  # noqa: E402

# 每个品种取一个代表性主力合约作为模板载体；展开后覆盖该合约的全部交易日。
TEMPLATE_CONTRACTS: dict[str, str] = {
    "rb": "rb2601",
    "fg": "FG2601",
    "MA": "MA2601",
    "c": "c2601",
    "m": "m2601",
    "i": "i2601",
    "AP": "AP2601",
    "jd": "jd2601",
    "au": "au2612",
    "cu": "cu2609",
}


def _registered_product_codes() -> set[str]:
    return {spec.product.casefold() for spec in registered_products()}


def _product_of(instrument: InstrumentId) -> str:
    return "".join(ch for ch in instrument.symbol if ch.isalpha())


def observed_trading_days(storage: ParquetDataStorage, codes: set[str], *, start: str, end: str | None) -> list[str]:
    """汇总已入库实际合约 (1d) 的可观测交易日，不依赖推算节假日表."""
    days: set[str] = set()
    snapshot = storage.capture_snapshot()
    for entry in snapshot.datasets.values():
        if entry.get("kind") != "bar" or entry.get("interval") != "1d":
            continue
        symbol = str(entry.get("instrument", ""))
        if "." not in symbol:
            continue
        exchange_name, code = symbol.split(".", 1)
        if re.fullmatch(r"[A-Za-z]+0", code):  # 主连/连续序列不是成交标的
            continue
        if _product_of(InstrumentId(Exchange(exchange_name), code)).casefold() not in codes:
            continue
        for bar in storage.read_bars(InstrumentId(Exchange(exchange_name), code), "1d", snapshot=snapshot):
            day = bar.meta.trading_day.isoformat()
            if day < start or (end is not None and day > end):
                continue
            days.add(day)
    return sorted(days)


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 S4 Session 模板文件")
    parser.add_argument("--version", default="s4-2024v2-heuristic", help="日历版本号")
    parser.add_argument("--output", default="config/sessions_s4_2024v2.json", help="输出路径")
    parser.add_argument("--storage", default="data_storage/s4_research", help="研究数据存储目录")
    parser.add_argument("--start", default="2024-06-01", help="覆盖区间起始交易日")
    parser.add_argument("--end", default=None, help="覆盖区间结束交易日")
    parser.add_argument("--keep-exceptions-from", default=None, help="沿用既有模板文件里的 night_session_exceptions")
    args = parser.parse_args()

    storage = ParquetDataStorage(root_dir=ROOT / args.storage)
    profiles: list[dict] = []
    for spec in registered_products():
        symbol = TEMPLATE_CONTRACTS.get(spec.product)
        if symbol is None:
            continue
        instrument = InstrumentId(spec.exchange, symbol)
        if not storage.has_bar_data(instrument, "1d"):
            continue
        profiles.append(
            {
                "exchange": spec.exchange.value,
                "symbol": symbol,
                "product": spec.product,
                "has_night": spec.night_session,
                "night_close": spec.night_close.strftime("%H:%M:%S") if spec.night_close else None,
                "day_auction_style": spec.day_auction_style.value,
            }
        )

    days = observed_trading_days(storage, _registered_product_codes(), start=args.start, end=args.end)
    if not days:
        print("no observed trading days; ingest data first", file=sys.stderr)
        return 1

    exceptions: list[dict] = []
    if args.keep_exceptions_from:
        previous = json.loads((ROOT / args.keep_exceptions_from).read_text(encoding="utf-8"))
        exceptions = list(previous.get("night_session_exceptions", []))

    payload = {
        "schema_version": 2,
        "version": args.version,
        "source_id": "observed_sina_trading_days_plus_exchange_session_rules",
        "available_at": datetime.now(timezone.utc).isoformat(),
        "coverage_start": days[0],
        "coverage_end": days[-1],
        "trading_days": days,
        "session_profiles": profiles,
        "night_session_exceptions": exceptions,
        "assumptions": [
            "交易日序列来自已入库实际合约日线的可观测交易日集合，不是推算的节假日表。",
            "夜盘收盘档位与日盘竞价风格取自交易所公开交易时间规则 (docs/references)。",
            "夜盘取消: 相邻交易日之间存在非交易工作日 (法定假日) 时，前一交易日晚不生成夜盘；只隔周末仍有夜盘。"
            "交易所公告的临时例外写入 night_session_exceptions [{eve, has_night, source}]，归档后再填 (A05/A25-05)。",
            "节后日盘竞价按常规模板处理；公告要求的特殊竞价安排待归档核验。",
        ],
    }

    out_file = ROOT / args.output
    out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {out_file} : {len(profiles)} profiles, {len(days)} trading days ({days[0]} ~ {days[-1]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
