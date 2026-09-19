#!/usr/bin/env python
"""[脚本工具] 构建 S4 测试品种组合的版本化历史合约目录 (S4-05, FR-CON-01, A29).

从已入库实际合约日线系列提取可观测的上市/摘牌区间，乘数与最小变动价位取自
`product_registry` 的研究登记。输出独立版本文件，不覆盖 S1 的工程样本目录。

来源标记为"观测序列 + 研究登记"，上市/摘牌区间可能被数据可得性截断，
对应缺口登记在 `config/data_coverage.yaml`；官方目录到位后应重建新版本。
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

SOURCE_ID = "sina_observed_series_plus_product_registry"


def _product_of(symbol: str) -> str:
    return "".join(ch for ch in symbol if ch.isalpha())


def _delivery(symbol: str) -> tuple[int, int] | None:
    digits = "".join(ch for ch in symbol if ch.isdigit())
    if len(digits) == 4:
        return int(digits[:2]) + 2000, int(digits[2:])
    if len(digits) == 3:
        return int(digits[0]) + 2000, int(digits[1:])
    return None


def _clean_symbol(exchange: Exchange, code: str) -> str:
    return code.upper() if exchange == Exchange.CZCE else code.lower()


def build_catalog(output_path: str = "config/contract_catalog_s4_2024v1.json") -> Path:
    storage = ParquetDataStorage(root_dir=ROOT / "data_storage")
    snapshot = storage.capture_snapshot()
    registered = {spec.product.casefold(): spec for spec in registered_products()}

    # 1d 实际合约系列 -> 可观测区间
    observed: dict[str, tuple[Exchange, str, str]] = {}
    for entry in snapshot.datasets.values():
        if entry.get("kind") != "bar" or entry.get("interval") != "1d":
            continue
        symbol = str(entry.get("instrument", ""))
        if "." not in symbol:
            continue
        exchange_name, code = symbol.split(".", 1)
        if re.fullmatch(r"[A-Za-z]+0", code):  # 主连/连续序列不是成交标的
            continue
        product = _product_of(code)
        if product.casefold() not in registered:
            continue
        observed[symbol] = (Exchange(exchange_name), code, product)

    available_at = datetime(2024, 1, 1, tzinfo=timezone.utc).isoformat()
    entries: list[dict] = []
    for symbol in sorted(observed):
        exchange, code, product = observed[symbol]
        spec = registered[product.casefold()]
        delivery = _delivery(code)
        if delivery is None:
            continue
        year, month = delivery
        bars = storage.read_bars(InstrumentId(exchange, code), "1d", snapshot=snapshot)
        if not bars:
            continue
        listed_on = min(bar.meta.trading_day for bar in bars).isoformat()
        last_trading_day = max(bar.meta.trading_day for bar in bars).isoformat()
        clean = _clean_symbol(exchange, code)
        aliases = {code, clean, f"{clean}.{exchange.value}", f"{exchange.value}.{clean}", product}
        if exchange == Exchange.CZCE:
            aliases.add(f"{product.upper()}{year % 100:02d}{month:02d}")
            aliases.add(f"{product.upper()}{year % 10}{month:02d}")
        entries.append(
            {
                "exchange": exchange.value,
                "symbol": clean,
                "product": spec.product,
                "aliases": sorted(aliases),
                "delivery_year": year,
                "delivery_month": month,
                "multiplier": str(spec.multiplier),
                "price_tick": str(spec.price_tick),
                "listed_on": listed_on,
                "last_trading_day": last_trading_day,
                "source_id": SOURCE_ID,
                "available_at": available_at,
            }
        )

    payload = {
        "schema_version": 1,
        "catalog_version": "s4-2024v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Observed Sina actual-contract series + product_registry research specs",
        "entries": entries,
    }
    out_file = ROOT / output_path
    out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out_file} : {len(entries)} actual contracts")
    return out_file


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 S4 测试品种组合合约目录")
    parser.add_argument("--output", default="config/contract_catalog_s4_2024v1.json", help="输出路径")
    args = parser.parse_args()
    build_catalog(args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
