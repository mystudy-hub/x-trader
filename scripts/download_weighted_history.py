#!/usr/bin/env python
"""[Scripts 层] 批量归档通达信国内期货加权日线/原生 30m，保留来源边界和逐页证据。"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from datetime import time as day_time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.data.tdx_exhq import (  # noqa: E402
    TDX_MARKET_MAP,
    DataFormatError,
    TdxExHqClient,
    load_tdx_servers,
)

DOMESTIC = {28, 29, 30, 47, 66}
INE_CODES = {"BCL9", "ECL9", "LUL9", "NRL9", "SCL9"}
MONTHLY_CODES = {"L-FL9", "PP-FL9", "V-FL9"}
FIELDS = [
    "exchange",
    "market",
    "code",
    "name",
    "interval",
    "source_datetime",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_interest",
    "turnover",
    "settlement_price",
    "source_price_proxy",
    "quality_flags",
]


def encode(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(type(value).__name__)


def json_bytes(value) -> bytes:
    return json.dumps(value, default=encode, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(json_bytes(value))
    os.replace(temporary, path)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save_page(path: Path, payload) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(json_bytes(payload), compresslevel=5, mtime=0))
    return {"path": path.name, "sha256": digest(path)}


class WeightedClient(TdxExHqClient):
    """仅本研究入口兼容已登记的三个月均加权代码及目录末尾占位。"""

    @staticmethod
    def _instrument(market, code):
        if market == 29 and code in MONTHLY_CODES:
            return struct.pack("<B9s", market, code.encode("ascii"))
        return TdxExHqClient._instrument(market, code)

    def directory_page(self, start: int, count: int = 1000) -> list[dict]:
        payload = self._request("instruments", 0x6748, 0, b"\xf5\x23" + struct.pack("<IH", start, count), seq=4)
        if len(payload) < 6:
            raise DataFormatError("truncated directory header")
        actual_start, actual_count = struct.unpack_from("<IH", payload)
        if actual_start == start and actual_count == 0 and len(payload) == 70 and payload[6:] == bytes(64):
            return []
        if actual_start != start or actual_count > count or len(payload) != 6 + actual_count * 64:
            raise DataFormatError("directory framing mismatch")
        result = []
        for index in range(actual_count):
            category, market, code, name, desc = struct.unpack_from("<BB3x9s17s9s", payload, 6 + index * 64)
            row = {"category": category, "market": market, "code": code.split(b"\0", 1)[0].decode("ascii")}
            for field, raw in (("name", name), ("desc", desc)):
                raw = raw.split(b"\0", 1)[0]
                try:
                    row[field] = raw.decode("gbk")
                except UnicodeDecodeError as exc:
                    # 服务端定长字段截去 GBK 尾字节；原帧保留，标记而不伪造名称。
                    if exc.reason != "incomplete multibyte sequence" or exc.start != len(raw) - 1:
                        raise
                    row[field] = raw[: exc.start].decode("gbk") + "\ufffd"
                    row[field + "_encoding_warning"] = raw.hex()
            result.append(row)
        return result


def exchange(item: dict) -> str:
    return "INE" if item["market"] == 30 and item["code"] in INE_CODES else TDX_MARKET_MAP[item["market"]]


def is_weighted(item: dict) -> bool:
    return item["market"] in DOMESTIC and item["code"].endswith("L9") and "加权" in item["name"]


def inventory(output: Path, servers) -> dict:
    path = output / "inventory.json"
    if path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
        for entry in saved["pages"]:
            if digest(output / entry["path"]) != entry["sha256"]:
                raise ValueError("inventory page hash mismatch")
        return saved
    frames, selected, pages, target_pages = [], {}, [], {}
    with WeightedClient(servers, timeout=10, retries=2, capture_callback=frames.append) as client:
        before = client.get_instrument_count()
        offset = 0
        for number in range(300):
            frames.clear()
            rows = client.directory_page(offset)
            page_path = output / "inventory_pages" / f"{number:04d}.json.gz"
            entry = save_page(page_path, {"offset": offset, "records": rows, "wire_frames": frames})
            entry["path"] = page_path.relative_to(output).as_posix()
            pages.append(entry)
            if not rows:
                break
            targets = [row for row in rows if is_weighted(row)]
            if targets:
                target_pages[offset] = hashlib.sha256(json_bytes(rows)).hexdigest()
            for item in targets:
                key = item["market"], item["code"]
                if key in selected and selected[key] != item:
                    raise ValueError("conflicting weighted directory entries")
                selected[key] = item
            offset += len(rows)
        else:
            raise ValueError("directory pagination limit reached")
        after = client.get_instrument_count()
        if before != after:
            raise ValueError("directory changed during enumeration; retry with a new output directory")
        for start, expected in target_pages.items():
            if hashlib.sha256(json_bytes(client.directory_page(start))).hexdigest() != expected:
                raise ValueError("weighted directory page changed during enumeration")
    if not selected:
        raise ValueError("no domestic futures weighted series found")
    result = {
        "created_at": datetime.now(timezone.utc),
        "source_id": "tdx_exhq",
        "declared_count": before,
        "retrieved_count": offset,
        "count_discrepancy": before - offset,
        "scope": "server-enumerable domestic futures weighted series; not exchange-complete history",
        "weighted": [selected[key] for key in sorted(selected)],
        "pages": pages,
        "weighted_pages_rechecked": len(target_pages),
    }
    save_json(path, result)
    return result


def validate_page(rows: list[dict], previous_oldest: datetime | None) -> list[dict]:
    rows.sort(key=lambda row: row["datetime"])
    stamps = [row["datetime"] for row in rows]
    if len(stamps) != len(set(stamps)):
        raise ValueError("duplicate source timestamps within page")
    # 来源按交易时序分页，但夜盘日期是交易日标签；同一天的昼夜标签可能跨页交错。
    # 只要求最早标签继续后退，跨页重复键在导出时核对全部字段，冲突不得覆盖。
    if rows and previous_oldest is not None and stamps[0] >= previous_oldest:
        raise ValueError("pages fail to advance backward")
    issues = []
    for row in rows:
        prices = [row[key] for key in ("open", "high", "low", "close")]
        if any(not value.is_finite() for value in prices):
            raise ValueError("non-finite source price")
        opening, high, low, close = prices
        if min(prices) <= 0 or not low <= min(opening, close) <= max(opening, close) <= high:
            issues.append({"source_datetime": row["datetime"], "code": "INVALID_OHLC"})
        if any(type(row[key]) is not int or row[key] < 0 for key in ("volume", "open_interest")):
            raise ValueError("invalid source quantity")
    return issues


def export_csv(directory: Path, meta: dict) -> None:
    written = invalid = 0
    start, end = meta["requested_start"], meta["requested_end"]
    csv_path = directory / "bars.csv"
    records, invalid_times = {}, set()
    duplicates = 0
    for entry in meta["pages"]:
        page = json.loads(gzip.decompress((directory / entry["path"]).read_bytes()))
        invalid_times.update(row["source_datetime"] for row in page["quality_issues"])
        for row in page["records"]:
            stamp = row["datetime"]
            if not start <= stamp[:10] <= end:
                continue
            if stamp in records:
                if row != records[stamp]:
                    raise ValueError(f"conflicting source records at {stamp}")
                duplicates += 1
            records[stamp] = row
    meta["identical_duplicate_records"] = duplicates
    with csv_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        for stamp in sorted(records):
            row = records[stamp]
            flags = ["TURNOVER_UNAVAILABLE"]
            if meta["interval"] == "30m":
                flags.append("TIME_SEMANTICS_UNVERIFIED")
            if stamp in invalid_times:
                flags.append("INVALID")
                invalid += 1
            item = meta["instrument"]
            writer.writerow(
                {
                    "exchange": exchange(item),
                    "market": item["market"],
                    "code": item["code"],
                    "name": item["name"],
                    "interval": meta["interval"],
                    "source_datetime": stamp,
                    **{key: row[key] for key in ("open", "high", "low", "close", "volume", "open_interest")},
                    "turnover": None,
                    "settlement_price": None,
                    "source_price_proxy": row.get("price"),
                    "quality_flags": "|".join(flags),
                }
            )
            if written == 0:
                meta["first_source_datetime"] = stamp
            meta["last_source_datetime"] = stamp
            written += 1
    meta.update(record_count=written, invalid_ohlc_count=invalid, csv={"path": "bars.csv", "sha256": digest(csv_path)})


def download_one(item: dict, interval: str, args, output: Path, servers) -> dict:
    directory = output / f"{exchange(item)}_{item['code']}" / interval
    path = directory / "manifest.json"
    if path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("status") == "downloaded" and (saved["requested_start"], saved["requested_end"]) == (
            str(args.start),
            str(args.end),
        ):
            for entry in saved["pages"] + [saved["csv"]]:
                if digest(directory / entry["path"]) != entry["sha256"]:
                    raise ValueError("existing dataset hash mismatch")
            return saved
    meta = {
        "instrument": item,
        "interval": interval,
        "requested_start": str(args.start),
        "requested_end": str(args.end),
        "status": "downloading",
        "created_at": datetime.now(timezone.utc),
        "pages": [],
        "first_source_datetime": None,
        "last_source_datetime": None,
        "record_count": 0,
        "invalid_ohlc_count": 0,
        "path": directory.relative_to(output).as_posix(),
        "source_id": "tdx_exhq",
        "research_only": True,
        "timestamp_semantics": "source labels, not normalized event times; night dates and native buckets unverified",
        "missing_fields": ["turnover", "official_settlement_price"],
        "series_semantics": "provider weighted index; weighting formula unverified; not an executable contract",
        "history_reaches_requested_start": False,
    }
    save_json(path, meta)
    frames = []
    boundary = None if getattr(args, "all_history", False) else datetime.combine(args.start, day_time())
    with WeightedClient(servers, timeout=10, retries=2, capture_callback=frames.append) as client:
        try:
            offset, oldest, first_digest = 0, None, None
            category = 4 if interval == "1d" else 2
            for number in range(args.max_pages):
                frames.clear()
                rows = client.get_instrument_bars(category, item["market"], item["code"], offset, 700)
                issues = validate_page(rows, oldest)
                if first_digest is None:
                    first_digest = hashlib.sha256(json_bytes(rows)).hexdigest()
                page_path = directory / "pages" / f"{number:05d}.json.gz"
                entry = save_page(
                    page_path,
                    {
                        "offset": offset,
                        "requested_count": 700,
                        "received_at": datetime.now(timezone.utc),
                        "records": rows,
                        "quality_issues": issues,
                        "wire_frames": frames,
                    },
                )
                entry.update(path=page_path.relative_to(directory).as_posix(), record_count=len(rows), offset=offset)
                meta["pages"].append(entry)
                if not rows:
                    meta["termination"] = "source_empty_page"
                    break
                oldest = rows[0]["datetime"]
                if boundary is not None and oldest <= boundary:
                    meta.update(termination="requested_start_reached", history_reaches_requested_start=True)
                    break
                offset += len(rows)
                time.sleep(0.025)
            else:
                raise ValueError("pagination cap reached; request is incomplete")
            frames.clear()
            checked = client.get_instrument_bars(category, item["market"], item["code"], 0, 700)
            checked.sort(key=lambda row: row["datetime"])
            save_page(directory / "latest_page_recheck.json.gz", {"records": checked, "wire_frames": frames})
            if hashlib.sha256(json_bytes(checked)).hexdigest() != first_digest:
                raise ValueError("latest page changed during download; stable snapshot required")
            meta["latest_page_rechecked"] = True
            export_csv(directory, meta)
            meta.update(status="downloaded", completed_at=datetime.now(timezone.utc))
        except (OSError, ValueError, ConnectionError) as exc:
            meta.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    save_json(path, meta)
    return meta


def summarize(output: Path, inv: dict, results: list[dict], args) -> dict:
    results.sort(key=lambda row: (row["instrument"]["market"], row["instrument"]["code"], row["interval"]))
    summary = {
        "requested_start": str(args.start),
        "requested_end": str(args.end),
        "series_count": len(inv["weighted"]),
        "dataset_count": len(results),
        "failed_count": sum(row["status"] != "downloaded" for row in results),
        "by_interval": {},
        "datasets": results,
        "inventory_count_discrepancy": inv["count_discrepancy"],
        "all_history": getattr(args, "all_history", False),
    }
    flat = []
    for row in results:
        item = row["instrument"]
        flat.append(
            {
                "exchange": exchange(item),
                "code": item["code"],
                "name": item["name"],
                **{
                    key: row[key]
                    for key in (
                        "interval",
                        "status",
                        "record_count",
                        "first_source_datetime",
                        "last_source_datetime",
                        "invalid_ohlc_count",
                        "history_reaches_requested_start",
                        "path",
                    )
                },
            }
        )
    with (output / "coverage.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(flat[0]))
        writer.writeheader()
        writer.writerows(flat)
    for interval in ("1d", "30m"):
        entries = [row for row in results if row["interval"] == interval and row["status"] == "downloaded"]
        combined = output / f"all_weighted_{interval}.csv.gz"
        count = invalid = pages = 0
        with gzip.open(combined, "wt", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            for row in entries:
                folder = output / row["path"]
                for entry in row["pages"] + [row["csv"]]:
                    if digest(folder / entry["path"]) != entry["sha256"]:
                        raise ValueError("artifact hash failed delivery verification")
                pages += len(row["pages"])
                written = 0
                with (folder / "bars.csv").open(encoding="utf-8-sig", newline="") as source:
                    for record in csv.DictReader(source):
                        if record["code"] != row["instrument"]["code"] or record["interval"] != interval:
                            raise ValueError("export identity mismatch")
                        writer.writerow(record)
                        count += 1
                        written += 1
                        invalid += "INVALID" in record["quality_flags"].split("|")
                if written != row["record_count"]:
                    raise ValueError("export count mismatch")
        summary["by_interval"][interval] = {
            "downloaded_series": len(entries),
            "record_count": count,
            "invalid_ohlc_count": invalid,
            "history_reaches_start_count": sum(row["history_reaches_requested_start"] for row in entries),
            "source_end_count": sum(row.get("termination") == "source_empty_page" for row in entries),
            "first_date": min((row["first_source_datetime"] for row in entries if row["record_count"]), default=None),
            "last_date": max((row["last_source_datetime"] for row in entries if row["record_count"]), default=None),
            "verified_page_files": pages,
            "combined_path": combined.name,
            "combined_sha256": digest(combined),
        }
    save_json(output / "summary.json", summary)
    coverage_label = "已读到来源末尾的序列" if getattr(args, "all_history", False) else "历史已覆盖请求起点的序列"
    lines = [
        "# 国内期货加权指数：日线与原生30分钟线",
        "",
        (
            f"请求范围：来源可取得的全部历史，截至 {args.end}；源：通达信扩展行情。"
            if getattr(args, "all_history", False)
            else f"请求范围：{args.start} 至 {args.end}（包含端点）；源：通达信扩展行情。"
        ),
        f"目录列出 {len(inv['weighted'])} 个加权序列，下载任务 {len(results)} 个，失败 {summary['failed_count']} 个。",
        "",
        f"| 周期 | 已下载序列 | 记录数 | {coverage_label} | 来源最新标签 | OHLC异常 |",
        "| :--- | ---: | ---: | ---: | :--- | ---: |",
    ]
    for interval, row in summary["by_interval"].items():
        covered = row["source_end_count"] if getattr(args, "all_history", False) else row["history_reaches_start_count"]
        lines.append(
            f"| {interval} | {row['downloaded_series']} | {row['record_count']:,} | "
            f"{covered} | {row['last_date']} | {row['invalid_ohlc_count']} |"
        )
    lines += [
        "",
        "- coverage.csv：逐序列、逐周期的实际起止、根数与覆盖标记。",
        "- all_weighted_1d.csv.gz / all_weighted_30m.csv.gz：按序列及来源时间排序的合并 CSV。",
        "- <交易所>_<代码>/<周期>/bars.csv：单序列 CSV；同目录含清单及带报文的分页归档。",
        "- inventory.json / inventory_pages：本次目录及来源计数差异的证据。",
        "",
        "起点不足五年可能来自上市较晚或服务器历史保留限制，不能据此断言上市日期；所有来源可取记录已归档。",
        "仅表示翻页已到请求起点或来源空页，不承诺交易所全历史、无中间缺口或全部品种均有五年数据。",
        "30m 是服务端原生周期，source_datetime 原样保留；夜盘日期与跨休市分桶尚未统一，不可直接当自然时刻回测。",
        "成交额和官方结算价缺失保持空白；source_price_proxy 不是官方结算价。异常原样保留并标记 INVALID。",
        "加权指数不是可成交实际合约；三个含连字符的代码为月均价加权，额外保留。市场30中的INE序列已单列INE。",
        f"服务器目录声明与实际枚举相差 {inv['count_discrepancy']} 条，国内加权所在页已复核未变。",
        "",
    ]
    (output / "README.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    history = parser.add_mutually_exclusive_group(required=True)
    history.add_argument("--start", type=date.fromisoformat)
    history.add_argument(
        "--all-history", action="store_true", help="page until source is empty, without a start cutoff"
    )
    parser.add_argument("--codes", nargs="+", help="optional weighted codes, e.g. FGL9")
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--max-pages", type=int, default=1000)
    args = parser.parse_args(argv)
    if args.all_history:
        args.start = date.min
    if args.start > args.end or not 1 <= args.workers <= 4 or not 1 <= args.max_pages <= 10000:
        parser.error("invalid dates, workers or page bound")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    request = {"start": str(args.start), "end": str(args.end), "intervals": ["1d", "30m"]}
    if args.all_history:
        request["all_history"] = True
    if args.codes:
        request["codes"] = sorted(set(args.codes))
    request_path = output / "request.json"
    if request_path.exists() and json.loads(request_path.read_text(encoding="utf-8")) != request:
        raise ValueError("output directory belongs to a different request")
    save_json(request_path, request)
    servers = load_tdx_servers(ROOT / "config/tdx_exhq_servers.yaml")
    inv = inventory(output, servers)
    if args.codes:
        codes = set(args.codes)
        selected = [item for item in inv["weighted"] if item["code"] in codes]
        missing = codes - {item["code"] for item in selected}
        if missing:
            parser.error(f"weighted codes absent from directory: {sorted(missing)}")
        inv = {**inv, "weighted": selected}
    print(
        json.dumps({"inventory": len(inv["weighted"]), "directory_count_discrepancy": inv["count_discrepancy"]}),
        flush=True,
    )
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending = [
            pool.submit(download_one, item, interval, args, output, servers)
            for item in inv["weighted"]
            for interval in ("1d", "30m")
        ]
        for future in as_completed(pending):
            row = future.result()
            results.append(row)
            save_json(output / "progress.json", results)
            print(
                json.dumps(
                    {
                        "done": len(results),
                        "code": row["instrument"]["code"],
                        "interval": row["interval"],
                        "status": row["status"],
                        "count": row["record_count"],
                        "error": row.get("error"),
                    }
                ),
                flush=True,
            )
    summary = summarize(output, inv, results, args)
    print(
        json.dumps(
            {"output": str(output), "failed": summary["failed_count"], "intervals": summary["by_interval"]},
            ensure_ascii=False,
        ),
        flush=True,
    )
    return int(summary["failed_count"] > 0)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
