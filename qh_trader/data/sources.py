"""Public data adapters. Provider responses are observations, not verified exchange rule evidence."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone
from datetime import time as day_time
from decimal import Decimal, InvalidOperation
from typing import Any

from qh_trader.core.constants import Exchange
from qh_trader.core.objects import InstrumentId
from qh_trader.data.schemas import integer_value, parse_day, parse_time

logger = logging.getLogger(__name__)


def normalize_instrument_to_symbol(instrument: InstrumentId | str) -> tuple[str, str | None]:
    if isinstance(instrument, InstrumentId):
        symbol, exchange = instrument.symbol, instrument.exchange.value
    else:
        text = str(instrument).strip()
        if "." in text:
            prefix, symbol = text.split(".", 1)
            exchange = Exchange(prefix.upper()).value
        else:
            symbol, exchange = text, None
    if re.fullmatch(r"[A-Za-z]+\d{3}", symbol):
        raise ValueError("short codes must first be resolved using a historical catalog and as_of date")
    if not re.fullmatch(r"[A-Za-z]+\d{4}", symbol):
        raise ValueError("a resolved actual contract code is required")
    return symbol, exchange


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("boolean market price")
    number = Decimal(str(value))
    return number if number.is_finite() else None


def _quantity(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return integer_value(value, "source quantity")


def _within_day(value: str, start: date | str | None, end: date | str | None) -> bool:
    day = parse_day(value)
    return (start is None or day >= parse_day(start)) and (end is None or day <= parse_day(end))


def _within_time(
    value: str,
    start: datetime | date | str | None,
    end: datetime | date | str | None,
) -> bool:
    at = parse_time(value, "Asia/Shanghai")

    def boundary(item, is_end):
        is_day = isinstance(item, date) and not isinstance(item, datetime)
        is_day = is_day or (isinstance(item, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", item) is not None)
        if is_day:
            day = parse_day(item) + (timedelta(days=1) if is_end else timedelta())
            return parse_time(datetime.combine(day, day_time()), "Asia/Shanghai"), is_end
        return parse_time(item, "Asia/Shanghai"), False

    if start is not None and at < boundary(start, False)[0]:
        return False
    if end is not None:
        until, exclusive = boundary(end, True)
        if at >= until if exclusive else at > until:
            return False
    return True


class BaseDataSource(ABC):
    source_timezone: str | None = None

    @property
    @abstractmethod
    def source_id(self) -> str: ...

    @property
    @abstractmethod
    def source_name(self) -> str: ...

    @abstractmethod
    def fetch_daily_bars(self, instrument, start_date=None, end_date=None) -> list[dict[str, Any]]: ...

    @abstractmethod
    def fetch_minute_bars(self, instrument, period="60", start_time=None, end_time=None) -> list[dict[str, Any]]: ...

    @abstractmethod
    def fetch_latest_tick(self, instrument) -> dict[str, Any] | None: ...


class SinaFuturesDataSource(BaseDataSource):
    source_timezone = "Asia/Shanghai"
    DAILY_URL_TEMPLATE = (
        "https://stock2.finance.sina.com.cn/futures/api/jsonp.php//InnerFuturesNewService.getDailyKLine?symbol={symbol}"
    )
    MIN_URL_TEMPLATE = (
        "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
        "/InnerFuturesNewService.getFewMinLine?symbol={symbol}&type={period}"
    )
    REALTIME_URL_TEMPLATE = "https://hq.sinajs.cn/list=nf_{symbol}"

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout
        self.captures: list[dict[str, str]] = []

    @property
    def source_id(self) -> str:
        return "sina_futures"

    @property
    def source_name(self) -> str:
        return "新浪财经公开行情（字段与许可另行核验）"

    def _http_get(self, url: str, referer: str = "https://finance.sina.com.cn", encoding: str = "utf-8") -> str:
        if not url.startswith("https://"):
            raise ValueError("market data requests require verified HTTPS")
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": referer,
                "Accept": "*/*",
            },
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
                text = raw.decode(encoding)
                self.captures.append(
                    {
                        "url": url,
                        "received_at": datetime.now(timezone.utc).isoformat(),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "body": text,
                        "encoding": encoding,
                    }
                )
                return text
            except urllib.error.HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise
            except (urllib.error.URLError, TimeoutError):
                if attempt == 2:
                    raise
            time.sleep(0.25 * (attempt + 1))
        raise RuntimeError("market data retry loop exhausted")

    @staticmethod
    def _items(raw: str) -> list[dict[str, Any]]:
        match = re.search(r"(\[.*\])", raw, re.S)
        if match is None:
            raise ValueError("provider response does not contain a market data array")
        items = json.loads(match.group(1), parse_float=Decimal)
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError("invalid market data response structure")
        return items

    def fetch_daily_bars(self, instrument, start_date=None, end_date=None) -> list[dict[str, Any]]:
        symbol, _ = normalize_instrument_to_symbol(instrument)
        items = self._items(self._http_get(self.DAILY_URL_TEMPLATE.format(symbol=symbol)))
        records = []
        for item in items:
            if not _within_day(item["d"], start_date, end_date):
                continue
            records.append(
                {
                    "date": item["d"],
                    "open": _decimal(item.get("o")),
                    "high": _decimal(item.get("h")),
                    "low": _decimal(item.get("l")),
                    "close": _decimal(item.get("c")),
                    "volume": _quantity(item.get("v")),
                    "open_interest": _quantity(item.get("p")),
                    # This endpoint does not establish actual turnover or publication semantics.
                    "turnover": _decimal(item.get("turnover")),
                    "settlement_price": _decimal(item.get("s")),
                }
            )
        return records

    def fetch_minute_bars(self, instrument, period="60", start_time=None, end_time=None) -> list[dict[str, Any]]:
        periods = {"1": "1", "5": "5", "15": "15", "30": "30", "60": "60", "1h": "60"}
        if period not in periods:
            raise ValueError("unsupported minute period")
        symbol, _ = normalize_instrument_to_symbol(instrument)
        items = self._items(self._http_get(self.MIN_URL_TEMPLATE.format(symbol=symbol, period=periods[period])))
        records = []
        for item in items:
            if not _within_time(item["d"], start_time, end_time):
                continue
            records.append(
                {
                    "datetime": item["d"],
                    "open": _decimal(item.get("o")),
                    "high": _decimal(item.get("h")),
                    "low": _decimal(item.get("l")),
                    "close": _decimal(item.get("c")),
                    "volume": _quantity(item.get("v")),
                    "open_interest": _quantity(item.get("p")),
                    "turnover": _decimal(item.get("turnover")),
                }
            )
        return records

    def fetch_latest_tick(self, instrument) -> dict[str, Any] | None:
        symbol, _ = normalize_instrument_to_symbol(instrument)
        raw = self._http_get(self.REALTIME_URL_TEMPLATE.format(symbol=symbol.upper()), encoding="gbk")
        match = re.search(r'="([^"]*)"', raw)
        if match is None or not match.group(1):
            return None
        values = match.group(1).split(",")
        if len(values) < 18:
            return None
        try:
            return {
                "symbol": symbol,
                "name": values[0],
                "time": values[1],
                "date": values[17] or None,
                "open": _decimal(values[2]),
                "high": _decimal(values[3]),
                "low": _decimal(values[4]),
                "pre_close": _decimal(values[5]),
                "bid_price": _decimal(values[6]),
                "ask_price": _decimal(values[7]),
                "last_price": _decimal(values[8]),
                "settlement_price": _decimal(values[9]),
                "pre_settlement_price": _decimal(values[10]),
                "bid_volume": _quantity(values[11]),
                "ask_volume": _quantity(values[12]),
                "open_interest": _quantity(values[13]),
                "volume": _quantity(values[14]),
                "source_id": self.source_id,
                "receive_time": datetime.now(timezone.utc),
            }
        except (ValueError, IndexError, InvalidOperation):
            logger.warning("malformed public market snapshot; record rejected")
            return None


class AkShareDataSource(BaseDataSource):
    source_timezone = "Asia/Shanghai"

    def __init__(self) -> None:
        try:
            import akshare  # noqa: F401
        except ImportError as exc:
            raise ImportError("AkShare is required for this adapter") from exc

    @property
    def source_id(self) -> str:
        return "akshare"

    @property
    def source_name(self) -> str:
        return "AkShare / Sina 公开行情（字段与许可另行核验）"

    def fetch_daily_bars(self, instrument, start_date=None, end_date=None) -> list[dict[str, Any]]:
        import akshare as ak

        symbol, _ = normalize_instrument_to_symbol(instrument)
        frame = ak.futures_zh_daily_sina(symbol=symbol)
        if frame is None or frame.empty:
            return []
        records = []
        for raw in frame.to_dict(orient="records"):
            day = str(raw["date"])
            if not _within_day(day, start_date, end_date):
                continue
            records.append(
                {
                    "date": day,
                    **{name: _decimal(raw.get(name)) for name in ("open", "high", "low", "close", "turnover")},
                    "volume": _quantity(raw.get("volume")),
                    "open_interest": _quantity(raw.get("hold")),
                    "settlement_price": _decimal(raw.get("settle")),
                }
            )
        return records

    def fetch_minute_bars(self, instrument, period="60", start_time=None, end_time=None) -> list[dict[str, Any]]:
        import akshare as ak

        period = "60" if period == "1h" else period
        if period not in {"1", "5", "15", "30", "60"}:
            raise ValueError("unsupported minute period")
        symbol, _ = normalize_instrument_to_symbol(instrument)
        frame = ak.futures_zh_minute_sina(symbol=symbol, period=period)
        if frame is None or frame.empty:
            return []
        records = []
        for raw in frame.to_dict(orient="records"):
            stamp = str(raw["datetime"])
            if not _within_time(stamp, start_time, end_time):
                continue
            records.append(
                {
                    "datetime": stamp,
                    **{name: _decimal(raw.get(name)) for name in ("open", "high", "low", "close", "turnover")},
                    "volume": _quantity(raw.get("volume")),
                    "open_interest": _quantity(raw.get("hold")),
                }
            )
        return records

    def fetch_latest_tick(self, instrument) -> dict[str, Any] | None:
        return SinaFuturesDataSource().fetch_latest_tick(instrument)


def create_data_source(source_type: str = "sina", **kwargs: Any) -> BaseDataSource:
    if source_type.lower() in {"sina", "sina_futures"}:
        return SinaFuturesDataSource(**kwargs)
    if source_type.lower() == "akshare":
        if kwargs:
            raise TypeError("AkShare adapter does not accept Sina transport options")
        return AkShareDataSource()
    raise ValueError(f"unknown data source type: {source_type}")
