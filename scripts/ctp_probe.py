#!/usr/bin/env python
"""[脚本工具] 柜台联调探测：登录、查询、报单与回报闭环证据 (S0-02, GAP-S0-01, FR-LIVE-01/04).

对应 06 的 S0-02 验证方式（`scripts/init_env.py` 的输出）与 GAP-S0-01 的关闭条件：
登录、查询、报单、接收回报全流程验证并记录原生库哈希。

边界：

- 只连接登记过的仿真环境，口令从 ``QH_CTP_PASSWORD`` 读取，证据文件写入 ``runs/``（不入库）。
- 探测用固定控制代次与内存事件记录器，不打开 ``trading.db``、不装配执行服务，也不写任何账户事实；
  它证明"柜台接口可用、回报能归一化并归属"，不构成执行服务或阶段出口的验收证据。
- 报单探测只发开仓限价单（开仓标志在所有交易所唯一，无需核验今昨仓映射），价格取柜台查询到的
  跌停价（低于市价，不会立即成交），随后撤单；成交与平仓相关能力仍待 A25/A23 用例在柜台核验。

用法::

    set QH_CTP_PASSWORD=...
    uv run --no-sync python scripts/ctp_probe.py --profile simnow_v6
    uv run --no-sync python scripts/ctp_probe.py --order-symbol SHFE.rb2601 --order-quantity 1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.core.constants import EventKind, Exchange, Offset, OrderStatus, OrderType, SendState, Side  # noqa: E402
from qh_trader.core.event import CanonicalEvent  # noqa: E402
from qh_trader.core.objects import (  # noqa: E402
    AccountFunds,
    ControlEpoch,
    InstrumentId,
    OrderIntent,
    Position,
    Trade,
)
from qh_trader.gateway.ctp_gateway import (  # noqa: E402
    CtpOrderRef,
    CtpOrderRefBook,
    CtpSettings,
    CtpTraderGateway,
    order_ref_evidence,
)
from qh_trader.gateway.ctp_market import CtpMarketDataGateway, CtpMarketSettings  # noqa: E402
from qh_trader.gateway.ctp_query import CtpQueryAdapter  # noqa: E402
from qh_trader.gateway.feedback_normalizer import build_normalizer  # noqa: E402
from scripts import ctp_setup  # noqa: E402

PROBE_CONTROLLER = "ctp-probe"
PROBE_EPOCH = ControlEpoch(PROBE_CONTROLLER, 1)
DEFAULT_ORDER_WAIT_S = 8.0


class ProbeError(RuntimeError):
    """探测步骤未达成；步骤结果仍会写入证据文件."""


@dataclass
class RecordingSink:
    """探测用事件记录器：等价于执行服务的回调出口，但只记录不记账."""

    events: list[CanonicalEvent] = field(default_factory=list)
    callback_errors: list[str] = field(default_factory=list)

    def enqueue(self, event: CanonicalEvent) -> bool:
        self.events.append(event)
        return True

    def enqueue_callback_error(self, source_id: str, error: Exception) -> None:
        self.callback_errors.append(f"{source_id}:{type(error).__name__}")

    def order_updates(self, ref: CtpOrderRef) -> list[str]:
        """按原会话三元组取回该委托的回报序列（探测脚本自己分配的 OrderRef）."""
        statuses: list[str] = []
        for event in self.events:
            if event.kind != EventKind.ORDER_REPORT:
                continue
            identity = getattr(event.payload, "identity", None)
            if identity is None:
                continue
            if (identity.front_id, identity.session_id, identity.order_ref) != ref.triple:
                continue
            statuses.append(str(event.payload.status))
        return statuses


@dataclass
class Step:
    name: str
    status: str
    detail: Mapping[str, object] = field(default_factory=dict)
    seconds: float = 0.0

    def as_mapping(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "seconds": round(self.seconds, 4),
            "detail": dict(self.detail),
        }


def mask_identifier(value: str) -> Mapping[str, str]:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    tail = value[-2:] if len(value) > 2 else ""
    return {"masked": "*" * max(0, len(value) - 2) + tail, "sha256_prefix": digest[:12]}


def parse_symbol(raw: str) -> InstrumentId:
    if "." not in raw:
        raise ProbeError("合约须写成 交易所.合约 形式，例如 SHFE.rb2601")
    prefix, symbol = raw.split(".", 1)
    try:
        return InstrumentId(Exchange(prefix), symbol)
    except ValueError as exc:
        raise ProbeError(f"未知交易所前缀 {prefix!r}") from exc


def _deadline_wait(sink: RecordingSink, ref: CtpOrderRef, wanted: set[str], timeout_s: float) -> list[str]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        statuses = sink.order_updates(ref)
        if statuses and statuses[-1] in wanted:
            return statuses
        time.sleep(0.05)
    return sink.order_updates(ref)


def run_probe(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    profile = ctp_setup.load_broker_profile(args.profile)
    settings: CtpSettings = ctp_setup.ctp_settings(
        profile,
        user_id=args.user or ctp_setup_account(profile, "user_id"),
        investor_id=args.investor or ctp_setup_account(profile, "investor_id"),
        front=args.front,
        flow_dir=args.flow_dir,
        query_interval_ms=args.query_interval_ms,
        connect_timeout_s=args.connect_timeout,
        login_timeout_s=args.login_timeout,
    )
    capability_profile = ctp_setup.capability_profile(profile)
    sink = RecordingSink()
    ref_book = CtpOrderRefBook()
    normalizer = build_normalizer(args.account, ref_book)
    # 价格步长先取本地登记；柜台查询到官方参数后立即替换，避免用错步长发送 (FR-RULE-05)
    price_tick = {"value": Decimal(str(args.price_tick))}
    gateway = CtpTraderGateway(
        settings=settings,
        account_id=args.account,
        events=sink,
        normalizer=normalizer,
        price_tick=lambda instrument: price_tick["value"],
        capability_profile=capability_profile,
        capability_version="registered:" + str(profile.get("profile_name")),
        authority=lambda: PROBE_EPOCH,
        offset_mappings=ctp_setup.offset_mappings(profile),
        ref_book=ref_book,
        source_id="ctp-probe",
    )
    queries = CtpQueryAdapter(
        account_id=args.account,
        channel=gateway,
        normalizer=normalizer,
        investor_id=settings.investor_id,
        broker_id=settings.broker_id,
        trading_day=lambda: gateway.trading_day,
        interval_ms=args.query_interval_ms,
        timeout_s=args.query_timeout,
        # 绑定版本只有加载后才知道，因此用惰性取值，避免证据里出现 "unavailable"
        source_version=lambda: f"ctp:{gateway.binding.version}",
    )
    gateway.router.queries = queries
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "ctp_runtime_probe",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.system(),
            "machine": platform.machine(),
        },
        "binding": {"name": gateway.binding.name, "version": gateway.binding.version},
        "profile": ctp_setup.profile_summary(profile),
        "front_candidates": list(ctp_setup.register_front_candidates(profile)),
        "account": {
            "account_id": args.account,
            "investor_id": mask_identifier(settings.investor_id),
            "user_id": mask_identifier(settings.user_id),
            "broker_id": settings.broker_id,
            "front_trade": settings.front_trade,
            "terminal_authentication": settings.authenticated,
        },
        "steps": [],
        "queries": {},
        "order_probe": None,
        "redaction": [
            "口令与 AuthCode 不写入证据文件，只记录是否配置",
            "投资者号与用户号仅保留掩码与 SHA-256 前缀",
        ],
        "scope": "仿真环境本地探测（工程样例）；不替代柜台联调、阶段出口或实盘验收",
    }
    exit_code = 0

    def record(step: Step) -> None:
        report["steps"].append(step.as_mapping())

    started = time.monotonic()
    try:
        session = gateway.connect()
    except Exception as exc:
        status = gateway.status()
        record(
            Step(
                "connect_login_settlement",
                "failed",
                {
                    "error_type": type(exc).__name__,
                    "front_connected": status.get("counts", {}).get("front_connected"),
                    "fault": status.get("fault"),
                    "counter_error_code": status.get("last_error_code"),
                    # 柜台报文只用于诊断；它不含凭证，但仍按原样记录以便对照 CTP 错误码表
                    "counter_error_message": status.get("last_error_message"),
                },
                time.monotonic() - started,
            )
        )
        report["gateway_status"] = dict(gateway.status())
        return report, 1
    record(
        Step(
            "connect_login_settlement",
            "passed",
            {
                **session.as_mapping(),
                "api_version": session.api_version,
                "notes": list(session.notes),
            },
            time.monotonic() - started,
        )
    )
    # 绑定版本与原生库哈希只有在绑定真正加载后才可知，握手后重新登记 (S0-02)
    report["binding"]["dll_hashes"] = dict(session.dll_hashes)
    report["binding"]["version"] = gateway.binding.version
    report["session"] = session.as_mapping()

    # 交易日以柜台为准：探测只记录柜台给出的交易日，不用本地日期推算 (FR-CAL-03)
    if session.trading_day is None:
        record(Step("counter_trading_day", "failed", {"reason": "the counter reported no trading day"}))
        return report, 1
    local_date = datetime.now(timezone(timedelta(hours=8))).date()
    matches_local = session.trading_day == local_date
    record(
        Step(
            "counter_trading_day",
            "passed",
            {
                "trading_day": session.trading_day.isoformat(),
                "local_date": local_date.isoformat(),
                "matches_local_date": matches_local,
            },
        )
    )
    if not matches_local:
        # 休市日或环境滞后：报单 / 撤单只能在交易时段验证，否则会留下无法撤销的委托
        warning = (
            f"柜台交易日 {session.trading_day.isoformat()} 与本地日期 {local_date.isoformat()} 不一致"
            "（休市日或环境按上一交易日镜像）：报单与撤单验证须在交易时段进行"
        )
        print(f"注意: {warning}")
        report["trading_day_warning"] = warning

    for name, query in (
        ("account", queries.query_account),
        ("positions", queries.query_positions),
        ("orders", queries.query_orders),
        ("trades", queries.query_trades),
    ):
        batch = queries.query_batch(name)  # 探测脚本显式构造批次，逐步记录每一步证据
        step_started = time.monotonic()
        try:
            result = query(batch)
        except Exception as exc:
            record(Step(f"query_{name}", "failed", {"error_type": type(exc).__name__}))
            report["queries"][name] = {"complete": False, "error": type(exc).__name__}
            exit_code = 1
            continue
        detail: dict[str, object] = {
            "complete": result.complete,
            "records": len(result.records),
            "error_code": result.error_code,
            "source_version": result.source_version,
        }
        if name == "account" and result.records:
            funds = cast(AccountFunds, result.records[0])
            detail["funds"] = {
                "balance": None if funds.balance is None else str(funds.balance),
                "margin": None if funds.margin is None else str(funds.margin),
                "available": None if funds.available_for_new_trades is None else str(funds.available_for_new_trades),
                "equity": None if funds.equity is None else str(funds.equity),
            }
        if name == "positions":
            detail["position_rows"] = [
                {
                    "instrument": str(item.instrument),
                    "side": str(item.side),
                    "pos_yd": item.pos_yd,
                    "pos_td": item.pos_td,
                    "frozen_yd": item.frozen_yd,
                    "frozen_td": item.frozen_td,
                }
                for item in (cast(Position, record) for record in result.records[:20])
            ]
        if name == "trades":
            detail["trade_ids"] = [cast(Trade, item).trade_id for item in result.records[:20]]
        record(
            Step(
                f"query_{name}",
                "passed" if result.complete else "incomplete",
                detail,
                time.monotonic() - step_started,
            )
        )
        report["queries"][name] = detail
        if not result.complete:
            exit_code = exit_code or 2

    if sink.callback_errors:
        report["callback_errors"] = list(sink.callback_errors)

    if args.market_symbol:
        market_report, market_exit = probe_market(args, gateway, sink)
        report["market_probe"] = market_report
        exit_code = max(exit_code, market_exit)

    if args.order_symbol:
        order_report, order_exit = probe_order(args, gateway, queries, sink, ref_book, price_tick)
        report["order_probe"] = order_report
        exit_code = max(exit_code, order_exit)

    report["gateway_status"] = dict(gateway.status())
    # 柜台明确拒绝但当前事件类型表达不了的回报（例如撤单被拒）必须留痕，便于直接看错误码
    report["callback_gaps"] = [dict(gap) for gap in normalizer.gaps]
    # 查询里被拒收的记录（例如柜台冻结量超过持仓这类当前类型表达不了的口径）必须能看见原因
    report["query_evidence"] = [dict(item) for item in queries.evidence]
    report["local_rejections"] = list(gateway.rejections)
    report["callbacks_normalized"] = dict(normalizer.counts)
    report["normalized_events"] = {
        "order_reports": sum(1 for e in sink.events if e.kind == EventKind.ORDER_REPORT),
        "trade_reports": sum(1 for e in sink.events if e.kind == EventKind.TRADE_REPORT),
        "control": sum(1 for e in sink.events if e.kind == EventKind.CONTROL),
    }
    gateway.close()
    return report, exit_code


def probe_market(
    args: argparse.Namespace, gateway: CtpTraderGateway, sink: RecordingSink
) -> tuple[Mapping[str, object], int]:
    """行情通道探测：连接行情前置、订阅合约、收集逐笔快照并归一化.

    行情与交易日无关（休市日也能取到快照），因此不受"非交易日"限制；但**不下单、不订阅全市场**。
    """
    instrument = parse_symbol(args.market_symbol)
    front = args.market_front or ctp_setup.front_addresses(ctp_setup.load_broker_profile(args.profile)).get("market")
    if not front:
        return {"error": "no market front is registered and none was given with --market-front"}, 2
    market = CtpMarketDataGateway(
        settings=CtpMarketSettings.from_settings(gateway.settings, front_market=front),
        events=sink,
        source_id="ctp-md-probe",
    )
    detail: dict[str, object] = {"instrument": str(instrument), "front_market": front}
    try:
        detail["session"] = dict(market.connect())
    except Exception as exc:
        detail["error"] = f"market data front could not be reached ({type(exc).__name__})"
        detail["status"] = dict(market.status())
        return detail, 1
    before = sum(1 for event in sink.events if event.kind == EventKind.MARKET_DATA)
    accepted = market.subscribe([instrument], timeout_s=args.market_seconds)
    detail["subscribed"] = list(accepted)
    if not accepted:
        detail["error"] = "the market data front did not accept the subscription"
        detail["status"] = dict(market.status())
        market.close()
        return detail, 2
    deadline = time.monotonic() + args.market_seconds
    while time.monotonic() < deadline:
        collected = sum(1 for event in sink.events if event.kind == EventKind.MARKET_DATA) - before
        if collected >= args.market_ticks:
            break
        time.sleep(0.2)
    ticks = [event for event in sink.events if event.kind == EventKind.MARKET_DATA][before:]
    detail["ticks"] = len(ticks)
    if ticks:
        first, last = ticks[0].payload, ticks[-1].payload
        detail["first_tick"] = {
            "event_time": first.meta.event_time.isoformat(),
            "trading_day": first.meta.trading_day.isoformat(),
            "last_price": None if first.last_price is None else str(first.last_price),
            "bid_price": None if first.bid_price is None else str(first.bid_price),
            "ask_price": None if first.ask_price is None else str(first.ask_price),
            "cumulative_volume": first.cumulative_volume,
            "open_interest": first.open_interest,
            "phase": str(first.phase),
        }
        detail["last_tick"] = {
            "event_time": last.meta.event_time.isoformat(),
            "last_price": None if last.last_price is None else str(last.last_price),
            "cumulative_volume": last.cumulative_volume,
        }
    detail["status"] = dict(market.status())
    market.close()
    if not ticks:
        detail["error"] = "no snapshot arrived before the deadline"
        return detail, 2
    detail["result"] = "market data snapshots normalized and enqueued"
    return detail, 0


def ctp_setup_account(profile: Mapping[str, Any], key: str) -> str | None:
    account = profile.get("account")
    if isinstance(account, Mapping) and account.get(key):
        return str(account[key])
    return None


def probe_order(
    args: argparse.Namespace,
    gateway: CtpTraderGateway,
    queries: CtpQueryAdapter,
    sink: RecordingSink,
    ref_book: CtpOrderRefBook,
    price_tick: dict[str, Decimal],
) -> tuple[Mapping[str, object], int]:
    """开仓限价单 + 撤单闭环：验证报单接口、回报归一化与归属，不验证今昨仓映射."""
    instrument = parse_symbol(args.order_symbol)
    symbol = instrument.symbol
    detail: dict[str, object] = {"instrument": str(instrument)}
    local_date = datetime.now(timezone(timedelta(hours=8))).date()
    if gateway.trading_day is not None and gateway.trading_day != local_date and not args.allow_non_trading_day:
        # 非交易日或环境滞后时不下单：撤单在柜台的（已关闭）交易日里找不到报单，会留下无法撤销的委托
        detail["error"] = (
            f"counter trading day {gateway.trading_day.isoformat()} differs from the local date "
            f"{local_date.isoformat()}; order probing needs a trading session "
            "(use --allow-non-trading-day only for API smoke tests)"
        )
        return detail, 2
    try:
        contract = queries.query_instrument(symbol)
        depth = queries.query_depth(symbol)
    except Exception as exc:
        detail["error"] = f"queries failed ({type(exc).__name__})"
        return detail, 1
    detail["contract"] = {
        "volume_multiple": None if not contract else contract.get("VolumeMultiple"),
        "price_tick": None if not contract else contract.get("PriceTick"),
        "exchange_id": None if not contract else contract.get("ExchangeID"),
        "expire_date": None if not contract else contract.get("ExpireDate"),
        "source": "ReqQryInstrument",
    }
    if not contract or not contract.get("PriceTick"):
        detail["error"] = "the counter reported no price tick for this contract; send is disabled rather than guessed"
        return detail, 2
    if not depth:
        detail["error"] = "no depth snapshot available for a non-marketable price reference"
        return detail, 2
    tick = Decimal(str(contract["PriceTick"]))
    price_tick["value"] = tick
    reference = depth.get("LowerLimitPrice") or depth.get("PreSettlementPrice")
    if not reference or Decimal(str(reference)) <= 0:
        detail["error"] = "the counter reported no usable price reference (lower limit / pre-settlement)"
        return detail, 2
    price_ticks = int(Decimal(str(reference)) / tick)
    if price_ticks <= 0:
        detail["error"] = "price reference is below one price tick"
        return detail, 2
    detail["price_reference"] = {
        "lower_limit_price": None if depth.get("LowerLimitPrice") is None else str(depth["LowerLimitPrice"]),
        "pre_settlement_price": None if depth.get("PreSettlementPrice") is None else str(depth["PreSettlementPrice"]),
        "price_tick": str(tick),
        "source": "ReqQryDepthMarketData",
    }
    intent = OrderIntent(
        client_order_id=f"probe-{datetime.now(timezone.utc).strftime('%H%M%S%f')}",
        account_id=args.account,
        strategy_id=PROBE_CONTROLLER,
        instrument=instrument,
        side=Side.BUY,
        offset=Offset.OPEN,
        quantity=int(args.order_quantity),
        order_type=OrderType.LIMIT,
        created_at=datetime.now(timezone.utc),
        limit_price_ticks=price_ticks,
    )
    detail["intent"] = {
        "client_order_id": intent.client_order_id,
        "limit_price_ticks": price_ticks,
        "price": str(Decimal(price_ticks) * tick),
        "quantity": intent.quantity,
        "offset": str(intent.offset),
        "order_type": str(intent.order_type),
    }
    if not gateway.mark_reconciled():
        detail["error"] = "queries did not establish a reconciled session; the probe refuses to send"
        return detail, 2
    send_result = gateway.submit(intent, PROBE_EPOCH)
    detail["send_result"] = {
        "state": str(send_result.state),
        "local_code": send_result.local_code,
        "evidence": send_result.evidence,
        "order_ref": None if send_result.remote_identity is None else send_result.remote_identity.order_ref,
    }
    if send_result.state == SendState.NOT_SENT:
        detail["error"] = "the counter gateway refused to send locally (see evidence); nothing was sent"
        return detail, 3
    if send_result.remote_identity is None:
        detail["error"] = "local send result carries no remote identity; attribution cannot be verified"
        return detail, 3
    reference_ref = CtpOrderRef(
        front_id=int(send_result.remote_identity.front_id or 0),
        session_id=int(send_result.remote_identity.session_id or 0),
        order_ref=str(send_result.remote_identity.order_ref),
    )
    statuses = _deadline_wait(sink, reference_ref, {str(OrderStatus.ACCEPTED)}, args.order_wait)
    detail["statuses_after_submit"] = statuses
    if str(OrderStatus.ACCEPTED) not in statuses:
        detail["error"] = "the counter did not report a queued order for this intent"
        return detail, 4
    cancel_result = gateway.cancel(send_result.remote_identity, PROBE_EPOCH)
    detail["cancel_result"] = {
        "state": str(cancel_result.state),
        "local_code": cancel_result.local_code,
        "evidence": cancel_result.evidence,
    }
    if cancel_result.state == SendState.NOT_SENT:
        detail["error"] = "cancellation was refused locally; the probe leaves the reference order for manual review"
        return detail, 4
    cancel_statuses = _deadline_wait(sink, reference_ref, {str(OrderStatus.CANCELLED)}, args.order_wait)
    detail["statuses_after_cancel"] = cancel_statuses
    detail["attribution"] = {
        "order_ref_evidence": order_ref_evidence(reference_ref),
        "refs_tracked": len(ref_book.snapshot()),
    }
    if str(OrderStatus.CANCELLED) not in cancel_statuses:
        detail["error"] = "cancellation did not complete before the probe deadline"
        return detail, 4
    detail["result"] = "order insert and cancel round trip verified against the counter"
    return detail, 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="柜台登录、查询与报单闭环探测 (S0-02)")
    parser.add_argument("--config", default="config/settings.yaml", help="运行配置，用于取 account_id 等本地口径")
    parser.add_argument("--profile", default=None, help="柜台登记名（默认取运行配置 broker.profile）")
    parser.add_argument("--user", default=None, help="覆盖登录用户号")
    parser.add_argument("--investor", default=None, help="覆盖投资者号")
    parser.add_argument("--front", default=None, help="覆盖交易前置地址")
    parser.add_argument("--account", default=None, help="本地账户标识（写入证据与事件归属）")
    parser.add_argument("--flow-dir", default="runs/live/ctp_flow", help="CTP 私有流目录（本地磁盘）")
    parser.add_argument("--query-interval-ms", type=int, default=1000, help="查询流控间隔")
    parser.add_argument("--query-timeout", type=float, default=15.0, help="单次查询等待应答的秒数")
    parser.add_argument("--connect-timeout", type=float, default=20.0, help="等待前置连接的秒数")
    parser.add_argument("--login-timeout", type=float, default=20.0, help="等待认证 / 登录 / 结算确认的秒数")
    parser.add_argument("--order-symbol", default=None, help="可选：做开仓限价单 + 撤单闭环的实际合约")
    parser.add_argument("--order-quantity", type=int, default=1, help="报单探测的手数（默认 1 手）")
    parser.add_argument("--order-wait", type=float, default=DEFAULT_ORDER_WAIT_S, help="等待回报的秒数")
    parser.add_argument(
        "--allow-non-trading-day",
        action="store_true",
        help="允许在柜台交易日与本地日期不一致时仍报单（仅用于接口冒烟，可能留下无法撤销的委托）",
    )
    parser.add_argument("--price-tick", default="1", help="报单探测使用的价格步长（须与合约登记一致）")
    parser.add_argument("--market-symbol", default=None, help="可选：订阅行情并收集逐笔快照，如 SHFE.rb2610")
    parser.add_argument("--market-front", default=None, help="行情前置（默认取柜台登记的 fronts.market）")
    parser.add_argument("--market-seconds", type=float, default=8.0, help="收集行情的秒数")
    parser.add_argument("--market-ticks", type=int, default=20, help="收集到多少笔快照即可提前结束")
    parser.add_argument("--out", default="runs/s0", help="证据输出目录（项目内）")
    parser.add_argument("--json", action="store_true", help="同时打印完整证据 JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    if args.profile is None:
        config_path = ROOT / args.config
        if config_path.is_file():
            import yaml

            data = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}
            args.profile = (data.get("broker") or {}).get("profile")
            if args.account is None:
                args.account = (data.get("risk") or {}).get("account_id")
    args.account = args.account or "ctp-probe-account"
    try:
        report, exit_code = run_probe(args)
    except ctp_setup.BrokerProfileError as exc:
        print(f"配置失败: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # 未预期的失败也要给出可读结论
        print(f"探测失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    out_dir = (ROOT / args.out).resolve()
    if not out_dir.is_relative_to(ROOT / "runs"):
        print("证据须写入项目 runs/ 目录，避免机器相关信息进入 Git", file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"ctp_runtime_evidence_{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    for step in report["steps"]:
        print(f"[{step['status']}] {step['name']} ({step['seconds']}s)")
    for gap in report.get("callback_gaps", []):
        print(
            f"柜台拒绝未表达回报: {gap.get('callback')} code={gap.get('counter_error_code')} "
            f"{gap.get('counter_error_message')}"
        )
    if report.get("market_probe"):
        probe = report["market_probe"]
        print(f"行情探测: ticks={probe.get('ticks')} {probe.get('result') or probe.get('error')}")
    if report.get("order_probe"):
        outcome = report["order_probe"].get("result") or report["order_probe"].get("error")
        print(f"报单探测: {json.dumps(outcome, ensure_ascii=False)}")
    print(f"证据文件: {path.relative_to(ROOT).as_posix()}")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
