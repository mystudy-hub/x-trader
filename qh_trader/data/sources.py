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

from qh_trader.core.constants import Exchange, MarketPhase
from qh_trader.core.objects import InstrumentId
from qh_trader.data.calendar import CHINA_TZ, TradingCalendar
from qh_trader.data.schemas import integer_value, parse_day, parse_time
from qh_trader.data.tdx_aggregate import aggregate_tdx_minutes

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
            except (urllib.error.URLError, TimeoutError, OSError, Exception):
                if attempt == 2:
                    raise
            time.sleep(0.5 * (attempt + 1))
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


class TushareFuturesDataSource(BaseDataSource):
    """基于 Tushare Pro 官方接口的期货数据源.

    特点:
    - 拥有官方权威的成交量 (vol) 与真实成交额 (amount, 万元 -> 元).
    - 包含官方每日结算价 (settle) 与前结算价 (pre_settle).
    - 提供合约基础元数据 (fut_basic)，用于构建标准历史合约目录.
    """

    DEFAULT_TOKEN = "cpg1VYJ6aBKU2SLkWCZcOEuAUbDSEPLT6Kvgw9f36JJyLl3BOZsiItzx"
    DEFAULT_HTTP_URL = "https://api.886018.xyz"
    source_timezone = "Asia/Shanghai"

    def __init__(
        self,
        token: str | None = None,
        http_url: str = DEFAULT_HTTP_URL,
        timeout: float = 15.0,
    ) -> None:
        import os

        try:
            import tushare as ts
        except ImportError as exc:
            raise ImportError("Tushare is required for this adapter: uv pip install tushare") from exc

        # 确保直连镜像 API 地址，避免受本机网络代理干扰发生连接重置
        no_proxy = os.environ.get("NO_PROXY", "")
        if "886018.xyz" not in no_proxy:
            os.environ["NO_PROXY"] = f"{no_proxy},api.886018.xyz,886018.xyz".strip(",")

        self.token = token or os.environ.get("TUSHARE_TOKEN") or self.DEFAULT_TOKEN
        self.http_url = http_url
        self.timeout = timeout
        self.captures: list[dict[str, str]] = []

        self.pro = ts.pro_api(self.token)
        # 设置自定义代理/镜像地址
        self.pro._DataApi__http_url = self.http_url

    @property
    def source_id(self) -> str:
        return "tushare"

    @property
    def source_name(self) -> str:
        return "Tushare Pro 期货官方数据源"

    @staticmethod
    def to_tushare_code(instrument: InstrumentId | str) -> str:
        """将系统 InstrumentId 转换为 Tushare 合约代码格式.

        例:
            SHFE.rb2410 -> RB2410.SHF
            CZCE.FG2501 -> FG2501.ZCE
            DCE.m2409   -> M2409.DCE
            CFFEX.IF2409 -> IF2409.CFX
            INE.sc2409   -> SC2409.INE
            GFEX.si2409  -> SI2409.GFE
        """
        if isinstance(instrument, InstrumentId):
            sym = instrument.symbol.upper()
            ex = instrument.exchange
        else:
            text = str(instrument).strip()
            if "." in text:
                prefix, sym_str = text.split(".", 1)
                ex = Exchange(prefix.upper())
                sym = sym_str.upper()
            else:
                sym = text.upper()
                p_lower = "".join(c for c in sym.lower() if c.isalpha())
                if p_lower in {"fg", "ta", "ma", "sa", "sr", "cf", "oi", "rm", "ur", "pk"}:
                    ex = Exchange.CZCE
                elif p_lower in {"m", "y", "a", "b", "p", "c", "i", "j", "jm", "pp", "l", "v", "eg", "eb", "pg"}:
                    ex = Exchange.DCE
                elif p_lower in {"if", "ih", "ic", "im", "tf", "t", "ts", "tl"}:
                    ex = Exchange.CFFEX
                elif p_lower in {"sc", "nr", "lu", "bc", "ec"}:
                    ex = Exchange.INE
                elif p_lower in {"si", "lc"}:
                    ex = Exchange.GFEX
                else:
                    ex = Exchange.SHFE

        exchange_suffixes = {
            Exchange.SHFE: "SHF",
            Exchange.DCE: "DCE",
            Exchange.CZCE: "ZCE",
            Exchange.CFFEX: "CFX",
            Exchange.INE: "INE",
            Exchange.GFEX: "GFE",
        }
        suffix = exchange_suffixes.get(ex, "SHF")
        return f"{sym}.{suffix}"

    def fetch_daily_bars(
        self,
        instrument: InstrumentId | str,
        start_date: date | str | None = None,
        end_date: date | str | None = None,
    ) -> list[dict[str, Any]]:
        ts_code = self.to_tushare_code(instrument)

        # 格式化日期为 YYYYMMDD
        def format_d(d: date | str | None) -> str | None:
            if not d:
                return None
            return str(d).replace("-", "")

        s_str = format_d(start_date)
        e_str = format_d(end_date)

        kwargs: dict[str, Any] = {"ts_code": ts_code}
        if s_str:
            kwargs["start_date"] = s_str
        if e_str:
            kwargs["end_date"] = e_str

        df = self.pro.fut_daily(**kwargs)
        body_str = df.to_json(orient="records", date_format="iso") if df is not None and not df.empty else "[]"
        raw_bytes = body_str.encode("utf-8")
        self.captures.append(
            {
                "url": f"{self.http_url}/fut_daily?ts_code={ts_code}",
                "received_at": datetime.now(timezone.utc).isoformat(),
                "sha256": hashlib.sha256(raw_bytes).hexdigest(),
                "body": body_str,
                "encoding": "utf-8",
            }
        )
        if df is None or df.empty:
            return []

        # Tushare 默认按日期降序排列，需反转为升序
        df = df.sort_values(by="trade_date", ascending=True)

        records: list[dict[str, Any]] = []
        for raw in df.to_dict(orient="records"):
            raw_d = str(raw["trade_date"])
            iso_date = f"{raw_d[:4]}-{raw_d[4:6]}-{raw_d[6:8]}"
            if not _within_day(iso_date, start_date, end_date):
                continue

            # amount 单位为万元，换算为元
            amount_val = raw.get("amount")
            if amount_val is not None and not (isinstance(amount_val, float) and (amount_val != amount_val)):
                turnover = Decimal(str(amount_val)) * Decimal("10000")
            else:
                turnover = Decimal("0.00")

            records.append(
                {
                    "date": iso_date,
                    "open": _decimal(raw.get("open")),
                    "high": _decimal(raw.get("high")),
                    "low": _decimal(raw.get("low")),
                    "close": _decimal(raw.get("close")),
                    "volume": _quantity(raw.get("vol")),
                    "turnover": turnover,
                    "open_interest": _quantity(raw.get("oi")),
                    "settlement_price": _decimal(raw.get("settle")),
                    "pre_settlement_price": _decimal(raw.get("pre_settle")),
                }
            )
        return records

    def fetch_minute_bars(
        self,
        instrument: InstrumentId | str,
        period: str = "60",
        start_time: datetime | str | None = None,
        end_time: datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        # 分钟线权限若未单独开通，自动平滑 fallback 到新浪/东财分钟线
        sina_fallback = SinaFuturesDataSource()
        return sina_fallback.fetch_minute_bars(instrument, period=period, start_time=start_time, end_time=end_time)

    def fetch_latest_tick(self, instrument: InstrumentId | str) -> dict[str, Any] | None:
        return SinaFuturesDataSource().fetch_latest_tick(instrument)

    def fetch_contract_catalog(self, exchange: str = "SHFE") -> list[dict[str, Any]]:
        """获取指定交易所的合约目录元数据 (fut_basic)."""
        df = self.pro.fut_basic(exchange=exchange.upper())
        body_str = df.to_json(orient="records", date_format="iso") if df is not None and not df.empty else "[]"
        raw_bytes = body_str.encode("utf-8")
        self.captures.append(
            {
                "url": f"{self.http_url}/fut_basic?exchange={exchange.upper()}",
                "received_at": datetime.now(timezone.utc).isoformat(),
                "sha256": hashlib.sha256(raw_bytes).hexdigest(),
                "body": body_str,
                "encoding": "utf-8",
            }
        )
        if df is None or df.empty:
            return []
        return df.to_dict(orient="records")


class TdxPaginationError(ValueError):
    """已到分页安全上限或分页不再向历史推进，禁止静默发布截断历史。"""


class TdxExHqDataSource(BaseDataSource):
    """[Data 层] 通达信扩展行情研究数据源 (S1-12, FR-DATA-02, FR-DATA-08)。

    只接受已解析的实际合约；主连仅可通过底层协议探测。注入的 client 由调用方
    管理连接，默认客户端每次完整抓取后关闭。超过 1m 的周期均从 1m 显式会话聚合。
    """

    source_timezone = "Asia/Shanghai"
    MARKET_MAP = {
        Exchange.CZCE: 28,
        Exchange.DCE: 29,
        Exchange.SHFE: 30,
        Exchange.CFFEX: 47,
        Exchange.GFEX: 66,
    }

    def __init__(
        self,
        *,
        client: Any = None,
        calendar: TradingCalendar | None = None,
        servers=None,
        timeout: float = 5.0,
        page_size: int = 700,
        max_pages: int = 1000,
        market_overrides: dict[Exchange, int] | None = None,
    ) -> None:
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 700:
            raise ValueError("TDX page_size must be between 1 and 700")
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 10000:
            raise ValueError("TDX max_pages must be between 1 and 10000")
        self.client = client
        self.calendar = calendar
        self.servers = servers
        self.timeout = timeout
        self.page_size = page_size
        self.max_pages = max_pages
        self.captures: list[dict[str, Any]] = []
        self.market_map = dict(self.MARKET_MAP)
        for exchange, market in (market_overrides or {}).items():
            if not isinstance(exchange, Exchange) or isinstance(market, bool) or not isinstance(market, int):
                raise TypeError("TDX market overrides require Exchange keys and integer market IDs")
            if not 0 <= market <= 255:
                raise ValueError("TDX market ID must fit uint8")
            self.market_map[exchange] = market

    @property
    def source_id(self) -> str:
        return "tdx_exhq"

    @property
    def source_name(self) -> str:
        return "通达信扩展行情（研究补充；缺成交额和官方结算价）"

    @property
    def research_metadata(self) -> dict[str, Any]:
        return {
            "research_only": True,
            "missing_fields": ["turnover", "official_settlement_price"],
            "settlement_semantics": "settlement_proxy is TDX calculated average; never official Settlement",
            "continuous_series": "rejected by data source; unadjusted L8 only available through protocol/probe",
            "expired_contracts": "not guaranteed; empty response cannot establish historical coverage",
            "minute_timestamp": "end of one-minute interval, Asia/Shanghai",
            "aggregation": "1m only; explicit continuous sessions; incomplete and short tail buckets discarded",
            "live_alignment": "same session-anchored full windows; unlike live aggregation, short tails are dropped",
            "date_filter": "explicit session trading days" if self.calendar is not None else "local calendar dates",
            "calendar_version": self.calendar.version if self.calendar is not None else None,
            "market_mapping": {exchange.value: market for exchange, market in self.market_map.items()},
            "max_pages": self.max_pages,
            "page_size": self.page_size,
        }

    def to_tdx_code(self, instrument: InstrumentId | str) -> tuple[int, str]:
        """已解析郑商所四位年月在协议端改为三位；不从短码或品种猜交易所。"""
        symbol, exchange_name = normalize_instrument_to_symbol(instrument)
        if exchange_name is None:
            raise ValueError("TDX requires an explicit exchange and resolved actual contract")
        exchange = Exchange(exchange_name)
        if exchange not in self.market_map:
            raise ValueError(f"TDX market for {exchange.value} is unverified; supply an evidence-based market override")
        if not 1 <= int(symbol[-2:]) <= 12:
            raise ValueError("actual contract delivery month must be between 01 and 12")
        symbol = symbol.upper()
        if exchange == Exchange.CZCE:
            symbol = symbol[:-4] + symbol[-3:]
        return self.market_map[exchange], symbol

    def _new_client(self):
        from qh_trader.data.tdx_exhq import TdxExHqClient

        options = {"timeout": self.timeout, "capture_callback": self.captures.append}
        if self.servers is not None:
            options["servers"] = self.servers
        return TdxExHqClient(**options)

    @staticmethod
    def _is_day(value: date | datetime | str | None) -> bool:
        is_date = isinstance(value, date) and not isinstance(value, datetime)
        return is_date or (isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is not None)

    @classmethod
    def _boundary(cls, value: date | datetime | str | None, *, end: bool = False) -> datetime | None:
        if value is None:
            return None
        if cls._is_day(value):
            day = parse_day(value) + (timedelta(days=1) if end else timedelta())
            stamp = parse_time(datetime.combine(day, day_time()), "Asia/Shanghai")
            return stamp - timedelta(microseconds=1) if end else stamp
        return parse_time(value, "Asia/Shanghai")

    def _fetch_pages(self, instrument, category: int, lower: datetime | None) -> list[dict[str, Any]]:
        market, code = self.to_tdx_code(instrument)
        client = self.client if self.client is not None else self._new_client()
        owned = self.client is None
        records: dict[datetime, dict[str, Any]] = {}
        previous_oldest: datetime | None = None
        offset = 0
        try:
            if owned:
                client.connect()
            for _ in range(self.max_pages):
                response = client.get_instrument_bars(category, market, code, offset, self.page_size)
                if len(response) > self.page_size:
                    raise ValueError("TDX server returned more rows than requested")
                if not response:
                    break
                page_stamps = []
                for raw in response:
                    row = dict(raw)
                    stamp = parse_time(row["datetime"], self.source_timezone)
                    row["datetime"] = stamp
                    if stamp in records and records[stamp] != row:
                        raise ValueError(f"conflicting TDX observations at {stamp.isoformat()}")
                    records[stamp] = row
                    page_stamps.append(stamp)
                oldest = min(page_stamps)
                if previous_oldest is not None and oldest >= previous_oldest:
                    raise TdxPaginationError("TDX pagination stopped advancing; historical completeness is unknown")
                if lower is not None and oldest <= lower:
                    break
                # 短页不能证明已到底；按实际返回数推进，直到空页或覆盖请求起点。
                offset += len(response)
                previous_oldest = oldest
            else:
                raise TdxPaginationError(
                    f"TDX history truncated at {self.max_pages} pages; narrow the date range or increase max_pages"
                )
        finally:
            if owned:
                client.close()
        return [records[stamp] for stamp in sorted(records)]

    def fetch_daily_bars(self, instrument, start_date=None, end_date=None) -> list[dict[str, Any]]:
        if start_date is not None and end_date is not None and parse_day(start_date) > parse_day(end_date):
            raise ValueError("requested dates are reversed")
        records = []
        for raw in self._fetch_pages(instrument, 4, self._boundary(start_date)):
            day = raw["datetime"].astimezone(CHINA_TZ).date().isoformat()
            if not _within_day(day, start_date, end_date):
                continue
            records.append(
                {
                    "date": day,
                    **{name: raw[name] for name in ("open", "high", "low", "close", "volume", "open_interest")},
                    "turnover": None,
                    "settlement_price": None,
                    "settlement_proxy": raw.get("price"),
                }
            )
        return records

    def fetch_minute_bars(self, instrument, period="60", start_time=None, end_time=None) -> list[dict[str, Any]]:
        normalized = str(period).removesuffix("m")
        normalized = "60" if normalized == "1h" else normalized
        if normalized not in {"1", "5", "15", "30", "60"}:
            raise ValueError("unsupported TDX minute period")
        minutes = int(normalized)
        if minutes > 1 and self.calendar is None:
            raise ValueError("TDX minute aggregation requires --calendar; download raw 1m without a calendar")
        lower, upper = self._boundary(start_time), self._boundary(end_time, end=True)
        symbol, exchange = normalize_instrument_to_symbol(instrument)
        if exchange is None:
            raise ValueError("TDX requires an explicit exchange and resolved actual contract")
        inst = instrument if isinstance(instrument, InstrumentId) else InstrumentId(Exchange(exchange), symbol)
        first_day = parse_day(start_time) if self._is_day(start_time) and self.calendar is not None else None
        last_day = parse_day(end_time) if self._is_day(end_time) and self.calendar is not None else None
        if first_day is not None and last_day is not None and first_day > last_day:
            raise ValueError("requested times are reversed")
        if self.calendar is not None:
            # 日期参数指交易日：周一夜盘可能始于周五，不能使用自然午夜截掉这段记录。
            for day in (first_day, last_day):
                if day is not None:
                    self.calendar.is_trading_day(day)  # 同时检查版本和覆盖边界。
            if first_day is not None or last_day is not None:
                days = sorted(
                    day
                    for day in self.calendar.trading_days
                    if (first_day is None or day >= first_day) and (last_day is None or day <= last_day)
                )
                if not days:
                    return []
                if first_day is not None:
                    lower = min(
                        session.start
                        for session in self.calendar.sessions_for_day(inst, days[0])
                        if session.phase == MarketPhase.CONTINUOUS and session.permissions.match
                    )
                if last_day is not None:
                    upper = max(
                        session.end
                        for session in self.calendar.sessions_for_day(inst, days[-1])
                        if session.phase == MarketPhase.CONTINUOUS and session.permissions.match
                    )
        if lower is not None and upper is not None and lower > upper:
            raise ValueError("requested times are reversed")
        # 多取一个周期，让 datetime 起点落在桶中部时仍有完整的首桶输入。
        fetch_lower = lower - timedelta(minutes=minutes) if lower is not None and minutes > 1 else lower
        if first_day is not None:
            fetch_lower = lower + timedelta(minutes=1)
        rows = []
        for raw in self._fetch_pages(instrument, 7, fetch_lower):
            stamp = raw["datetime"]
            if (fetch_lower is not None and stamp < fetch_lower) or (upper is not None and stamp > upper):
                continue
            rows.append(
                {
                    "datetime": stamp,
                    "bar_start": stamp - timedelta(minutes=1),
                    "bar_end": stamp,
                    **{name: raw[name] for name in ("open", "high", "low", "close", "volume", "open_interest")},
                    "turnover": None,
                    "settlement_price": None,
                }
            )
        if self.calendar is not None:
            rows = aggregate_tdx_minutes(rows, instrument=inst, calendar=self.calendar, period=minutes)
        return [
            row
            for row in rows
            if (lower is None or row["datetime"] >= lower)
            and (first_day is None or row["trading_day"] >= first_day)
            and (last_day is None or row["trading_day"] <= last_day)
        ]

    def fetch_latest_tick(self, instrument) -> dict[str, Any] | None:
        market, code = self.to_tdx_code(instrument)
        client = self.client if self.client is not None else self._new_client()
        owned = self.client is None
        try:
            if owned:
                client.connect()
            quotes = client.get_instrument_quote(market, code)
            if not quotes:
                return None
            if len(quotes) != 1:
                raise ValueError("TDX quote response must identify exactly one requested contract")
            quote = dict(quotes[0])
            quote.update(turnover=None, settlement_price=None, source_id=self.source_id, research_only=True)
            quote["last_price"] = quote["price"]
            return quote
        finally:
            if owned:
                client.close()


def create_data_source(source_type: str = "sina", **kwargs: Any) -> BaseDataSource:
    st = source_type.lower().strip()
    if st in {"sina", "sina_futures"}:
        return SinaFuturesDataSource(**kwargs)
    if st == "akshare":
        if kwargs:
            raise TypeError("AkShare adapter does not accept Sina transport options")
        return AkShareDataSource()
    if st == "tushare":
        return TushareFuturesDataSource(**kwargs)
    if st in {"tdx", "tdx_exhq"}:
        return TdxExHqDataSource(**kwargs)
    raise ValueError(f"unknown data source type: {source_type}")
