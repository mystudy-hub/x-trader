#!/usr/bin/env python
"""[脚本工具] 抓取 S4 测试品种组合的实际合约行情 (S4-05, FR-CAL-08, FR-DATA-02).

覆盖第二章第 4 项的测试品种组合：螺纹钢 (rb, 有夜盘) / 甲醇 (MA, 跨交易所) /
铁矿或豆粕 (i/m, 大商所) / 苹果或鸡蛋 (AP/jd, 无夜盘) / 黄金或铜 (au/cu, 凌晨收盘)。

流程：抓取实际合约日线(与可选小时线) -> 字段标准化 -> 质量校验 -> 不可变 Parquet 归档。
只用实际合约，绝不把主连序列当作成交标的 (FR-CON-01/A29)。

数据来源为新浪公开行情 (research mode)。该来源缺少成交额字段，质量标记与
"待核验"语义按 FR-DATA-08 显式登记，不静默放宽为精确核算。
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.core.objects import InstrumentId  # noqa: E402
from qh_trader.data.product_registry import get_product_spec, normalize_product  # noqa: E402
from qh_trader.data.schemas import (  # noqa: E402
    build_standard_calendar_and_timings,
    convert_daily_records_to_bars,
    convert_minute_records_to_bars,
    validate_ohlc_records,
)
from qh_trader.data.sources import create_data_source  # noqa: E402
from qh_trader.data.storage import ParquetDataStorage  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_s4_test_products")


# 覆盖 2024-06 ~ 2026-12 的可交易区间；日期不存在时来源返回空，直接跳过。
S4_CONTRACTS: dict[str, tuple[str, ...]] = {
    "rb": ("rb2410", "rb2501", "rb2505", "rb2510", "rb2601", "rb2605", "rb2610"),
    "fg": ("FG2501", "FG2505", "FG2509", "FG2601", "FG2605", "FG2609"),
    "MA": ("MA2501", "MA2505", "MA2509", "MA2601", "MA2605", "MA2609"),
    "i": ("i2501", "i2505", "i2509", "i2601", "i2605", "i2609"),
    "m": ("m2501", "m2505", "m2509", "m2601", "m2605", "m2609"),
    "AP": ("AP2501", "AP2505", "AP2510", "AP2601", "AP2605", "AP2610"),
    "jd": ("jd2501", "jd2505", "jd2509", "jd2601", "jd2605", "jd2609"),
    "au": ("au2412", "au2506", "au2512", "au2606", "au2612"),
    "cu": ("cu2410", "cu2411", "cu2412", "cu2503", "cu2506", "cu2509", "cu2512", "cu2603", "cu2606", "cu2609"),
    # 已有 rb/FG 实际合约；补齐玉米实际合约以便多品种组合归因。
    "c": ("c2501", "c2505", "c2509", "c2601", "c2605", "c2609"),
}

DEFAULT_INTERVALS = ("1d",)


def _to_instrument(code: str) -> InstrumentId:
    product = normalize_product("".join(ch for ch in code if ch.isalpha()))
    spec = get_product_spec(product)
    return InstrumentId(spec.exchange, code)


def fetch_product_contracts(
    storage: ParquetDataStorage,
    product: str,
    contracts: tuple[str, ...],
    intervals: tuple[str, ...],
    *,
    start_date: str | None,
    end_date: str | None,
    sleep_seconds: float,
) -> list[dict]:
    ds = create_data_source("sina")
    spec = get_product_spec(product)
    rows: list[dict] = []
    for code in contracts:
        instrument = InstrumentId(spec.exchange, code)
        for interval in intervals:
            try:
                if interval == "1d":
                    raw = ds.fetch_daily_bars(instrument, start_date=start_date, end_date=end_date)
                    time_key = "date"
                else:
                    period = "60" if interval == "1h" else interval.removesuffix("m")
                    raw = ds.fetch_minute_bars(instrument, period=period)
                    time_key = "datetime"

                if not raw:
                    rows.append({"instrument": str(instrument), "interval": interval, "count": 0, "status": "NO_DATA"})
                    continue

                report = validate_ohlc_records(raw, time_key=time_key, strict=False, instrument=instrument)
                if not report.is_clean:
                    logger.warning(
                        "[%s %s] 质量标记 %d 条 (研究模式保留，报告标注)", instrument, interval, len(report.issues)
                    )

                timings, calendar = build_standard_calendar_and_timings(raw, instrument, interval=interval)
                source_version = (
                    (start_date or "earliest").replace("-", "")
                    + "_"
                    + (end_date or "latest").replace("-", "")
                )
                if interval == "1d":
                    bars = convert_daily_records_to_bars(
                        raw,
                        instrument=instrument,
                        timings=timings,
                        calendar=calendar,
                        source_id=ds.source_id,
                        source_version=source_version,
                        require_turnover=False,
                    )
                else:
                    bars = convert_minute_records_to_bars(
                        raw,
                        instrument=instrument,
                        interval=interval,
                        timings=timings,
                        calendar=calendar,
                        source_id=ds.source_id,
                        source_version=source_version,
                        source_timezone="Asia/Shanghai",
                        require_turnover=False,
                    )

                storage.save_bars(bars, instrument=instrument, interval=interval)
                rows.append(
                    {
                        "instrument": str(instrument),
                        "interval": interval,
                        "count": len(bars),
                        "span": f"{raw[0][time_key]} ~ {raw[-1][time_key]}",
                        "status": "SUCCESS",
                    }
                )
                logger.info(
                    "[%s %s] 归档 %d 条 (%s ~ %s)",
                    instrument,
                    interval,
                    len(bars),
                    raw[0][time_key],
                    raw[-1][time_key],
                )
            except Exception as exc:  # noqa: BLE001 - 单合约失败不阻断其余品种
                logger.error("[%s %s] 失败: %s", instrument, interval, exc)
                rows.append(
                    {"instrument": str(instrument), "interval": interval, "count": 0, "status": f"FAILED: {exc}"}
                )
            if sleep_seconds:
                time.sleep(sleep_seconds)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取 S4 测试品种组合实际合约行情")
    parser.add_argument("--products", default="rb,fg,MA,i,m,AP,jd,au,cu,c", help="目标品种，逗号分隔")
    parser.add_argument("--intervals", default=",".join(DEFAULT_INTERVALS), help="周期，逗号分隔 (1d,1h)")
    parser.add_argument("--start", default="2024-06-01", help="日线起始日期")
    parser.add_argument("--end", default=None, help="日线结束日期")
    parser.add_argument("--sleep", type=float, default=0.3, help="请求间隔秒数")
    args = parser.parse_args()

    storage = ParquetDataStorage(root_dir=ROOT / "data_storage")
    products = [normalize_product(p.strip()) for p in args.products.split(",") if p.strip()]
    intervals = tuple(x.strip() for x in args.intervals.split(",") if x.strip())

    all_rows: list[dict] = []
    for product in products:
        contracts = S4_CONTRACTS.get(product)
        if contracts is None:
            logger.warning("品种 %s 未登记测试合约清单，跳过", product)
            continue
        logger.info("=========== %s (%d 个合约) ===========", product, len(contracts))
        all_rows.extend(
            fetch_product_contracts(
                storage,
                product,
                contracts,
                intervals,
                start_date=args.start,
                end_date=args.end,
                sleep_seconds=args.sleep,
            )
        )

    succeeded = sum(1 for r in all_rows if r["status"] == "SUCCESS")
    failed = [r for r in all_rows if r["status"] not in ("SUCCESS", "NO_DATA")]
    logger.info(
        "完成: 成功 %d 项, 失败 %d 项, 无数据 %d 项",
        succeeded,
        len(failed),
        sum(1 for row in all_rows if row["status"] == "NO_DATA"),
    )
    for row in failed:
        print(f"FAILED {row['instrument']} {row['interval']}: {row['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
