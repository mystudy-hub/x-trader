#!/usr/bin/env python
"""[脚本工具] SimNow 只读预检与运行目录生成 (S5-05, FR-LIVE-04, FR-DATA-02)。

仅登录、结算确认、查询和订阅；不装配交易执行服务，不报单、不撤单。
合约来自柜台目录，活跃度来自本次行情；费率原始字段不自动升级为核验规则。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.core.constants import EventKind, Exchange, QualityFlag  # noqa: E402
from qh_trader.core.objects import InstrumentId, Tick  # noqa: E402
from qh_trader.data.storage import ParquetDataStorage  # noqa: E402
from qh_trader.gateway.ctp_gateway import CtpOrderRefBook, CtpTraderGateway, load_ctp_binding  # noqa: E402
from qh_trader.gateway.ctp_market import CtpMarketDataGateway, CtpMarketSettings  # noqa: E402
from qh_trader.gateway.ctp_query import (  # noqa: E402
    QUERY_LOCAL_REJECT_CODE,
    QUERY_TIMEOUT_CODE,
    QUERY_TRANSPORT_CODE,
    TERMINAL_STATUSES,
    CtpQueryAdapter,
)
from qh_trader.gateway.feedback_normalizer import build_normalizer  # noqa: E402
from scripts import ctp_setup  # noqa: E402
from scripts.ctp_probe import RecordingSink, mask_identifier  # noqa: E402

RATE_FIELDS = {
    "commission": (
        "InstrumentID",
        "ExchangeID",
        "InvestorRange",
        "BizType",
        "OpenRatioByMoney",
        "OpenRatioByVolume",
        "CloseRatioByMoney",
        "CloseRatioByVolume",
        "CloseTodayRatioByMoney",
        "CloseTodayRatioByVolume",
    ),
    "margin": (
        "InstrumentID",
        "ExchangeID",
        "InvestorRange",
        "HedgeFlag",
        "IsRelative",
        "LongMarginRatioByMoney",
        "LongMarginRatioByVolume",
        "ShortMarginRatioByMoney",
        "ShortMarginRatioByVolume",
    ),
}
RATE_REQUESTS = {
    "commission": ("CThostFtdcQryInstrumentCommissionRateField", "ReqQryInstrumentCommissionRate"),
    "margin": ("CThostFtdcQryInstrumentMarginRateField", "ReqQryInstrumentMarginRate"),
}
CONTRACT_FIELDS = (
    "InstrumentID",
    "ExchangeID",
    "ProductID",
    "ProductClass",
    "VolumeMultiple",
    "PriceTick",
    "OpenDate",
    "ExpireDate",
    "IsTrading",
    "InstLifePhase",
    "DeliveryYear",
    "DeliveryMonth",
)


def registered_front_pair(profile: Mapping[str, Any], trade: str | None, market: str | None) -> tuple[str, str]:
    """仅允许登记的 SimNow 主机和交易/行情配对，拒绝任意覆盖到实盘前置。"""
    if profile.get("profile_name") != "simnow_v6":
        raise ValueError("preflight only supports the registered simnow_v6 profile")
    fronts = profile.get("fronts") or {}
    registered = {fronts.get("trade"), *(fronts.get("alternatives") or ())}
    pairs = {
        f"tcp://182.254.243.31:{port}": f"tcp://182.254.243.31:{port + 10}" for port in (30001, 30002, 30003, 40001)
    }
    trade = trade or fronts.get("trade")
    if trade not in registered or trade not in pairs:
        raise ValueError("trade front is not an active registered SimNow candidate")
    market = market or pairs[trade]
    if market != pairs[trade]:
        raise ValueError("market front must match the registered SimNow trade-front pair")
    return trade, market


class RawRateQueries:
    """本脚本独立收集费率回调，按请求号和终止标志收口，字段白名单不包含用户信息。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[int, dict[str, Any]] = {}

    def on_response(self, kind, record, info, request_id, is_last) -> None:
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None or pending["kind"] != kind:
                return
            code = getattr(info, "ErrorID", 0) if info is not None else 0
            if code:
                pending["error_code"] = int(code)
            if record is not None:
                pending["records"].append(
                    {name: getattr(record, name) for name in RATE_FIELDS[kind] if hasattr(record, name)}
                )
            if is_last:
                pending["done"].set()

    def query(self, channel, settings, kind: str, symbol: str, *, timeout: float, interval_ms: int) -> dict[str, Any]:
        time.sleep(interval_ms / 1000)
        request_id = channel.next_request_id()
        pending = {"kind": kind, "records": [], "error_code": None, "done": threading.Event()}
        with self._lock:
            self._pending[request_id] = pending
        try:
            field_name, request = RATE_REQUESTS[kind]
            field = channel.new_field(field_name)
            field.BrokerID = settings.broker_id
            field.InvestorID = settings.investor_id
            field.InstrumentID = symbol
            if kind == "margin":
                field.HedgeFlag = "1"
            code = channel.send_request(request, field, request_id)
            if code != 0:
                return {"complete": False, "local_code": code, "records": []}
            complete = pending["done"].wait(timeout)
            rows = pending["records"]
            matched = [row for row in rows if row.get("InstrumentID") in (symbol, "rb")]
            return {
                "complete": complete and pending["error_code"] is None and bool(matched) and len(matched) == len(rows),
                "error_code": pending["error_code"],
                "records": matched,
                "source": request,
                "scope_note": "InstrumentID=rb means the counter returned a product-scoped rate",
                "verified_rule": False,
            }
        except Exception as exc:
            return {"complete": False, "error_type": type(exc).__name__, "records": []}
        finally:
            with self._lock:
                self._pending.pop(request_id, None)


class PreflightBinding:
    """复用标准 CTP 绑定，仅补充独立只读费率回调，不改全局路由表。"""

    def __init__(self, rates: RawRateQueries, base=None) -> None:
        self.base = base if base is not None else load_ctp_binding()
        self.rates = rates

    def __getattr__(self, name):
        return getattr(self.base, name)

    def trader_spi_base(self):
        rates = self.rates
        base = self.base.trader_spi_base()

        class PreflightSpi(base):
            def OnRspQryInstrumentCommissionRate(self, record, info, request_id, is_last):  # noqa: N802
                rates.on_response("commission", record, info, request_id, is_last)

            def OnRspQryInstrumentMarginRate(self, record, info, request_id, is_last):  # noqa: N802
                rates.on_response("margin", record, info, request_id, is_last)

        return PreflightSpi


def rb_candidates(records: Sequence[Mapping[str, Any]], trading_day: date) -> list[dict[str, Any]]:
    """只接受柜台明确给出的实际在市 rb 期货，期权、连续合约和到期合约不入选。"""
    result = []
    for row in records:
        symbol = str(row.get("InstrumentID", ""))
        if row.get("ExchangeID") != "SHFE" or not re.fullmatch(r"rb\d{4}", symbol):
            continue
        if row.get("ProductID") != "rb" or str(row.get("ProductClass")) != "1":
            continue
        if str(row.get("IsTrading")) not in ("1", "True"):
            continue
        try:
            listed = datetime.strptime(str(row["OpenDate"]), "%Y%m%d").date()
            expiry = datetime.strptime(str(row["ExpireDate"]), "%Y%m%d").date()
            multiplier, price_tick = Decimal(str(row["VolumeMultiple"])), Decimal(str(row["PriceTick"]))
            year, month = int(row["DeliveryYear"]), int(row["DeliveryMonth"])
            if not listed <= trading_day < expiry or not 1 <= month <= 12 or not 2000 <= year <= 9999:
                continue
            if symbol != f"rb{year % 100:02d}{month:02d}":
                continue
            if not multiplier.is_finite() or not price_tick.is_finite() or min(multiplier, price_tick) <= 0:
                continue
        except (KeyError, ValueError, ArithmeticError):
            continue
        result.append({name: row[name] for name in CONTRACT_FIELDS if name in row})
    return sorted(result, key=lambda row: row["InstrumentID"])


def query_rb_catalog(queries, *, timeout: float, symbols: Sequence[str] = ()):
    """优先查询 SHFE/rb 目录；允许调用方明确指定候选，但从不推算未返回的合约。

    使用已有适配器的原始收集入口，以保留错误码；报文内容不写入诊断，避免泄漏账号。
    """
    requested = []
    for raw in symbols:
        if not re.fullmatch(r"SHFE\.rb\d{4}", raw):
            raise ValueError("explicit candidate must be an actual SHFE.rbYYYY contract code")
        requested.append(raw.split(".", 1)[1])
    filters = [
        {"ExchangeID": "SHFE", "ProductID": "rb", **({"InstrumentID": symbol} if symbol else {})}
        for symbol in (requested or [None])
    ]
    diagnostics = {
        "mode": "explicit_counter_verified_candidates" if requested else "counter_product_directory",
        "complete": True,
        "requests": [],
    }
    records = []
    previous_timeout = queries._timeout_s
    queries._timeout_s = timeout
    try:
        for fields in filters:
            started = time.monotonic()
            rows, error = queries._collect("instrument", queries.query_batch("instrument"), fields)
            requested_symbol = fields.get("InstrumentID")
            matching = [row for row in rows if requested_symbol is None or row.get("InstrumentID") == requested_symbol]
            if requested_symbol is not None and (not matching or len(matching) != len(rows)) and error is None:
                error = (-1005, "instrument response does not match requested candidate")
            code = None if error is None else error[0]
            reasons = {
                QUERY_TIMEOUT_CODE: "query_timeout_before_final_response",
                QUERY_LOCAL_REJECT_CODE: "query_rejected_by_local_api",
                QUERY_TRANSPORT_CODE: "query_transport_or_field_error",
                -1005: "candidate_response_mismatch",
            }
            diagnostics["requests"].append(
                {
                    "request": "ReqQryInstrument",
                    "filter": fields,
                    "complete": error is None,
                    "error_code": code,
                    "reason": None if error is None else reasons.get(code, "counter_query_error"),
                    "received_records": len(rows),
                    "seconds": round(time.monotonic() - started, 3),
                }
            )
            if error is not None:
                diagnostics["complete"] = False
                return (), diagnostics
            records.extend(matching)
    finally:
        queries._timeout_s = previous_timeout
    unique = {str(row.get("InstrumentID")): row for row in records}
    return tuple(unique.values()), diagnostics


def select_active(candidates: Sequence[Mapping[str, Any]], ticks: Sequence[Tick], day: date, now: datetime):
    """本次新鲜行情按持仓量、累计成交量排序；未收到行情的候选不可被猜作主力。"""
    symbols = {row["InstrumentID"] for row in candidates}
    latest: dict[str, Tick] = {}
    for tick in ticks:
        if tick.instrument.exchange != Exchange.SHFE or tick.instrument.symbol not in symbols:
            continue
        if tick.meta.trading_day != day or tick.meta.quality_flags != QualityFlag.OK or tick.last_price is None:
            continue
        if not timedelta(0) <= now - tick.meta.event_time <= timedelta(seconds=10):
            continue
        previous = latest.get(tick.instrument.symbol)
        if previous is None or tick.meta.event_time > previous.meta.event_time:
            latest[tick.instrument.symbol] = tick
    ranked = sorted(
        latest.values(), key=lambda tick: (-tick.open_interest, -tick.cumulative_volume, tick.instrument.symbol)
    )
    rows = [
        {
            "instrument": str(tick.instrument),
            "open_interest": tick.open_interest,
            "cumulative_volume": tick.cumulative_volume,
            "event_time": tick.meta.event_time.isoformat(),
            "last_price": str(tick.last_price),
            "bid_price": None if tick.bid_price is None else str(tick.bid_price),
            "ask_price": None if tick.ask_price is None else str(tick.ask_price),
        }
        for tick in ranked
    ]
    return (None if not rows else rows[0]["instrument"]), rows


def build_catalog(candidates: Sequence[Mapping[str, Any]], now: datetime) -> dict[str, Any]:
    entries = []
    for row in candidates:
        entries.append(
            {
                "exchange": "SHFE",
                "symbol": row["InstrumentID"],
                "product": "rb",
                "delivery_year": int(row["DeliveryYear"]),
                "delivery_month": int(row["DeliveryMonth"]),
                "multiplier": str(row["VolumeMultiple"]),
                "price_tick": str(row["PriceTick"]),
                "listed_on": datetime.strptime(str(row["OpenDate"]), "%Y%m%d").date().isoformat(),
                "last_trading_day": datetime.strptime(str(row["ExpireDate"]), "%Y%m%d").date().isoformat(),
                "aliases": [],
                "source_id": "simnow:ReqQryInstrument",
                "available_at": now.isoformat(),
            }
        )
    return {
        "schema_version": 1,
        "catalog_version": "simnow-preflight-" + now.strftime("%Y%m%dT%H%M%S%fZ"),
        "entries": entries,
    }


def history_capability(storage_dir: Path, symbol: str, now: datetime) -> dict[str, Any]:
    """只报告本地 30m 数据能力；不下载、不把当前合约规格回填成历史规则。"""
    instrument = InstrumentId(Exchange.SHFE, symbol.split(".", 1)[1])
    report: dict[str, Any] = {"instrument": symbol, "interval": "30m", "required_warmup_bars": 200}
    try:
        bars = ParquetDataStorage(storage_dir).read_bars(instrument, "30m")
        eligible = [
            bar
            for bar in bars
            if bar.meta.available_at <= now
            and bar.meta.quality_flags == QualityFlag.OK
            and bar.bar_end - bar.bar_start == timedelta(minutes=30)
        ]
        report.update(
            {
                "stored_bars": len(bars),
                "closed_full_length_bars": len(eligible),
                "has_200_bars": len(eligible) >= 200,
                "latest_bar_end": None if not eligible else max(bar.bar_end for bar in eligible).isoformat(),
                "suitability": "requires_session_continuity_and_strategy_validation" if eligible else "unavailable",
            }
        )
    except Exception as exc:
        report.update(
            {"stored_bars": None, "has_200_bars": False, "suitability": "unavailable", "error_type": type(exc).__name__}
        )
    return report


def run_preflight(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any] | None, int]:
    import yaml

    config = yaml.safe_load((ROOT / args.config).read_text(encoding="utf-8-sig")) or {}
    broker = config.get("broker") or {}
    profile_name = args.profile or broker.get("profile")
    if profile_name != "simnow_v6":
        raise ValueError("preflight requires broker.profile=simnow_v6")
    profile = ctp_setup.load_broker_profile(profile_name)
    trade_front, market_front = registered_front_pair(
        profile, args.front or broker.get("front_trade_uri"), args.market_front or broker.get("front_market_uri")
    )
    if broker.get("broker_id") and str(broker["broker_id"]) != str(profile["broker_id"]):
        raise ValueError("configured broker ID differs from registered SimNow broker")
    out = (ROOT / args.out).resolve()
    if not out.is_relative_to((ROOT / "runs").resolve()):
        raise ValueError("preflight output must stay within runs")
    out.mkdir(parents=True, exist_ok=True)
    settings = ctp_setup.ctp_settings(
        profile,
        user_id=args.user or broker.get("user_id") or broker.get("investor_id"),
        investor_id=args.investor or broker.get("investor_id"),
        front=trade_front,
        flow_dir=out / "ctp_flow",
        query_timeout_s=args.query_timeout,
    )
    account_id = str((config.get("risk") or {}).get("account_id") or "simnow-preflight")
    sink = RecordingSink()
    book = CtpOrderRefBook()
    normalizer = build_normalizer(account_id, book)
    rates = RawRateQueries()
    gateway = CtpTraderGateway(
        settings=settings,
        account_id=account_id,
        events=sink,
        normalizer=normalizer,
        price_tick=lambda instrument: Decimal("1"),
        capability_profile=ctp_setup.capability_profile(profile),
        capability_version="registered:simnow_v6",
        authority=lambda: None,
        ref_book=book,
        binding=PreflightBinding(rates),
        source_id="simnow-preflight",
    )
    queries = CtpQueryAdapter(
        account_id=account_id,
        channel=gateway,
        normalizer=normalizer,
        investor_id=settings.investor_id,
        broker_id=settings.broker_id,
        trading_day=lambda: gateway.trading_day,
        interval_ms=args.query_interval_ms,
        timeout_s=args.query_timeout,
        source_version=lambda: f"ctp:{gateway.binding.version}",
    )
    gateway.router.queries = queries
    market = CtpMarketDataGateway(
        settings=CtpMarketSettings.from_settings(settings, front_market=market_front),
        events=sink,
        source_id="simnow-preflight-md",
        timestamp_tolerance=timedelta(seconds=10),
    )
    now = datetime.now(timezone.utc)
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "simnow_read_only_preflight",
        "generated_at": now.isoformat(),
        "profile": "simnow_v6",
        "front_trade": trade_front,
        "front_market": market_front,
        "user": mask_identifier(settings.user_id),
        "investor": mask_identifier(settings.investor_id),
        "expected_trading_day": None if args.expected_trading_day is None else args.expected_trading_day.isoformat(),
        "ready_for_trading": False,
        "orders_sent": 0,
        "cancels_sent": 0,
        "limitations": [
            "read-only evidence; no trade round trip",
            "raw rates are not verified trading rules",
            "historical bars need independent session validation",
        ],
    }
    catalog = None
    stage = "connect_login_settlement"
    try:
        session = gateway.connect()
        day = session.trading_day
        report["counter_trading_day"] = None if day is None else day.isoformat()
        if day is None or (args.expected_trading_day is not None and day != args.expected_trading_day):
            report["error"] = "counter trading day does not match explicit expectation"
            return report, catalog, 2
        query_results = {}
        for name, call in (
            ("account", queries.query_account),
            ("positions", queries.query_positions),
            ("orders", queries.query_orders),
        ):
            stage = f"query_{name}"
            result = call(queries.query_batch(name))
            query_results[name] = result
        report["queries"] = {
            name: {"complete": result.complete, "error_code": result.error_code, "records": len(result.records)}
            for name, result in query_results.items()
        }
        report["funds"] = [
            {
                name: None if getattr(item, name) is None else str(getattr(item, name))
                for name in ("balance", "equity", "margin", "available_for_new_trades")
            }
            for item in query_results["account"].records
        ]
        report["positions"] = [
            {
                "instrument": str(item.instrument),
                "side": str(item.side),
                "hedge_flag": item.hedge_flag,
                "pos_td": item.pos_td,
                "pos_yd": item.pos_yd,
                "frozen_td": item.frozen_td,
                "frozen_yd": item.frozen_yd,
            }
            for item in query_results["positions"].records
        ]
        report["active_orders"] = [
            {
                "instrument": str(item.instrument),
                "side": str(item.side),
                "offset": str(item.offset),
                "status": str(item.status),
                "quantity": item.quantity,
                "filled_quantity": item.filled_quantity,
            }
            for item in query_results["orders"].records
            if item.status not in TERMINAL_STATUSES
        ]
        stage = "query_rb_catalog"
        raw_candidates, catalog_query = query_rb_catalog(
            queries, timeout=args.catalog_timeout, symbols=args.candidate_symbol
        )
        report["catalog_query"] = catalog_query
        if not catalog_query["complete"]:
            report["failed_stage"] = stage
            report["error"] = "counter instrument query incomplete; see catalog_query.requests error_code"
            return report, catalog, 2
        candidates = rb_candidates(raw_candidates, day)
        report["candidates"] = candidates
        if not candidates:
            report["error"] = "no active actual SHFE.rb futures returned by counter catalog"
            return report, catalog, 2
        catalog = build_catalog(candidates, datetime.now(timezone.utc))
        stage = "connect_market"
        market.connect()
        instruments = [InstrumentId(Exchange.SHFE, row["InstrumentID"]) for row in candidates]
        stage = "subscribe_and_collect_market"
        accepted = market.subscribe(instruments, timeout_s=args.query_timeout)
        report["subscribed"] = list(accepted)
        deadline = time.monotonic() + args.market_seconds
        while time.monotonic() < deadline:
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        observed_at = datetime.now(timezone.utc)
        ticks = [event.payload for event in sink.events if event.kind == EventKind.MARKET_DATA]
        selected, ranking = select_active(candidates, ticks, day, observed_at)
        report["selection"] = {
            "instrument": selected,
            "method": "fresh_tick_open_interest_then_volume",
            "ranking": ranking,
        }
        report["market_quality"] = {
            key: market.counts.get(key, 0)
            for key in ("ticks_enqueued", "timestamp_anomalies", "front_disconnected", "empty_snapshots")
        }
        if selected is None:
            report["error"] = "no fresh market snapshot from a catalog candidate"
            return report, catalog, 2
        selected_symbol = selected.split(".", 1)[1]
        report["selected_contract"] = next(row for row in candidates if row["InstrumentID"] == selected_symbol)
        stage = "query_commission_and_margin"
        report["rates"] = {
            kind: rates.query(
                gateway, settings, kind, selected_symbol, timeout=args.query_timeout, interval_ms=args.query_interval_ms
            )
            for kind in RATE_FIELDS
        }
        stage = "inspect_local_30m_history"
        report["history_30m"] = history_capability(
            ROOT / str((config.get("data") or {}).get("storage_dir", "data_storage")), selected, observed_at
        )
        report["runtime_inputs"] = {
            "symbol": selected,
            "trading_day": day.isoformat(),
            "catalog_path": (out / "contract_catalog.json").relative_to(ROOT).as_posix(),
            "front_trade": trade_front,
            "front_market": market_front,
            "interval": "30m",
        }
        good = (
            all(result.complete for result in query_results.values())
            and all(row["complete"] for row in report["rates"].values())
            and not report["market_quality"]["timestamp_anomalies"]
            and not report["market_quality"]["front_disconnected"]
            and not sink.callback_errors
        )
        report["read_only_checks_passed"] = good
        return report, catalog, 0 if good else 2
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        report["failed_stage"] = stage
        return report, catalog, 1
    finally:
        report["query_diagnostics"] = {
            "counts": dict(queries.counts),
            "evidence": [
                {key: row[key] for key in ("kind", "request_id", "local_code", "error_code") if key in row}
                for row in queries.evidence
            ],
            "gateway_error_code": gateway.status().get("last_error_code"),
        }
        market.close()
        gateway.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SimNow 只读账户/实际合约/行情/费率预检")
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--profile", choices=("simnow_v6",))
    parser.add_argument("--user")
    parser.add_argument("--investor")
    parser.add_argument("--front")
    parser.add_argument("--market-front")
    parser.add_argument("--expected-trading-day", type=date.fromisoformat)
    parser.add_argument("--query-interval-ms", type=int, default=1000)
    parser.add_argument("--query-timeout", type=float, default=15.0)
    parser.add_argument("--catalog-timeout", type=float, default=45.0)
    parser.add_argument(
        "--candidate-symbol", action="append", default=[], help="可选：明确指定实际候选，逐一核对柜台目录"
    )
    parser.add_argument("--market-seconds", type=float, default=12.0)
    parser.add_argument("--out", default="runs/s0/simnow_preflight")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if (
        args.query_interval_ms < 0
        or min(args.query_timeout, args.catalog_timeout) <= 0
        or not 0 < args.market_seconds <= 60
    ):
        raise ValueError("query and market wait limits must be positive; market window must be at most 60 seconds")
    out = (ROOT / args.out).resolve()
    if not out.is_relative_to((ROOT / "runs").resolve()):
        raise ValueError("preflight output must stay within runs")
    try:
        report, catalog, code = run_preflight(args)
    except Exception as exc:
        report, catalog, code = (
            {"kind": "simnow_read_only_preflight", "error_type": type(exc).__name__, "read_only_checks_passed": False},
            None,
            1,
        )
    out.mkdir(parents=True, exist_ok=True)
    (out / "preflight.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    if catalog is not None:
        (out / "contract_catalog.json").write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(
        json.dumps(
            {
                "exit_code": code,
                "evidence": (out / "preflight.json").relative_to(ROOT).as_posix(),
                "selection": report.get("selection"),
            },
            ensure_ascii=False,
        )
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
