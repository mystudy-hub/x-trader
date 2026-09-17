#!/usr/bin/env python
"""[脚本工具] 从 Tushare 自动构建符合系统规范的真实历史合约目录 (S1-03, FR-CON-01, A29).

从 Tushare fut_basic 接口提取真实的上市日 (list_date)、摘牌日 (delist_date)、
乘数与最小变动价位，生成供 ContractResolver.from_file 使用的规范 JSON 目录。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.data.sources import create_data_source  # noqa: E402

# 常见品种乘数与最小变动
PRODUCT_SPECS: dict[str, tuple[Decimal, Decimal]] = {
    "rb": (Decimal("10"), Decimal("1")),
    "fg": (Decimal("20"), Decimal("1")),
    "c": (Decimal("10"), Decimal("1")),
    "m": (Decimal("10"), Decimal("1")),
    "cu": (Decimal("5"), Decimal("10")),
    "au": (Decimal("1000"), Decimal("0.02")),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("build_contract_catalog")


def build_catalog(
    exchanges: tuple[str, ...] = ("SHFE", "CZCE", "DCE"),
    products: set[str] | None = None,
    output_path: str = "config/contract_catalog_2024v1.json",
) -> Path:
    out_file = ROOT / output_path
    out_file.parent.mkdir(parents=True, exist_ok=True)

    ds = create_data_source("tushare")
    entries: list[dict] = []

    available_at = datetime(2020, 1, 1, 0, 0, 0, tzinfo=timezone.utc).isoformat()

    for ex in exchanges:
        logger.info("正在获取交易所 %s 的合约基础信息...", ex)
        raw_items = ds.fetch_contract_catalog(ex)
        logger.info("交易所 %s 返回 %d 个合约记录", ex, len(raw_items))

        for row in raw_items:
            # 过滤品种
            fut_code = str(row.get("fut_code", "")).lower()
            if products and fut_code not in products:
                continue

            symbol_raw = str(row.get("symbol", ""))
            # 排除非纯合约 (如套利合约、期权等)
            if any(char in symbol_raw for char in ("&", "-", "C", "P")) and not fut_code.isalpha():
                continue

            d_month = str(row.get("d_month", ""))
            if len(d_month) != 6 or not d_month.isdigit():
                continue

            delivery_year = int(d_month[:4])
            delivery_month = int(d_month[4:6])

            list_date_raw = str(row.get("list_date", ""))
            delist_date_raw = str(row.get("delist_date", ""))
            if not (len(list_date_raw) == 8 and len(delist_date_raw) == 8):
                continue

            listed_on = f"{list_date_raw[:4]}-{list_date_raw[4:6]}-{list_date_raw[6:]}"
            last_trading_day = f"{delist_date_raw[:4]}-{delist_date_raw[4:6]}-{delist_date_raw[6:]}"

            # 获取乘数与最小变动价位 (优先从系统内验证过的 PRODUCT_DEFAULTS 取)
            defaults = PRODUCT_SPECS.get(fut_code)
            if defaults:
                multiplier, price_tick = defaults
            else:
                per_unit = row.get("per_unit") or 10.0
                multiplier = Decimal(str(per_unit))
                price_tick = Decimal("1.0")

            clean_symbol = symbol_raw.lower() if ex != "CZCE" else symbol_raw.upper()
            aliases = [symbol_raw, clean_symbol, f"{clean_symbol}.{ex}"]
            # 郑商所处理: 根据真实的 delivery_year 生成对应的 4 位与 3 位别名
            if ex == "CZCE":
                y_2d = f"{delivery_year % 100:02d}"
                m_2d = f"{delivery_month:02d}"
                aliases.append(f"{fut_code.upper()}{y_2d}{m_2d}")
                aliases.append(f"{fut_code.upper()}{y_2d}{m_2d}.CZCE")
                # 3 位短代码
                aliases.append(f"{fut_code.upper()}{y_2d[-1]}{m_2d}")

            entries.append(
                {
                    "exchange": ex,
                    "symbol": clean_symbol,
                    "product": fut_code,
                    "aliases": sorted(list(set(aliases))),
                    "delivery_year": delivery_year,
                    "delivery_month": delivery_month,
                    "multiplier": str(multiplier),
                    "price_tick": str(price_tick),
                    "listed_on": listed_on,
                    "last_trading_day": last_trading_day,
                    "source_id": "tushare_pro_fut_basic",
                    "available_at": available_at,
                }
            )

    # 为所包含的品种添加主连连续条目
    for p_code, (mult, pt) in PRODUCT_SPECS.items():
        if products and p_code not in products:
            continue
        p_ex = "SHFE" if p_code in {"rb", "cu", "au"} else ("CZCE" if p_code == "fg" else "DCE")
        p_sym = f"{p_code.lower() if p_ex != 'CZCE' else p_code.upper()}0"
        entries.append(
            {
                "exchange": p_ex,
                "symbol": p_sym,
                "product": p_code,
                "aliases": [p_sym, f"{p_sym}.{p_ex}", p_code],
                "delivery_year": 2099,
                "delivery_month": 12,
                "multiplier": str(mult),
                "price_tick": str(pt),
                "listed_on": "2000-01-01",
                "last_trading_day": "2099-12-31",
                "source_id": "system_continuous",
                "available_at": available_at,
            }
        )

    catalog_data = {
        "schema_version": 1,
        "catalog_version": "2024v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Tushare Pro",
        "entries": entries,
    }

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(catalog_data, f, indent=2, ensure_ascii=False)

    logger.info("合约目录生成成功! 写入 %d 个合约条目 -> %s", len(entries), out_file)
    return out_file


def main() -> int:
    parser = argparse.ArgumentParser(description="从 Tushare 自动构建规范合约目录")
    parser.add_argument("--products", default="rb,fg", help="目标品种，逗号分隔 (默认: rb,fg)")
    parser.add_argument("--output", default="config/contract_catalog_2024v1.json", help="输出路径")
    args = parser.parse_args()

    prod_set = {p.strip().lower() for p in args.products.split(",") if p.strip()}
    build_catalog(products=prod_set, output_path=args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
