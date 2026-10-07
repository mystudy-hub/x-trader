"""[data 层] 通达信扩展行情的只读标准库协议客户端（S1-12，A03、A04）。

帧格式依据 docs/references/通达信扩展行情接入方案.md；其余响应偏移交叉参考
https://github.com/rainx/pytdx/tree/master/pytdx/parser/ex_get_*.py。
本模块独立编解码，不依赖 pytdx，不读取客户端私有文件。成交额和官方结算价
始终缺失；日线 price 仅保留服务端均价代理。逐笔只返回时刻，交易日由调用方
使用项目日历确定，绝不使用本机日期或把查询日期伪装成成交自然日。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import socket
import struct
import threading
import time as clock
import zlib
from collections.abc import Callable, Mapping, Sequence
from datetime import date as calendar_date
from datetime import datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

# 方案初稿误重复了 16 字节；公开 ex_setup_commands.py 的真实请求为 92 字节，
# 等于 10 字节请求头 + 两个长度字段声明的 0x52 字节 body。
TDX_EXHQ_HANDSHAKE_REQ = bytes.fromhex(
    "01 01 48 65 00 01 52 00 52 00 54 24 1f 32 c6 e5"
    "d5 3d fb 41 1f 32 c6 e5 d5 3d fb 41 1f 32 c6 e5"
    "d5 3d fb 41 1f 32 c6 e5 d5 3d fb 41 1f 32 c6 e5"
    "d5 3d fb 41 1f 32 c6 e5 d5 3d fb 41 1f 32 c6 e5"
    "d5 3d fb 41 1f 32 c6 e5 d5 3d fb 41 cc e1 6d ff"
    "d5 ba 3f b8 cb c5 7a 05 4f 77 48 ea"
)
TDX_MARKET_MAP = {28: "CZCE", 29: "DCE", 30: "SHFE", 47: "CFFEX", 66: "GFEX", 42: "INDEX", 65: "SPREAD"}
TDX_CATEGORY_MAP = {0: "5m", 1: "15m", 2: "30m", 3: "1h", 4: "1d", 5: "1w", 6: "1M", 7: "1m", 8: "1m"}
DEFAULT_TDX_SERVERS = (("116.205.143.214", 7727),)
MAX_FRAME_SIZE = 65535
MAX_BAR_COUNT = 700


class DataFormatError(ValueError):
    """协议字段、记录长度或压缩内容不符合可验证格式。"""


def _integer(value: object, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be an integer in {minimum}..{maximum}: {value!r}")
    return value


def _servers(values: Sequence[tuple[str, int] | Mapping[str, Any]]) -> tuple[tuple[str, int], ...]:
    result = []
    for value in values:
        if isinstance(value, Mapping):
            host, port = value.get("host"), value.get("port", 7727)
        elif isinstance(value, (tuple, list)) and len(value) == 2:
            host, port = value
        else:
            raise ValueError("each TDX server must contain host and port")
        if not isinstance(host, str) or not host.strip() or host != host.strip():
            raise ValueError("TDX server host must be a nonempty string without surrounding whitespace")
        result.append((host, _integer(port, "port", 1, 65535)))
    if not result:
        raise ValueError("at least one TDX server is required")
    return tuple(result)


def load_tdx_servers(path: str | Path) -> list[tuple[str, int]]:
    """读取 JSON 兼容 YAML 节点文件，维持协议模块零第三方依赖。"""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("TDX server config must use JSON-compatible YAML") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("servers"), list):
        raise ValueError("TDX server config requires a servers list")
    return list(_servers(payload["servers"]))


def _text(raw: bytes, field: str, *, encoding: str = "gbk") -> str:
    try:
        return raw.split(b"\0", 1)[0].decode(encoding)
    except UnicodeDecodeError as exc:
        raise DataFormatError(f"invalid {field} encoding") from exc


def _price(value: float, field: str) -> Decimal:
    if not math.isfinite(value):
        raise DataFormatError(f"non-finite {field}")
    return Decimal(str(value))


def _records(payload: bytes, offset: int, width: int, *, max_count: int | None = None) -> tuple[int, memoryview]:
    if len(payload) < offset + 2:
        raise DataFormatError("truncated response record count")
    count = struct.unpack_from("<H", payload, offset)[0]
    if max_count is not None and count > max_count:
        raise DataFormatError(f"response count {count} exceeds requested count {max_count}")
    expected = offset + 2 + count * width
    if len(payload) != expected:
        raise DataFormatError(f"response length {len(payload)} does not match {count} records ({expected} bytes)")
    return count, memoryview(payload)[offset + 2 :]


def _clock(minutes: int, seconds: int = 0) -> time:
    try:
        return time(minutes // 60, minutes % 60, seconds)
    except ValueError as exc:
        raise DataFormatError(f"invalid protocol time minutes={minutes}, seconds={seconds}") from exc


def _bar_datetime(raw: bytes | memoryview, category: int) -> datetime:
    try:
        if category in (4, 5, 6):
            value = struct.unpack_from("<I", raw)[0]
            return datetime(value // 10000, value % 10000 // 100, value % 100)
        zipday, minutes = struct.unpack_from("<HH", raw)
        stamp = _clock(minutes)
        return datetime((zipday >> 11) + 2004, (zipday % 2048) // 100, (zipday % 2048) % 100, stamp.hour, stamp.minute)
    except ValueError as exc:
        raise DataFormatError("invalid bar datetime") from exc


class TdxExHqClient:
    """有界传输与多节点重连；全部命令只读，断连后允许重放请求。

    retries 为每轮服务器列表之外的额外重试轮数。capture_callback 可接收每次
    完整交换的原始帧及哈希；默认关闭，客户端不累积历史帧。
    """

    def __init__(
        self,
        servers: Sequence[tuple[str, int] | Mapping[str, Any]] | None = None,
        *,
        timeout: float = 5.0,
        retries: int = 1,
        capture_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.servers = _servers(DEFAULT_TDX_SERVERS if servers is None else servers)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a finite positive number")
        self.timeout = float(timeout)
        self.retries = _integer(retries, "retries", 0, 10)
        self.capture_callback = capture_callback
        self.endpoint: tuple[str, int] | None = None
        self._socket: socket.socket | None = None
        self._next_server = 0
        self._lock = threading.RLock()

    def __enter__(self) -> TdxExHqClient:
        return self.connect()

    def __exit__(self, *_: object) -> None:
        self.close()

    def connect(self) -> TdxExHqClient:
        with self._lock:
            if self._socket is not None:
                return self
            last_error: Exception | None = None
            for _ in range(len(self.servers) * (self.retries + 1)):
                endpoint = self.servers[self._next_server % len(self.servers)]
                self._next_server += 1
                try:
                    self._socket = socket.create_connection(endpoint, timeout=self.timeout)
                    self.endpoint = endpoint
                    self._exchange(TDX_EXHQ_HANDSHAKE_REQ, "handshake")
                    return self
                except (OSError, ConnectionError, DataFormatError) as exc:
                    last_error = exc
                    self.close()
            if isinstance(last_error, DataFormatError):
                raise last_error
            raise ConnectionError("all configured TDX servers failed to connect or handshake") from last_error

    def close(self) -> None:
        with self._lock:
            if self._socket is not None:
                try:
                    self._socket.close()
                finally:
                    self._socket = None
            self.endpoint = None

    def _recv_exact(self, count: int, deadline: float) -> bytes:
        if not 0 <= count <= MAX_FRAME_SIZE:
            raise DataFormatError("response read exceeds protocol size bound")
        assert self._socket is not None
        result = bytearray()
        while len(result) < count:
            remaining = deadline - clock.monotonic()
            if remaining <= 0:
                raise ConnectionError("TDX response deadline exceeded")
            self._socket.settimeout(remaining)
            piece = self._socket.recv(count - len(result))
            if not piece:
                raise ConnectionError(f"TDX response truncated: received {len(result)} of {count} bytes")
            result.extend(piece)
        return bytes(result)

    def _exchange(self, request: bytes, command: str) -> bytes:
        assert self._socket is not None
        self._socket.settimeout(self.timeout)
        deadline = clock.monotonic() + self.timeout
        self._socket.sendall(request)
        header = self._recv_exact(16, deadline)
        _, _, _, zip_size, unzip_size = struct.unpack("<IIIHH", header)
        payload = self._recv_exact(zip_size, deadline)
        if self.capture_callback is not None:
            response = header + payload
            self.capture_callback(
                {
                    "command": command,
                    "endpoint": list(self.endpoint) if self.endpoint else None,
                    "request_hex": request.hex(),
                    "response_hex": response.hex(),
                    "request_sha256": hashlib.sha256(request).hexdigest(),
                    "response_sha256": hashlib.sha256(response).hexdigest(),
                }
            )
        if zip_size == unzip_size:
            return payload
        try:
            decoder = zlib.decompressobj()
            decoded = decoder.decompress(payload, unzip_size + 1)
        except zlib.error as exc:
            raise DataFormatError("invalid zlib response") from exc
        if len(decoded) != unzip_size or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
            raise DataFormatError("zlib response length or stream boundary mismatch")
        return decoded

    def _request(self, command: str, cmd: int, flag: int, body: bytes, *, seq: int = 1) -> bytes:
        frame = struct.pack("<BBHBBHH", 1, seq, cmd, flag, 1, len(body), len(body)) + body
        with self._lock:
            last_error: Exception | None = None
            for _ in range(len(self.servers) * (self.retries + 1)):
                self.connect()
                try:
                    return self._exchange(frame, command)
                except DataFormatError:
                    self.close()
                    raise
                except (OSError, ConnectionError) as exc:
                    last_error = exc
                    self.close()
            raise ConnectionError(f"TDX {command} failed after reconnect attempts") from last_error

    @staticmethod
    def _instrument(market: int, code: str) -> bytes:
        if isinstance(market, bool) or not isinstance(market, int) or market not in TDX_MARKET_MAP:
            raise ValueError(f"unsupported TDX market: {market!r}")
        if not isinstance(code, str) or re.fullmatch(r"[A-Z0-9]{1,9}", code) is None:
            raise ValueError("TDX code must contain 1..9 uppercase ASCII letters or digits")
        return struct.pack("<B9s", market, code.encode("ascii"))

    @staticmethod
    def _identity(payload: bytes, market: int, code: str) -> None:
        if len(payload) < 10:
            raise DataFormatError("truncated response instrument")
        actual_market, actual_code = struct.unpack_from("<B9s", payload)
        if actual_market != market or _text(actual_code, "code", encoding="ascii") != code:
            raise DataFormatError("response instrument does not match request")

    def get_markets(self) -> list[dict[str, Any]]:
        payload = self._request("markets", 0x6948, 0, b"\xf4\x23", seq=2)
        count, records = _records(payload, 0, 64)
        result = []
        for index in range(count):
            category, name, market, short_name = struct.unpack_from("<B32sB2s", records, index * 64)
            if market or category:
                result.append(
                    {
                        "market": market,
                        "category": category,
                        "name": _text(name, "name"),
                        "short_name": _text(short_name, "short_name"),
                    }
                )
        return result

    def get_instrument_count(self) -> int:
        payload = self._request("instrument_count", 0x6648, 0, b"\xf0\x23", seq=3)
        if len(payload) < 23:
            raise DataFormatError("truncated instrument count response")
        return struct.unpack_from("<I", payload, 19)[0]

    def get_instruments(self, start: int = 0, count: int = 100) -> list[dict[str, Any]]:
        _integer(start, "start", 0, 0xFFFFFFFF)
        _integer(count, "count", 1, (MAX_FRAME_SIZE - 6) // 64)
        payload = self._request("instruments", 0x6748, 0, b"\xf5\x23" + struct.pack("<IH", start, count), seq=4)
        ret_count, records = _records(payload, 4, 64, max_count=count)
        if struct.unpack_from("<I", payload)[0] != start:
            raise DataFormatError("instrument directory start does not match request")
        result = []
        for index in range(ret_count):
            category, market, code, name, description = struct.unpack_from("<BB3x9s17s9s", records, index * 64)
            result.append(
                {
                    "category": category,
                    "market": market,
                    "code": _text(code, "code", encoding="ascii"),
                    "name": _text(name, "name"),
                    "desc": _text(description, "description"),
                }
            )
        return result

    get_instrument_info = get_instruments

    def get_instrument_bars(
        self, category: int, market: int, code: str, start: int = 0, count: int = MAX_BAR_COUNT
    ) -> list[dict[str, Any]]:
        instrument = self._instrument(market, code)
        if isinstance(category, bool) or not isinstance(category, int) or category not in TDX_CATEGORY_MAP:
            raise ValueError(f"unsupported TDX category: {category!r}")
        _integer(start, "start", 0, 0xFFFFFFFF)
        _integer(count, "count", 1, MAX_BAR_COUNT)
        body = b"\xff\x23" + instrument + struct.pack("<HHIH", category, 1, start, count)
        payload = self._request("bars", 0x6A08, 1, body)
        # 2026-10-06 实录 RB2410：无记录也保留一个全零 32 字节占位，
        # 且 instrument 后的 42 字节全部为零。只接纳此已核实的精确布局。
        if len(payload) == 52 and payload[10:] == b"\0" * 42:
            self._identity(payload, market, code)
            return []
        ret_count, records = _records(payload, 18, 32, max_count=count)
        self._identity(payload, market, code)
        result = []
        for index in range(ret_count):
            record = records[index * 32 : (index + 1) * 32]
            open_price, high, low, close, position, volume, price = struct.unpack_from("<ffffIIf", record, 4)
            result.append(
                {
                    "datetime": _bar_datetime(record, category),
                    "open": _price(open_price, "open"),
                    "high": _price(high, "high"),
                    "low": _price(low, "low"),
                    "close": _price(close, "close"),
                    "open_interest": position,
                    "volume": volume,
                    "price": _price(price, "price proxy"),
                    "turnover": None,
                    "settlement_price": None,
                }
            )
        return result

    def get_instrument_quote(self, market: int, code: str) -> list[dict[str, Any]]:
        payload = self._request("quote", 0x0208, 2, b"\xfa\x23" + self._instrument(market, code))
        # 服务端无报价响应可以为空或仅包含 10 字节 instrument + 4 字节状态。
        if not payload:
            return []
        self._identity(payload, market, code)
        if len(payload) == 14:
            return []
        if len(payload) < 150:
            raise DataFormatError("truncated quote response")
        values = struct.unpack_from("<5f9I5f5I5f5I", payload, 14)
        result: dict[str, Any] = {"market": market, "code": code, "turnover": None, "settlement_price": None}
        for field, value in zip(("pre_close", "open", "high", "low", "price"), values[:5], strict=True):
            result[field] = _price(value, field)
        result.update(
            {
                "opening_volume": values[5],
                "volume": values[7],
                "last_volume": values[8],
                "inside_volume": values[10],
                "outside_volume": values[11],
                "open_interest": values[13],
            }
        )
        for level in range(5):
            result[f"bid{level + 1}"] = _price(values[14 + level], "bid")
            result[f"bid_vol{level + 1}"] = values[19 + level]
            result[f"ask{level + 1}"] = _price(values[24 + level], "ask")
            result[f"ask_vol{level + 1}"] = values[29 + level]
        return [result]

    def _transactions(self, payload: bytes, market: int, code: str, count: int) -> list[dict[str, Any]]:
        self._identity(payload, market, code)
        ret_count, records = _records(payload, 14, 16, max_count=count)
        result = []
        for index in range(ret_count):
            minutes, price, volume, change, nature = struct.unpack_from("<HIIiH", records, index * 16)
            mark, second = divmod(nature, 10000)
            result.append(
                {
                    "time": _clock(minutes, second),
                    "price": Decimal(price) / Decimal(1000),
                    "volume": volume,
                    "open_interest_change": change,
                    "nature": nature,
                    "nature_mark": mark,
                    "direction": 1 if mark == 0 else -1 if mark == 1 else 0,
                    "turnover": None,
                }
            )
        return result

    def get_transaction_data(self, market: int, code: str, start: int = 0, count: int = 700) -> list[dict[str, Any]]:
        instrument = self._instrument(market, code)
        _integer(start, "start", 0, 0x7FFFFFFF)
        _integer(count, "count", 1, (MAX_FRAME_SIZE - 16) // 16)
        body = b"\xfc\x23" + instrument + struct.pack("<iH", start, count)
        return self._transactions(self._request("transactions", 0x0008, 3, body), market, code, count)

    def get_history_transaction_data(
        self, market: int, code: str, date: int, start: int = 0, count: int = 700
    ) -> list[dict[str, Any]]:
        instrument = self._instrument(market, code)
        _integer(start, "start", 0, 0x7FFFFFFF)
        _integer(count, "count", 1, (MAX_FRAME_SIZE - 16) // 16)
        _integer(date, "date", 10000101, 99991231)
        try:
            calendar_date(date // 10000, date % 10000 // 100, date % 100)
        except ValueError as exc:
            raise ValueError("date must be a valid YYYYMMDD query date") from exc
        body = b"\x06\x24" + struct.pack("<I", date) + instrument + struct.pack("<iH", start, count)
        return self._transactions(self._request("history_transactions", 0x0030, 2, body), market, code, count)

    def get_minute_time_data(self, market: int, code: str) -> list[dict[str, Any]]:
        payload = self._request("minute_time", 0x0008, 1, b"\x0b\x24" + self._instrument(market, code), seq=7)
        self._identity(payload, market, code)
        count, records = _records(payload, 10, 18)
        result = []
        for index in range(count):
            minutes, price, average, volume, position = struct.unpack_from("<HffII", records, index * 18)
            result.append(
                {
                    "time": _clock(minutes),
                    "price": _price(price, "price"),
                    "avg_price": _price(average, "average price proxy"),
                    "volume": volume,
                    "open_interest": position,
                    "turnover": None,
                    "settlement_price": None,
                }
            )
        return result
