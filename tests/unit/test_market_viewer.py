"""看盘关键行为：周末/跨午夜映射、EMA初始化、只读HTTP资源边界。"""

import csv
import hashlib
import json
import threading
import urllib.error
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer

import pytest

from qh_trader.viewer.market_data import MarketArchive, display_time, with_ema
from scripts.serve_market_viewer import handler_for


def test_friday_night_and_saturday_midnight_belong_to_monday():
    days = [date(2026, 9, 18), date(2026, 9, 21)]
    evening = display_time("2026-09-21T21:30:00", days)
    midnight = display_time("2026-09-21T00:30:00", days)
    day = display_time("2026-09-21T09:30:00", days)
    assert evening.isoformat() == "2026-09-18T21:30:00+08:00"
    assert midnight.isoformat() == "2026-09-19T00:30:00+08:00"
    assert day.isoformat() == "2026-09-21T09:30:00+08:00"
    assert evening < midnight < day


def test_first_night_without_preceding_day_is_not_guessed():
    assert display_time("2026-09-21T21:30:00", [date(2026, 9, 21)]) is None
    assert display_time("2026-09-21T00:30:00", [date(2026, 9, 21)]) is None
    assert display_time("2026-09-21T09:30:00", [date(2026, 9, 21)]) is not None


def test_ema_has_no_future_information_and_uses_full_history():
    bars = [{"close": float(i)} for i in range(1, 251)]
    prefix = [dict(row) for row in bars[:100]]
    with_ema(bars)
    with_ema(prefix)
    assert bars[:100] == prefix
    assert bars[18]["ema20"] is None
    assert bars[19]["ema20"] == 10.5
    assert bars[20]["ema20"] == pytest.approx(11.5)
    assert bars[199]["ema200"] == 100.5


def test_http_only_exposes_whitelisted_files(tmp_path):
    class Archive:
        symbols = []
        summary = {"requested_start": "2021-10-07", "requested_end": "2026-10-07"}

        def bars(self, *args):
            raise KeyError("unknown")

    (tmp_path / "index.html").write_text("viewer", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("must not be served", encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(Archive(), tmp_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(base + "/", timeout=5) as response:
            assert response.read() == b"viewer"
        for path in ("/secret.txt", "/../config/settings.yaml", "/api/bars?symbol=../../secret"):
            with pytest.raises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(base + path, timeout=5)
            assert raised.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_extended_archive_replaces_only_matching_symbol(tmp_path):
    def write_archive(root, entries):
        datasets = []
        for code, dates in entries:
            folder = root / f"CZCE_{code}" / "1d"
            folder.mkdir(parents=True)
            path = folder / "bars.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["source_datetime", "close"])
                writer.writeheader()
                writer.writerows({"source_datetime": stamp + "T00:00:00", "close": "100"} for stamp in dates)
            datasets.append(
                {
                    "instrument": {"code": code, "name": code},
                    "interval": "1d",
                    "status": "downloaded",
                    "path": folder.relative_to(root).as_posix(),
                    "csv": {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
                }
            )
        (root / "summary.json").write_text(json.dumps({"datasets": datasets}), encoding="utf-8")

    base, extended = tmp_path / "base", tmp_path / "extended"
    write_archive(base, [("FGL9", ["2021-10-08"]), ("MAL9", ["2021-10-08"])])
    write_archive(extended, [("FGL9", ["2012-12-03", "2021-10-08"])])
    archive = MarketArchive(base, (extended,))
    by_code = {row["code"]: row for row in archive.symbols}
    assert set(by_code) == {"FGL9", "MAL9"}
    assert by_code["FGL9"]["firstDate"] == "2012-12-03"
    assert by_code["MAL9"]["firstDate"] == "2021-10-08"
