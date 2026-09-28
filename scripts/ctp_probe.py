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

无凭据联调（尚未拿到柜台账号时，只证明原生库与前置正常）::

    uv run --no-sync python scripts/ctp_probe.py --profile openctp_tts --dummy-login

``--dummy-login`` 用明显虚构的探测账号发起一次登录：柜台会应答并拒绝，证据里记为
``login_probe.kind = unregistered_dummy_account`` 与柜台错误码，**不构成登录通过**，退出码 3。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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
    OrderIdentity,
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
from scripts import ctp_position_probe, ctp_setup  # noqa: E402

PROBE_CONTROLLER = "ctp-probe"
PROBE_EPOCH = ControlEpoch(PROBE_CONTROLLER, 1)
DEFAULT_ORDER_WAIT_S = 8.0
#: 无凭据联调的虚构探测账号：它不属于任何客户，只用来确认柜台会在同一连接上应答登录请求。
DUMMY_PROBE_USER = "qh_probe"
DUMMY_PROBE_PASSWORD = "QhProbe!Unregistered#2026"
DUMMY_PROBE_ACCOUNT = "openctp-tts-probe"
DUMMY_PROBE_EXIT_CODE = 3


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


def record_native_libs(report: dict[str, Any], gateway: CtpTraderGateway) -> None:
    """登记实际装载的原生库（flavor、暂存文件与摘要）；未登记 flavor 时记 ``None``.

    原生库只有在装载后才可核对，因此连接成功与连接失败都要登记，否则证据里看不到用的是哪一套库。
    """
    staged = getattr(gateway.binding, "native_lib_report", None)
    report["native_libs"] = None if staged is None else staged.as_mapping()


def probe_market_reachability(
    args: argparse.Namespace, settings: CtpSettings, sink: RecordingSink
) -> Mapping[str, object]:
    """行情前置可达性与原生库核验：只登行情前置，只记录柜台应答，不订阅也不下单。

    无凭据联调时账户是虚构的，因此这里同样**只**证明"TTS 原生库能加载行情模块、行情前置能建立
    会话并给出应答"，不构成行情登录通过。
    """
    front = args.market_front or ctp_setup.front_addresses(ctp_setup.load_broker_profile(args.profile)).get("market")
    if not front:
        return {"result": "no market front is registered and none was given with --market-front"}
    market = CtpMarketDataGateway(
        settings=CtpMarketSettings.from_settings(settings, front_market=front),
        events=sink,
        source_id="ctp-md-probe",
    )
    detail: dict[str, object] = {"front_market": front}
    started = time.monotonic()
    try:
        detail["session"] = dict(market.connect())
        detail["result"] = "the market data front accepted the login"
    except Exception as exc:
        status = dict(market.status())
        detail["error"] = type(exc).__name__
        detail["fault"] = status.get("fault")
        detail["counts"] = status.get("counts")
        detail["counter_error_code"] = status.get("last_error_code")
        detail["counter_error_message"] = status.get("last_error_message")
        detail["result"] = "the counter answered the market login on the same connection"
    finally:
        detail["native_libs"] = market.native_lib_report()
        detail["seconds"] = round(time.monotonic() - started, 6)
        market.close()
    return detail


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
        terminal_mode=args.terminal_mode,
        collector_lib_path=args.collector_lib,
    )
    capability_profile = ctp_setup.capability_profile(profile)
    offset_mappings = list(ctp_setup.offset_mappings(profile))
    capability_version = "registered:" + str(profile.get("profile_name"))
    if args.position_probe:
        # 持仓探测要真的成交与平仓：开平标志与市价单在登记里仍未核验，因此显式换成**仅供探测**的
        # 档案（证据写 probe:*），并把目标交易所的候选开平标志挂上；执行服务不受影响。
        probed_exchange = parse_symbol(args.position_probe).exchange
        offset_mappings = [item for item in offset_mappings if item.exchange != probed_exchange]
        offset_mappings.append(ctp_position_probe.probe_offset_mapping(probed_exchange))
        capability_profile = ctp_position_probe.probe_capability_profile(capability_profile)
        capability_version = ctp_position_probe.PROBE_CAPABILITY_EVIDENCE
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
        capability_version=capability_version,
        authority=lambda: PROBE_EPOCH,
        offset_mappings=offset_mappings,
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
        "native_libs": None,
        "login_probe": None,
        "market_reachability": None,
        "redaction": [
            "口令与 AuthCode 不写入证据文件，只记录是否配置",
            "投资者号与用户号仅保留掩码与 SHA-256 前缀",
        ],
        "scope": (
            "仿真环境本地探测（工程样例）；持仓探测会在仿真柜台真实成交并留下手续费，"
            "收尾必须确认已平仓；不替代柜台联调、阶段出口或实盘验收"
        ),
    }
    exit_code = 0

    def record(step: Step) -> None:
        report["steps"].append(step.as_mapping())

    started = time.monotonic()
    dummy = bool(getattr(args, "dummy_login", False))
    try:
        session = gateway.connect()
    except Exception as exc:
        status = gateway.status()
        counts = status.get("counts") or {}
        # “柜台应答了”与“请求根本没得到应答”是两回事：只有前者才能证明链路与原生库正常
        answered = status.get("last_error_code") is not None
        expected_rejection = dummy and answered
        detail = {
            "error_type": type(exc).__name__,
            "front_connected": counts.get("front_connected"),
            "front_disconnected": counts.get("front_disconnected"),
            "fault": status.get("fault"),
            "counter_error_code": status.get("last_error_code"),
            # 柜台报文只用于诊断；它不含凭证，但仍按原样记录以便对照 CTP 错误码表
            "counter_error_message": status.get("last_error_message"),
        }
        record(
            Step(
                "connect_login_settlement",
                "rejected" if expected_rejection else "failed",
                detail,
                time.monotonic() - started,
            )
        )
        report["gateway_status"] = dict(gateway.status())
        record_native_libs(report, gateway)
        if dummy:
            if args.market_symbol:
                # 行情前置不校验口令（openctp 实测），因此无账号也能把行情通道跑到底：登录 + 订阅 + 收快照
                market_report, _ = probe_market(args, gateway, sink)
                report["market_probe"] = market_report
            else:
                report["market_reachability"] = probe_market_reachability(args, settings, sink)
            report["login_probe"] = {
                "kind": "unregistered_dummy_account",
                "counter_answered": answered,
                "user_id": mask_identifier(DUMMY_PROBE_USER),
                "front_connected": counts.get("front_connected"),
                "front_disconnected": counts.get("front_disconnected"),
                "counter_error_code": status.get("last_error_code"),
                "counter_error_message": status.get("last_error_message"),
                "fault": status.get("fault"),
                "interpretation": (
                    "柜台在同一连接上应答了登录请求：说明登记的原生库已装载且前置可达。"
                    "本记录是预期内的拒绝，不构成登录通过，也不构成柜台能力核验。"
                    if answered
                    else "登录请求没有等到柜台应答：不能用它证明链路或原生库正常。"
                ),
            }
            return report, DUMMY_PROBE_EXIT_CODE if expected_rejection else 1
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
    report["native_libs"] = session.native_libs
    report["login_probe"] = {
        "kind": "unregistered_dummy_account" if dummy else "registered_account",
        "passed": True,
    }

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

    if args.cancel_active_symbol:
        cancel_report, cancel_exit = probe_cancel_active(args, gateway, queries)
        report["cancel_cleanup"] = cancel_report
        exit_code = max(exit_code, cancel_exit)

    if args.verify_catalog:
        catalog_report, catalog_exit = probe_catalog(args, queries)
        report["catalog_check"] = catalog_report
        exit_code = max(exit_code, catalog_exit)
        out_dir = (ROOT / args.out).resolve() if args.out else None
        if out_dir is not None and out_dir.is_relative_to(ROOT / "runs"):
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            diff_path = out_dir / f"ctp_catalog_diff_{stamp}.json"
            payload = json.dumps(catalog_report, ensure_ascii=False, indent=2, default=str) + "\n"
            diff_path.write_text(payload, encoding="utf-8")
            report["catalog_diff_file"] = diff_path.relative_to(ROOT).as_posix()

    if args.verify_rates:
        rate_report, rate_exit = probe_rates(args, queries)
        report["rate_check"] = rate_report
        exit_code = max(exit_code, rate_exit)
        out_dir = (ROOT / args.out).resolve() if args.out else None
        if out_dir is not None and out_dir.is_relative_to(ROOT / "runs"):
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            rate_path = out_dir / f"ctp_rate_evidence_{stamp}.json"
            payload = json.dumps(rate_report, ensure_ascii=False, indent=2, default=str) + "\n"
            rate_path.write_text(payload, encoding="utf-8")
            report["rate_evidence_file"] = rate_path.relative_to(ROOT).as_posix()

    if args.market_symbol:
        market_report, market_exit = probe_market(args, gateway, sink)
        report["market_probe"] = market_report
        exit_code = max(exit_code, market_exit)

    if args.order_symbol:
        order_report, order_exit = probe_order(args, gateway, queries, sink, ref_book, price_tick)
        report["order_probe"] = order_report
        exit_code = max(exit_code, order_exit)

    if args.position_probe:
        position_report, position_exit = ctp_position_probe.run_position_probe(
            instrument=parse_symbol(args.position_probe),
            quantity=int(args.position_quantity),
            account_id=args.account,
            controller_id=PROBE_CONTROLLER,
            epoch=PROBE_EPOCH,
            gateway=gateway,
            queries=queries,
            sink=sink,
            price_tick=price_tick,
            wait_s=args.fill_wait,
            market_order_probe=bool(args.market_order_probe),
            probe_all_close_flags=bool(args.all_close_flags),
            cross_ticks=int(args.cross_ticks),
            allow_non_trading_day=bool(args.allow_non_trading_day),
        )
        report["position_probe"] = position_report
        exit_code = max(exit_code, position_exit)

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


def probe_cancel_active(
    args: argparse.Namespace, gateway: CtpTraderGateway, queries: CtpQueryAdapter
) -> tuple[Mapping[str, object], int]:
    """撤销柜台当前活动报单（只针对给定合约）——用于清理历史探测留下的委托.

    只做“查询 → 按可唯一归属的标识撤单”，不猜测、不按品种批量误撤；每笔都记录柜台应答。
    """
    instrument = parse_symbol(args.cancel_active_symbol)
    detail: dict[str, object] = {"instrument": str(instrument)}
    try:
        active = queries.query_orders(queries.query_batch("order"))
    except Exception as exc:
        detail["error"] = f"order query failed ({type(exc).__name__})"
        return detail, 1
    targets = [update for update in active.records if update.instrument == instrument]
    detail["active_for_instrument"] = len(targets)
    detail["active_total"] = len(active.records)
    if not targets:
        detail["result"] = "no active order for this instrument"
        return detail, 0
    if not gateway.mark_reconciled():
        detail["error"] = "the counter session is not reconciled; cancelling is refused"
        return detail, 2
    outcomes: list[Mapping[str, object]] = []
    exit_code = 0
    for update in targets:
        book = gateway.ref_book
        if update.identity.order_ref is None and not update.identity.exchange_order_id:
            outcomes.append(
                {"order_ref": None, "status": str(update.status), "result": "no unique identifier on the report"}
            )
            exit_code = max(exit_code, 3)
            continue
        # 查询回报没有本地单号：只为撤单登记“该委托属于这个合约”，归属仍按柜台回报给出的标识
        placeholder = (
            f"cancel-cleanup:{instrument.symbol}:{update.identity.order_ref or update.identity.exchange_order_id}"
        )
        book.remember_instrument(placeholder, instrument)
        identity = OrderIdentity(
            account_id=args.account,
            exchange=instrument.exchange,
            client_order_id=placeholder,
            exchange_order_id=update.identity.exchange_order_id,
            front_id=update.identity.front_id,
            session_id=update.identity.session_id,
            order_ref=update.identity.order_ref,
        )
        result = gateway.cancel(identity, PROBE_EPOCH)
        outcomes.append(
            {
                "order_ref": update.identity.order_ref,
                "locator": "session" if update.identity.front_id is not None else "exchange",
                "order_sys_id": update.identity.exchange_order_id,
                "state": str(result.state),
                "local_code": result.local_code,
                "evidence": result.evidence,
            }
        )
        if str(result.state) == "NOT_SENT":
            exit_code = max(exit_code, 3)
        time.sleep(1.5)
    detail["cancellations"] = outcomes
    remaining = queries.query_orders(queries.query_batch("order")).records
    detail["remaining_active"] = len(remaining)
    detail["result"] = "cancellation sent for every matched order" if not exit_code else "some orders were refused"
    return detail, exit_code


def probe_catalog(args: argparse.Namespace, queries: CtpQueryAdapter) -> tuple[Mapping[str, object], int]:
    """柜台品种 / 交易所 / 投资者 / 用户会话查询，并与本地品种登记比对 (FR-RULE-05, A23).

    只读查询，交易日无关；费率与保证金查询在休市日返回空，这里不把它们当成已核验口径。
    """
    detail: dict[str, object] = {}
    try:
        products = queries.query_products()
        exchanges = queries.query_exchanges()
        investor = queries.query_investor()
        sessions = queries.query_user_sessions()
    except Exception as exc:
        detail["error"] = f"counter queries failed ({type(exc).__name__})"
        return detail, 1
    detail["counter_products"] = len(products)
    detail["counter_exchanges"] = [str(item.get("ExchangeID")) for item in exchanges]
    detail["investor"] = {
        "investor_id": None if investor is None else investor.get("InvestorID"),
        "active": None if investor is None else investor.get("IsActive"),
        "name_present": bool(investor and investor.get("InvestorName")),
    }
    detail["user_sessions"] = [
        {"front_id": item.get("FrontID"), "session_id": item.get("SessionID")} for item in sessions
    ]
    comparison = ctp_setup.compare_products(products)
    detail["comparison"] = comparison
    # 品种级口径在部分柜台（实测 openctp TTS）恒返 0，因此真正的核验落在合约级 ReqQryInstrument
    instruments = queries.query_all_instruments()
    detail["counter_instruments"] = len(instruments)
    instrument_comparison = ctp_setup.compare_instruments(instruments)
    detail["instrument_comparison"] = instrument_comparison
    detail["result"] = instrument_comparison["result"]
    if instrument_comparison["mismatches"] or comparison["mismatches"]:
        return detail, 2
    if instrument_comparison["missing_at_counter"]:
        return detail, 2
    return detail, 0


COMMISSION_RATE_FIELDS = (
    "OpenRatioByMoney",
    "OpenRatioByVolume",
    "CloseRatioByMoney",
    "CloseRatioByVolume",
    "CloseTodayRatioByMoney",
    "CloseTodayRatioByVolume",
    "BizType",
)
MARGIN_RATE_FIELDS = (
    "HedgeFlag",
    "LongMarginRatioByMoney",
    "LongMarginRatioByVolume",
    "ShortMarginRatioByMoney",
    "ShortMarginRatioByVolume",
    "IsRelative",
)


def _trim(record: Mapping[str, object], names: Sequence[str]) -> dict[str, object]:
    return {name: record.get(name) for name in names if record.get(name) is not None}


def nearest_listed_contracts(
    counter_instruments: Sequence[Mapping[str, object]],
) -> tuple[dict[str, str], list[str]]:
    """每个本地登记品种取柜台清单里最近的上市合约作为费率核验样本.

    品种级口径在部分柜台（实测 openctp TTS）恒返 0，因此费率核验按合约取样；柜台没有该品种的
    可交易合约时把品种列入 ``absent``，不拿别的品种或已摘牌合约替代。
    """
    registry = ctp_setup.product_specs()
    by_product: dict[tuple[str, str], list[str]] = {}
    for record in counter_instruments:
        product_id = record.get("ProductID")
        exchange = record.get("ExchangeID")
        symbol = record.get("InstrumentID")
        if not isinstance(product_id, str) or not isinstance(exchange, str) or not isinstance(symbol, str):
            continue
        if str(record.get("IsTrading", "")) != "1":
            continue
        by_product.setdefault((product_id.upper(), exchange), []).append(symbol)
    selected: dict[str, str] = {}
    absent: list[str] = []
    for name, spec in sorted(registry.items()):
        candidates = sorted(by_product.get((name, spec.exchange.value), []))
        if not candidates:
            absent.append(f"{spec.exchange.value}.{name}")
            continue
        selected[f"{spec.exchange.value}.{candidates[0]}"] = name
    return selected, absent


def probe_rates(args: argparse.Namespace, queries: CtpQueryAdapter) -> tuple[Mapping[str, object], int]:
    """柜台手续费率 / 保证金率与本地品种登记比对 (FR-RULE-05).

    只读查询；柜台返回空（休市日或该柜台不支持）时记为"柜台未给出该口径"，不当成不一致，也不当成
    已核验。命令行给了 ``--rate-symbols`` 就只查这些合约，否则每个登记品种取柜台最近的上市合约。
    """
    requested = [item.strip() for item in str(getattr(args, "rate_symbols", "") or "").split(",") if item.strip()]
    absent: list[str] = []
    counter_only: list[str] = []
    if requested:
        products = {symbol: "".join(ch for ch in symbol.split(".")[-1] if ch.isalpha()) for symbol in requested}
        source = "命令行 --rate-symbols"
    else:
        instruments = queries.query_all_instruments()
        pairs, absent = nearest_listed_contracts(instruments)
        products = {symbol: product for symbol, product in pairs.items()}
        counter_only = _counter_only_contracts(instruments)
        source = "柜台合约清单（每品种最近的上市合约）"
    observations: list[dict[str, object]] = []
    errors: list[str] = []
    for symbol, product in products.items():
        observation, error = _rate_observation(queries, symbol, product)
        if error:
            errors.append(error)
            continue
        observations.append(observation)
    # 柜台自有合约（本地无登记）：只登记柜台口径，用于证明查询通道本身可用
    counter_only_observations: list[dict[str, object]] = []
    for symbol in counter_only:
        observation, error = _rate_observation(queries, symbol, None)
        if error:
            errors.append(error)
            continue
        counter_only_observations.append(observation)
    comparison = ctp_setup.compare_contract_rates(observations)
    detail: dict[str, object] = {
        "symbol_source": source,
        "symbols": sorted(products),
        "products_without_listed_contracts": absent,
        "observations": observations,
        "comparison": comparison,
        "counter_only_observations": counter_only_observations,
        "errors": errors,
        "result": comparison["result"],
    }
    return detail, 2 if (comparison["mismatches"] or errors) else 0


def _rate_observation(
    queries: CtpQueryAdapter, symbol: str, product: str | None
) -> tuple[dict[str, object], str | None]:
    # 柜台查询只认裸合约代码：登记里的 "SHFE.rb2610" 直接当 InstrumentID 发出去会得到空记录
    bare = symbol.split(".")[-1]
    try:
        commission = queries.query_commission_rate(bare)
        margin = queries.query_margin_rate(bare)
    except Exception as exc:
        return {}, f"{symbol}:{type(exc).__name__}"
    return (
        {
            "instrument": symbol,
            "product": product,
            "commission": None if commission is None else _trim(commission, COMMISSION_RATE_FIELDS),
            "margin": None if margin is None else _trim(margin, MARGIN_RATE_FIELDS),
        },
        None,
    )


def _counter_only_contracts(counter_instruments: Sequence[Mapping[str, object]], *, limit: int = 3) -> list[str]:
    """柜台自有交易所的上市合约（本地无登记）：优先 TTS 自有的撮合合约，最多取若干条.

    实测这类合约才在柜台配了费率与保证金（重放的真实市场合约返回空记录），因此它们是"查询通道可用"
    的唯一证人；按交易所各取一条，不重复。
    """
    registered = {spec.exchange.value for spec in ctp_setup.product_specs().values()}
    by_exchange: dict[str, list[str]] = {}
    for record in counter_instruments:
        exchange = record.get("ExchangeID")
        symbol = record.get("InstrumentID")
        if not isinstance(exchange, str) or not isinstance(symbol, str) or exchange in registered:
            continue
        if str(record.get("IsTrading", "")) != "1":
            continue
        by_exchange.setdefault(exchange, []).append(symbol)
    ordered = sorted(by_exchange, key=lambda name: (name != "TTS", name))
    selected: list[str] = []
    for exchange in ordered[:limit]:
        selected.append(sorted(by_exchange[exchange])[0])
    return selected


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
    parser.add_argument(
        "--query-timeout",
        type=float,
        default=30.0,
        help="单次查询等待应答的秒数（全量合约清单在 openctp TTS 上 >20 秒，故默认 30 秒）",
    )
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
    parser.add_argument(
        "--terminal-mode",
        choices=["none", "direct", "relay"],
        default=None,
        help="看穿式终端采集模式（默认从柜台登记读取；可选 none/direct/relay）",
    )
    parser.add_argument(
        "--collector-lib",
        default=None,
        help="看穿式采集动态库路径（WinDataCollect.dll / libDataCollect.so）",
    )
    parser.add_argument("--price-tick", default="1", help="报单探测使用的价格步长（须与合约登记一致）")
    parser.add_argument(
        "--position-probe",
        default=None,
        help="会成交的持仓探测：穿价建仓后逐个开平标志平仓，如 SHFE.rb2610（仿真环境专用）",
    )
    parser.add_argument("--position-quantity", type=int, default=1, help="持仓探测的手数（默认 1 手）")
    parser.add_argument("--fill-wait", type=float, default=10.0, help="等待成交 / 终局状态的秒数")
    parser.add_argument(
        "--market-order-probe",
        action="store_true",
        help="附加市价单探测：用 AnyPrice 再建一笔仓并平掉（登记里市价单仍未核验，仅供探测）",
    )
    parser.add_argument(
        "--cross-ticks",
        type=int,
        default=2,
        help="持仓探测的穿价余量（跳）：做市模式要求高于叫卖价才立即成交（默认 2 跳）",
    )
    parser.add_argument(
        "--all-close-flags",
        action="store_true",
        help="对每个候选开平标志各建一次仓再平一次：既证明哪个能用，也证明哪个被拒",
    )
    parser.add_argument(
        "--cancel-active-symbol",
        default=None,
        help="撤销该合约在柜台的全部活动报单（清理历史探测留下的委托），如 SHFE.rb2610",
    )
    parser.add_argument(
        "--verify-catalog",
        action="store_true",
        help="查询柜台品种 / 交易所 / 投资者 / 用户会话，并与本地品种登记比对（写入 runs/s0/ctp_catalog_diff_*.json）",
    )
    parser.add_argument(
        "--verify-rates",
        action="store_true",
        help="查询柜台手续费率 / 保证金率并与本地品种登记比对（写入 runs/<out>/ctp_rate_evidence_*.json）",
    )
    parser.add_argument(
        "--rate-symbols",
        default=None,
        help="费率核验的合约清单（逗号分隔）；默认每个登记品种取柜台最近的上市合约",
    )
    parser.add_argument("--market-symbol", default=None, help="可选：订阅行情并收集逐笔快照，如 SHFE.rb2610")
    parser.add_argument(
        "--dummy-login",
        action="store_true",
        help="无凭据联调：用虚构账号发起一次登录，只验证原生库与前置，须同时显式指定 --profile",
    )
    parser.add_argument("--dummy-user", default=DUMMY_PROBE_USER, help="无凭据联调使用的虚构用户号")
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
    explicit_account = args.account
    config_path = ROOT / args.config
    config_data: dict[str, Any] = {}
    if config_path.is_file():
        import yaml

        config_data = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}
    broker = config_data.get("broker") or {}
    if args.profile is None:
        args.profile = broker.get("profile")
    if broker.get("profile") == args.profile:
        # 账号标识只从“与所选登记一致”的本地配置取，避免把 SimNow 账号带进 openctp 环境
        args.user = args.user or broker.get("user_id")
        args.investor = args.investor or broker.get("investor_id")
        if explicit_account is None:
            args.account = (config_data.get("risk") or {}).get("account_id")
    if args.dummy_login and args.profile is None:
        # 无凭据联调不能落到运行配置的环境上：用错环境会把柜台行为当成网络故障解读
        print("无凭据联调必须显式指定 --profile（如 --profile openctp_tts）", file=sys.stderr)
        return 2
    if args.dummy_login:
        args.user = args.user or args.dummy_user
        args.investor = args.investor or args.dummy_user
        os.environ[ctp_setup.PASSWORD_ENV] = DUMMY_PROBE_PASSWORD
        if explicit_account is None:
            args.account = DUMMY_PROBE_ACCOUNT
        print(f"无凭据联调：使用虚构探测账号 {args.dummy_user}，口令为脚本内虚构值，不代表任何客户账号")
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
    if report.get("cancel_cleanup"):
        cleanup = report["cancel_cleanup"]
        print(
            f"活动委托清理: 目标 {cleanup.get('active_for_instrument')} 笔，"
            f"剩余 {cleanup.get('remaining_active')}，{cleanup.get('result') or cleanup.get('error')}"
        )
    if report.get("catalog_check"):
        check = report["catalog_check"]
        product = check.get("comparison") or {}
        instrument = check.get("instrument_comparison") or {}
        print(
            f"柜台口径比对: 品种 {check.get('counter_products')} 个（不一致 {len(product.get('mismatches', []))}，"
            f"柜台未给出 {len(product.get('counter_absent', []))}），"
            f"合约 {check.get('counter_instruments')} 个（不一致 {len(instrument.get('mismatches', []))}，"
            f"柜台缺失 {len(instrument.get('missing_at_counter', []))}）"
        )
        print(f"    {check.get('result')}")
        print(f"    比对文件: {report.get('catalog_diff_file')}")
    if report.get("market_probe"):
        probe = report["market_probe"]
        print(f"行情探测: ticks={probe.get('ticks')} {probe.get('result') or probe.get('error')}")
    if report.get("position_probe"):
        check = report["position_probe"]
        print(f"持仓探测: {check.get('result') or check.get('error')}")
        confirmed = check.get("close_confirmed") or {}
        if confirmed:
            print(f"    平仓确认: {confirmed.get('meaning')}（flag={confirmed.get('flag')}）")
        residual = check.get("residual_check") or {}
        print(
            f"    残留持仓: {residual.get('open_position_after')} 手（已平={residual.get('flat')}，"
            f"查询完整={residual.get('query_complete')}）"
        )
        for warning in check.get("warnings") or []:
            print(f"    注意: {warning}")
    if report.get("order_probe"):
        outcome = report["order_probe"].get("result") or report["order_probe"].get("error")
        print(f"报单探测: {json.dumps(outcome, ensure_ascii=False)}")
    if report.get("native_libs"):
        staged = report["native_libs"]
        print(f"原生库: {staged.get('flavor')}（api_marker={staged.get('api_marker')}，loaded={staged.get('loaded')}）")
        for item in staged.get("files") or []:
            print(f"    {item.get('loader_name')} {item.get('sha256')}")
    if report.get("login_probe"):
        probe = report["login_probe"]
        if probe.get("kind") == "unregistered_dummy_account":
            if probe.get("counter_answered"):
                print(
                    f"无凭据联调: 柜台已应答登录请求并被拒绝（code={probe.get('counter_error_code')} "
                    f"{probe.get('counter_error_message')}）；本记录不构成登录通过"
                )
            else:
                print(
                    f"无凭据联调失败: 登录未得到柜台应答（fault={probe.get('fault')}，"
                    f"disconnect={probe.get('front_disconnected')}）；清单与故障见证据文件"
                )
        else:
            print(f"登录证据: {probe.get('kind')}")
    if report.get("rate_check"):
        check = report["rate_check"]
        comparison = check.get("comparison") or {}
        print(
            f"费率与保证金核验: 合约 {len(check.get('symbols') or [])} 个（不一致 {len(comparison.get('mismatches', []))}，"
            f"柜台未给出 {len(comparison.get('counter_absent', []))}）"
        )
        print(f"    {check.get('result')}")
        counter_only = check.get("counter_only_observations") or []
        if counter_only:
            print(f"    柜台自有合约（本地无登记）: {[item['instrument'] for item in counter_only]}")
        print(f"    证据文件: {report.get('rate_evidence_file')}")
    if report.get("market_reachability"):
        reachability = report["market_reachability"]
        print(
            f"行情前置: {reachability.get('front_market')} -> {reachability.get('result')}"
            + (
                ""
                if reachability.get("session")
                else f"（code={reachability.get('counter_error_code')} {reachability.get('counter_error_message')}）"
            )
        )
    print(f"证据文件: {path.relative_to(ROOT).as_posix()}")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
