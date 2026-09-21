#!/usr/bin/env python
"""[脚本工具] 把误入工程样本存储的 S4 研究数据集迁移到独立研究存储，并按日历重标日线开盘时段 (B4/B5).

步骤：
1. 从源存储 (默认 data_storage) 当前快照中挑出 S4 研究来源 (sina_futures) 的实际合约日线；
2. 按 S4 时段模板投影的日历重标 open_time / session_id (夜盘品种 → 前一自然日 21:00 夜盘首笔)，
   标记 SYNTHETIC，并把时段假设写入数据集 provenance；
3. 发布到目标研究存储 (默认 data_storage/s4_research)；
4. 把工程样本合约 (SHFE.rb2410 1d) 恢复到固定工程快照的版本，并从源存储当前指针移除 S4 数据集键。
   内容寻址文件与旧清单原样保留，旧快照仍可按 ID 读取 (FR-VAL-07)。

只在 --apply 时写入；默认只打印计划。
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
from qh_trader.data.calendar import project_product_calendar  # noqa: E402
from qh_trader.data.daily_timing import DAILY_OPEN_TIMING_ASSUMPTION, retime_daily_bars  # noqa: E402
from qh_trader.data.product_registry import get_product_spec, normalize_product  # noqa: E402
from qh_trader.data.storage import ParquetDataStorage  # noqa: E402

ENGINEERING_SAMPLE = InstrumentId(Exchange.SHFE, "rb2410")
ENGINEERING_SNAPSHOT = "4379bc819d9db69d8264309f319333cb1fd36af05c8311f6ff71409f50fccd40"
RESEARCH_SOURCE = "sina_futures"


def _product_of(symbol: str) -> str:
    return normalize_product("".join(ch for ch in symbol if ch.isalpha()))


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate and retime the S4 research dataset")
    parser.add_argument("--source", default="data_storage")
    parser.add_argument("--target", default="data_storage/s4_research")
    parser.add_argument("--session-template", default="config/sessions_s4_2024v2.json")
    parser.add_argument("--engineering-snapshot", default=ENGINEERING_SNAPSHOT)
    parser.add_argument("--apply", action="store_true", help="Write; otherwise print the plan only")
    args = parser.parse_args()

    source = ParquetDataStorage(ROOT / args.source)
    target = ParquetDataStorage(ROOT / args.target)
    current = source.capture_snapshot()
    pinned = source.capture_snapshot(args.engineering_snapshot)

    research_keys: list[str] = []
    plan: list[tuple[InstrumentId, int, int, str]] = []
    for key, entry in sorted(current.datasets.items()):
        if entry.get("kind") != "bar" or entry.get("interval") != "1d":
            continue
        exchange_name, symbol = str(entry["instrument"]).split(".", 1)
        if not re.fullmatch(r"[A-Za-z]{1,3}\d{4}", symbol):
            continue
        instrument = InstrumentId(Exchange(exchange_name), symbol)
        bars = source.read_bars(instrument, "1d", snapshot=current)
        research = [bar for bar in bars if bar.meta.source_id == RESEARCH_SOURCE]
        if not research:
            continue
        try:
            spec = get_product_spec(_product_of(symbol))
        except Exception:  # noqa: BLE001 - 未登记品种不属于 S4 研究集
            continue
        days = sorted({bar.meta.trading_day for bar in research})
        calendar = project_product_calendar(
            ROOT / args.session_template, spec.product, (instrument,), window=(days[0], days[-1])
        )
        retimed = retime_daily_bars(research, calendar, instrument)
        night = sum(1 for bar in retimed if bar.meta.session_id.startswith("night"))
        plan.append((instrument, len(retimed), night, calendar.version or ""))
        if key not in pinned.datasets or instrument == ENGINEERING_SAMPLE:
            research_keys.append(key)
        if args.apply:
            target.publish_batch(
                instrument,
                "1d",
                bars=retimed,
                merge_existing=False,
                provenance={
                    "migrated_from": f"{args.source}@{current.snapshot_id}",
                    "source_dataset_sha256": entry["sha256"],
                    "timing_assumption": DAILY_OPEN_TIMING_ASSUMPTION,
                    "session_template": args.session_template,
                    "session_template_version": calendar.version,
                    "retimed_at": datetime.now(timezone.utc).isoformat(),
                },
            )

    for instrument, count, night, version in plan:
        print(f"{instrument}: {count} bars, {night} night-open, calendar {version}")
    print(f"research datasets to retire from {args.source}: {len(research_keys)}")

    if not args.apply:
        print("dry run; pass --apply to write")
        return 0

    # 恢复工程样本到固定快照版本 (与旧内容哈希相同，只是把当前指针指回去)
    sample_bars = source.read_bars(ENGINEERING_SAMPLE, "1d", snapshot=pinned)
    source.publish_batch(
        ENGINEERING_SAMPLE,
        "1d",
        bars=sample_bars,
        merge_existing=False,
        provenance={
            "restored_from_snapshot": args.engineering_snapshot,
            "restored_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    retire = [key for key in research_keys if key != f"bar/{ENGINEERING_SAMPLE}/1d"]
    if retire:
        source.retire_datasets(retire)
    final = source.capture_snapshot()
    restored = final.datasets[f"bar/{ENGINEERING_SAMPLE}/1d"]["sha256"]
    original = pinned.datasets[f"bar/{ENGINEERING_SAMPLE}/1d"]["sha256"]
    print(
        json.dumps(
            {
                "source_snapshot": final.snapshot_id,
                "target_snapshot": target.capture_snapshot().snapshot_id,
                "rb2410_restored": restored == original,
                "retired": len(retire),
            },
            indent=2,
        )
    )
    return 0 if restored == original else 1


if __name__ == "__main__":
    sys.exit(main())
