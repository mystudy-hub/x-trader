#!/usr/bin/env python
"""[Scripts 层] 启动只读本地历史行情页面：http://127.0.0.1:8765。"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.viewer.market_data import MarketArchive  # noqa: E402


def handler_for(archive: MarketArchive, web_root: Path):
    assets = {
        "/": "index.html",
        "/app.js": "app.js",
        "/drawings.js": "drawings.js",
        "/style.css": "style.css",
        "/vendor/lightweight-charts.js": "vendor/lightweight-charts-5.2.1.js",
    }

    class Handler(BaseHTTPRequestHandler):
        def send(self, status: int, payload: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(payload)

        def json(self, status: int, payload):
            self.send(
                status,
                json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(),
                "application/json; charset=utf-8",
            )

        def do_GET(self):
            url = urlsplit(self.path)
            try:
                if url.path == "/api/symbols":
                    self.json(
                        200,
                        {
                            "symbols": archive.symbols,
                            "requestedStart": archive.summary["requested_start"],
                            "requestedEnd": archive.summary["requested_end"],
                        },
                    )
                elif url.path == "/api/bars":
                    query = parse_qs(url.query)
                    self.json(200, archive.bars(query.get("symbol", [""])[0], query.get("interval", ["1d"])[0]))
                elif url.path in assets:
                    path = web_root / assets[url.path]
                    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                    self.send(200, path.read_bytes(), mime + "; charset=utf-8")
                else:
                    self.json(404, {"error": "未找到资源"})
            except KeyError:
                self.json(404, {"error": "未找到该品种或周期"})
            except (BrokenPipeError, ConnectionResetError):
                return
            except (ValueError, OSError) as exc:
                self.json(500, {"error": str(exc)})

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data_storage/raw/tdx_weighted/20261007_5y")
    args = parser.parse_args()
    index_path = ROOT / "data_storage/viewer_archives.json"
    additional = []
    if index_path.exists():
        index = json.loads(index_path.read_text(encoding="utf-8"))
        if index.get("schema_version") != 1:
            raise ValueError("unsupported viewer archive index")
        for value in index["archives"]:
            path = (index_path.parent / value).resolve()
            if not path.is_relative_to(index_path.parent.resolve()):
                raise ValueError("viewer archive index must stay within data_storage")
            additional.append(path)
    archive = MarketArchive(args.data_dir, tuple(additional))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(archive, ROOT / "web"))
    print(f"QH Trader: http://127.0.0.1:{args.port} | {len(archive.symbols)} series", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
