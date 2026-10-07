"""加权历史下载：真实标签的分页交错、日期过滤与重复记录冲突。"""

import csv
from argparse import Namespace
from datetime import date, datetime
from decimal import Decimal

import pytest

from scripts import download_weighted_history as download


def bar(stamp, close="100"):
    return {
        "datetime": datetime.fromisoformat(stamp),
        "open": Decimal("100"),
        "high": Decimal("101"),
        "low": Decimal("99"),
        "close": Decimal(close),
        "volume": 10,
        "open_interest": 20,
        "turnover": None,
        "settlement_price": None,
        "price": Decimal("100"),
    }


@pytest.mark.parametrize("all_history", [False, True])
def test_short_pages_and_interleaved_night_labels_reach_date_boundary(monkeypatch, tmp_path, all_history):
    pages = {
        0: [bar("2026-09-30T09:30:00"), bar("2026-09-30T10:00:00")],
        2: [bar("2026-09-30T23:00:00"), bar("2026-09-29T15:00:00")],
        4: [bar("2026-09-28T15:00:00")],
        5: [],
    }
    calls = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get_instrument_bars(self, category, market, code, start, count):
            calls.append(start)
            return list(pages[start])

    monkeypatch.setattr(download, "WeightedClient", Client)
    monkeypatch.setattr(download.time, "sleep", lambda seconds: None)
    args = Namespace(
        start=date.min if all_history else date(2026, 9, 29),
        end=date(2026, 9, 30),
        max_pages=10,
        all_history=all_history,
    )
    item = {"market": 30, "code": "RBL9", "name": "螺纹加权"}
    result = download.download_one(item, "30m", args, tmp_path, [])
    assert result["status"] == "downloaded", result.get("error")
    assert calls == ([0, 2, 4, 5, 0] if all_history else [0, 2, 4, 0])
    assert result["history_reaches_requested_start"] == (not all_history)
    assert result["termination"] == ("source_empty_page" if all_history else "requested_start_reached")
    assert result["record_count"] == (5 if all_history else 4)
    with (tmp_path / result["path"] / "bars.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["source_datetime"] == ("2026-09-28T15:00:00" if all_history else "2026-09-29T15:00:00")
    assert rows[-1]["source_datetime"] == "2026-09-30T23:00:00"
    assert all(row["turnover"] == row["settlement_price"] == "" for row in rows)


def test_export_preserves_invalid_and_rejects_conflicting_duplicate(tmp_path):
    first = bar("2026-09-30T09:30:00", "102")
    issues = download.validate_page([first], None)
    assert issues[0]["code"] == "INVALID_OHLC"
    page = download.save_page(tmp_path / "first.json.gz", {"records": [first], "quality_issues": issues})
    meta = {
        "requested_start": "2026-09-30",
        "requested_end": "2026-09-30",
        "pages": [page],
        "interval": "30m",
        "instrument": {"market": 30, "code": "RBL9", "name": "螺纹加权"},
    }
    download.export_csv(tmp_path, meta)
    assert meta["invalid_ohlc_count"] == 1
    conflict = download.save_page(
        tmp_path / "second.json.gz",
        {
            "records": [bar("2026-09-30T09:30:00")],
            "quality_issues": [],
        },
    )
    meta["pages"].append(conflict)
    with pytest.raises(ValueError, match="conflicting source records"):
        download.export_csv(tmp_path, meta)


def test_same_page_cannot_be_repeated_as_older_history():
    rows = [bar("2026-09-30T09:30:00")]
    with pytest.raises(ValueError, match="advance backward"):
        download.validate_page(rows, rows[0]["datetime"])


def test_weighted_scope_and_exact_monthly_code_exceptions():
    assert download.is_weighted({"market": 30, "code": "RBL9", "name": "螺纹加权"})
    assert not download.is_weighted({"market": 42, "code": "RBL9", "name": "海外加权"})
    assert not download.is_weighted({"market": 30, "code": "RBL8", "name": "螺纹主连"})
    assert download.exchange({"market": 30, "code": "SCL9"}) == "INE"
    assert len(download.WeightedClient._instrument(29, "PP-FL9")) == 10
    with pytest.raises(ValueError):
        download.WeightedClient._instrument(29, "XX-FL9")
