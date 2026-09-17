#!/usr/bin/env python
"""[脚本工具] 批量获取多品种历史行情数据 (玉米、玻璃、豆粕、螺纹钢、沪铜、沪金).

覆盖周期:
- 1d (日线，近 2 年)
- 1h (1 小时线)
- 15m (15 分钟线)

自动完成: 数据抓取 -> 字段标准化 -> 质量标记与校验 -> 不可变 Parquet 持久化与快照更新.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.core.constants import Exchange  # noqa: E402
from qh_trader.core.objects import InstrumentId  # noqa: E402
from qh_trader.data.schemas import (  # noqa: E402
    build_standard_calendar_and_timings,
    convert_daily_records_to_bars,
    convert_minute_records_to_bars,
    validate_ohlc_records,
)
from qh_trader.data.sources import create_data_source  # noqa: E402
from qh_trader.data.storage import ParquetDataStorage  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_multi_products")

TARGET_PRODUCTS = [
    ("玉米", Exchange.DCE, "c0"),
    ("玻璃", Exchange.CZCE, "FG0"),
    ("豆粕", Exchange.DCE, "m0"),
    ("螺纹钢", Exchange.SHFE, "rb0"),
    ("沪铜", Exchange.SHFE, "cu0"),
    ("沪金", Exchange.SHFE, "au0"),
]

TARGET_INTERVALS = ["1d", "1h", "15m"]


def main() -> int:
    storage = ParquetDataStorage(root_dir=ROOT / "data_storage")
    ds = create_data_source("sina")

    # 至今 2 年起始日期 (2024-09-14)
    start_date = (datetime.now() - timedelta(days=2 * 365)).strftime("%Y-%m-%d")

    logger.info("================ 开始批量获取多品种行情数据 ================")
    logger.info("目标品种: %s", [name for name, _, _ in TARGET_PRODUCTS])
    logger.info("目标周期: %s, 日线起始日期: %s", TARGET_INTERVALS, start_date)

    summary_rows: list[dict] = []

    for name, ex, sym in TARGET_PRODUCTS:
        inst = InstrumentId(ex, sym)
        logger.info("\n>>> 正在处理品种: %s (%s) <<<", name, inst)

        for inv in TARGET_INTERVALS:
            try:
                # 1. 抓取数据
                if inv == "1d":
                    raw_records = ds.fetch_daily_bars(inst, start_date=start_date)
                    time_key = "date"
                else:
                    period_param = "60" if inv == "1h" else inv.removesuffix("m")
                    raw_records = ds.fetch_minute_bars(inst, period=period_param)
                    time_key = "datetime"

                if not raw_records:
                    logger.warning("[%s - %s] 未获取到数据", inst, inv)
                    continue

                # 2. 质量校验
                _quality_report = validate_ohlc_records(raw_records, time_key=time_key, strict=False)
                if not _quality_report.is_clean:
                    logger.warning("数据存在质量问题: %d 条", len(_quality_report.issues))

                # 3. 转换为规范 Bar 对象
                timings, calendar = build_standard_calendar_and_timings(raw_records, inst, interval=inv)
                if inv == "1d":
                    bars = convert_daily_records_to_bars(
                        raw_records,
                        instrument=inst,
                        timings=timings,
                        calendar=calendar,
                        source_id=ds.source_id,
                        source_version="1.0",
                    )
                else:
                    bars = convert_minute_records_to_bars(
                        raw_records,
                        instrument=inst,
                        interval=inv,
                        timings=timings,
                        calendar=calendar,
                        source_id=ds.source_id,
                        source_version="1.0",
                        source_timezone="Asia/Shanghai",
                    )

                # 4. 原子持久化与版本发布
                out_path = storage.save_bars(bars, instrument=inst, interval=inv)

                first_time = raw_records[0][time_key]
                last_time = raw_records[-1][time_key]

                logger.info(
                    "[%s - %s] 成功归档! 条数: %d, 跨度: %s ~ %s, 路径: %s",
                    name,
                    inv,
                    len(bars),
                    first_time,
                    last_time,
                    out_path.name,
                )

                summary_rows.append(
                    {
                        "product": name,
                        "instrument": str(inst),
                        "interval": inv,
                        "count": len(bars),
                        "span": f"{first_time} ~ {last_time}",
                        "status": "SUCCESS",
                    }
                )

                # 避免连续请求过快
                time.sleep(0.3)

            except Exception as exc:
                logger.error("[%s - %s] 处理异常: %s", name, inv, exc)
                summary_rows.append(
                    {
                        "product": name,
                        "instrument": str(inst),
                        "interval": inv,
                        "count": 0,
                        "span": "-",
                        "status": f"FAILED: {exc}",
                    }
                )

    logger.info("\n" + "=" * 80)
    logger.info("                       批量数据获取汇总报告")
    logger.info("=" * 80)
    fmt = "{:<8s} | {:<12s} | {:<8s} | {:<8s} | {:<35s} | {:<10s}"
    print(fmt.format("品种", "合约标识", "周期", "记录数", "时间跨度", "状态"))
    print("-" * 90)
    for r in summary_rows:
        print(fmt.format(r["product"], r["instrument"], r["interval"], str(r["count"]), r["span"], r["status"]))
    print("=" * 90)

    return 0


if __name__ == "__main__":
    sys.exit(main())
