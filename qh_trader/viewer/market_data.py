"""将原始加权归档转换为看盘数据；不改写归档，不提供交易执行价格。"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
from bisect import bisect_left
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

CHINA_TZ = ZoneInfo("Asia/Shanghai")


def display_time(label: str, trading_days: list[date]) -> datetime | None:
    """TDX 夜盘挂在交易日标签下；用来源实际交易日推导自然日期，不按自然日减一。"""
    stamp = datetime.fromisoformat(label)
    day = stamp.date()
    if time(6) <= stamp.time() < time(20):
        return stamp.replace(tzinfo=CHINA_TZ)
    index = bisect_left(trading_days, day)
    if index == 0 or index == len(trading_days) or trading_days[index] != day:
        return None
    previous = trading_days[index - 1]
    actual = previous if stamp.hour >= 20 else previous + timedelta(days=1)
    return datetime.combine(actual, stamp.time(), CHINA_TZ)


def with_ema(bars: list[dict]) -> None:
    for period in (20, 50, 200):
        value = None
        total = 0.0
        for index, bar in enumerate(bars):
            if index < period:
                total += bar["close"]
                if index == period - 1:
                    value = total / period
            else:
                value = bar["close"] * (2 / (period + 1)) + value * (1 - 2 / (period + 1))
            bar[f"ema{period}"] = value


class MarketArchive:
    def __init__(self, root: Path, additional_archives: tuple[Path, ...] = ()):
        self.root = root.resolve()
        self.summary = json.loads((self.root / "summary.json").read_text(encoding="utf-8"))
        self.datasets = {}
        self.dataset_roots = {}
        self.symbols = []
        self._read = lru_cache(maxsize=8)(self._read)
        self.bars = lru_cache(maxsize=8)(self.bars)
        for archive_root in (self.root, *additional_archives):
            archive_root = archive_root.resolve()
            summary = json.loads((archive_root / "summary.json").read_text(encoding="utf-8"))
            for row in summary["datasets"]:
                if row["status"] != "downloaded":
                    continue
                folder = (archive_root / row["path"]).resolve()
                if not folder.is_relative_to(archive_root):
                    raise ValueError("dataset path outside archive")
                exchange = row["path"].split("_")[0]
                key = f"{exchange}.{row['instrument']['code']}"
                self.datasets[(key, row["interval"])] = row
                self.dataset_roots[(key, row["interval"])] = archive_root
        for key, interval in self.datasets:
            if interval != "1d":
                continue
            row = self.datasets[(key, interval)]
            daily = self._read(key, "1d")
            if not daily:
                continue
            current = float(daily[-1]["close"])
            previous = float(daily[-2]["close"]) if len(daily) > 1 else current
            precision = 0 if all(float(r["close"]).is_integer() for r in daily[-20:]) else 2
            self.symbols.append(
                {
                    "id": key,
                    "exchange": key.split(".")[0],
                    "code": row["instrument"]["code"],
                    "name": row["instrument"]["name"],
                    "last": current,
                    "change": current - previous,
                    "changePct": (current / previous - 1) * 100 if previous else 0,
                    "date": daily[-1]["source_datetime"][:10],
                    "precision": precision,
                    "intervals": [p for p in ("1d", "30m") if (key, p) in self.datasets],
                    "firstDate": daily[0]["source_datetime"][:10],
                }
            )

    def _read(self, symbol: str, interval: str) -> list[dict]:
        meta = self.datasets[(symbol, interval)]
        path = self.dataset_roots[(symbol, interval)] / meta["path"] / "bars.csv"
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != meta["csv"]["sha256"]:
                raise ValueError("行情文件哈希不符，请重新核验数据归档")
        with path.open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))

    def _trading_days(self, symbol: str) -> list[date]:
        days = {date.fromisoformat(row["source_datetime"][:10]) for row in self._read(symbol, "1d")}
        # 日期过滤前的最后一页可提供区间首日的前一交易日，避免猜测节假日夜盘日期。
        meta = self.datasets[(symbol, "1d")]
        archive_root = self.dataset_roots[(symbol, "1d")]
        for entry in reversed(meta["pages"]):
            path = (archive_root / meta["path"] / entry["path"]).resolve()
            if not path.is_relative_to(archive_root):
                raise ValueError("page path outside archive")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                raise ValueError("原始日线分页校验失败")
            rows = json.loads(gzip.decompress(raw))["records"]
            days.update(date.fromisoformat(row["datetime"][:10]) for row in rows)
            if rows:
                break
        return sorted(days)

    def bars(self, symbol: str, interval: str) -> dict:
        if interval not in {"1d", "30m"} or (symbol, interval) not in self.datasets:
            raise KeyError("未找到该品种或周期")
        raw = self._read(symbol, interval)
        days = self._trading_days(symbol) if interval == "30m" else []
        bars, invalid, unknown = [], 0, 0
        times = set()
        for row in raw:
            values = {field: float(row[field]) for field in ("open", "high", "low", "close")}
            if "INVALID" in row["quality_flags"] or not all(math.isfinite(v) and v > 0 for v in values.values()):
                invalid += 1
                continue
            label = row["source_datetime"]
            if interval == "1d":
                chart_time, actual = label[:10], None
            else:
                actual = display_time(label, days)
                if actual is None:
                    unknown += 1
                    continue
                chart_time = int(actual.timestamp())
            if chart_time in times:
                raise ValueError("时间映射出现重复，请核验来源时间口径")
            times.add(chart_time)
            bars.append(
                {
                    "time": chart_time,
                    **values,
                    "volume": int(row["volume"]),
                    "openInterest": int(row["open_interest"]),
                    "tradingDay": label[:10],
                    "sourceLabel": label,
                    "naturalTime": actual.isoformat() if actual else label[:10],
                    "session": "夜盘" if actual and (actual.hour >= 20 or actual.hour < 6) else "日盘",
                }
            )
        bars.sort(key=lambda row: row["time"])
        with_ema(bars)
        expected_days = {row["source_datetime"][:10] for row in self._read(symbol, "1d")}
        present_days = {bar["tradingDay"] for bar in bars}
        missing_days = sorted(expected_days - present_days) if interval == "30m" else []
        return {
            "symbol": symbol,
            "interval": interval,
            "bars": bars,
            "sourceCount": len(raw),
            "excludedInvalid": invalid,
            "excludedUnknownTime": unknown,
            "missingTradingDays": len(missing_days),
            "missingDayRange": [missing_days[0], missing_days[-1]] if missing_days else [],
            "timePolicy": "夜盘按来源前一交易日映射自然日期，凌晨接续该夜盘；显示时区为上海。此映射仅用于历史浏览。",
            "source": "通达信 · 加权指数",
            "snapshotDate": self.summary["requested_end"],
        }
