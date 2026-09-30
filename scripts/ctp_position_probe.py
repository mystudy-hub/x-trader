#!/usr/bin/env python
"""[脚本工具] 持仓 / 开平标志 / 市价单柜台实测 (S5-01, FR-ORD-01/02, GAP-S0-05).

目的：把"今昨仓映射"和"市价单是否被接受"从登记缺口变成柜台实测结论。做法是在仿真柜台（openctp TTS
7x24 / 仿真，或任何仿真柜台）上真的建一笔仓，再按候选开平标志平掉，全程只记录柜台应答：

1. 先查一次持仓：如果上一轮探测留下残留仓位，**先按候选标志把它平掉**（探测因此是幂等且自愈的）；
2. 两种模式：
   - 单一循环（默认）：穿价限价单（买价 = 卖一价）建 1 手多仓，再按候选标志顺序平仓，能平掉就停；
   - 候选矩阵（``--all-close-flags``）：**每个**候选标志各建一次仓再平一次，因此既能证明"哪个能用"，
     也能证明"哪个不能用"（今昨仓映射要的正是后者）；
3. 可选（``--market-order-probe``）：用市价单（``AnyPrice``）再建一笔仓，确认柜台是否受理，再按已确认
   的标志平掉；
4. 结束时再次查询持仓与资金：任何残留持仓都被大声登记（退出码 5），绝不当成功。

边界与风险：

- 这是**会成交**的探测：它在仿真柜台留下真实成交与手续费。只应在仿真环境运行，且必须能平掉。
- 开平标志与市价单在项目里属"未核验即禁用"的能力，因此这里显式构造**仅供探测使用**的能力档案与开平
  映射（``verified=True``、证据写 ``probe:*``），并在证据里标注；执行服务仍然只认登记里的核验状态。
- 价格参考取柜台快照的买一 / 卖一：没有双边报价就明确失败，不用涨跌停价或上次价猜。
- 资金差额里混着买卖价差与手续费：本探测只登记柜台给出的余额 / 保证金变化，不把它当成纯手续费口径。
- 平仓不被接受时柜台会回一条明确拒单（实测 openctp TTS：错误码 1009 持仓不足）；若柜台什么都不回，
  记为 ``no terminal status``——两种情形都绝不当成已平仓。
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from qh_trader.core.constants import EventKind, Exchange, Offset, OrderStatus, OrderType, SendState, Side
from qh_trader.core.objects import (
    Capability,
    CapabilityProfile,
    ControlEpoch,
    InstrumentId,
    OrderIntent,
)
from qh_trader.gateway.ctp_gateway import (
    CtpOffsetMapping,
    CtpOrderRef,
    CtpTraderGateway,
    order_ref_evidence,
)
from qh_trader.gateway.ctp_query import CtpQueryAdapter

PROBE_CAPABILITY_EVIDENCE = "probe:ctp_position_probe"
OFFSET_OPEN_FLAG = "0"  # THOST_FTDC_OF_Open：由头文件唯一定义，与交易所无关
#: 候选开平标志（CTP 头文件口径）；本探测记录柜台实际接受哪一个，而不是假定映射成立
CANDIDATE_OFFSETS: tuple[tuple[Offset, str, str], ...] = (
    (Offset.CLOSE_TODAY, "3", "平今 THOST_FTDC_OF_CloseToday"),
    (Offset.CLOSE, "1", "平仓 THOST_FTDC_OF_Close"),
    (Offset.CLOSE_YESTERDAY, "4", "平昨 THOST_FTDC_OF_CloseYesterday"),
)
RESIDUAL_EXIT_CODE = 5


def probe_capability_profile(base: CapabilityProfile) -> CapabilityProfile:
    """**仅供探测**的能力档案：把市价单标成已核验，好让网关把请求真的发到柜台上.

    这不是能力核验结论：探测记录的是柜台怎么回答。执行服务仍��只读登记里的状态（市价单仍是登记缺口）。
    """
    values = dict(base.values)
    values["order_types.market_order"] = Capability(True, True, PROBE_CAPABILITY_EVIDENCE)
    return CapabilityProfile(profile_id=base.profile_id, ctp_version=base.ctp_version, values=values)


def probe_offset_mapping(exchange: Exchange) -> CtpOffsetMapping:
    """**仅供探测**的开平映射：把平今 / 平仓 / 平昨三个候选标志都挂上，逐个试."""
    flags: dict[Offset, str] = {Offset.OPEN: OFFSET_OPEN_FLAG}
    for offset, flag, _ in CANDIDATE_OFFSETS:
        flags[offset] = flag
    return CtpOffsetMapping(exchange=exchange, flags=flags, verified=True, evidence_ref=PROBE_CAPABILITY_EVIDENCE)


def _wait_statuses(sink: Any, ref: CtpOrderRef, wanted: set[str], timeout_s: float) -> list[str]:
    """按原会话三元组等待某组状态出现；返回截至超时的全序列."""
    deadline = time.monotonic() + max(0.5, timeout_s)
    statuses = sink.order_updates(ref)
    while time.monotonic() < deadline:
        statuses = sink.order_updates(ref)
        if wanted & set(statuses):
            break
        time.sleep(0.1)
    return statuses


def _filled_volume(sink: Any, instrument: InstrumentId, since: int) -> int:
    total = 0
    for event in sink.events[since:]:
        if event.kind != EventKind.TRADE_REPORT:
            continue
        trade = event.payload
        if str(getattr(trade, "instrument", "")) != str(instrument):
            continue
        total += int(getattr(trade, "quantity", 0) or 0)
    return total


def _position_rows(queries: CtpQueryAdapter, instrument: InstrumentId) -> tuple[list[dict[str, object]], bool]:
    """该合约的持仓行（含今昨仓）；返回 ``(行, 查询是否完整)``."""
    result = queries.query_positions(queries.query_batch("position"))
    rows = [
        {
            "side": str(item.side),
            "pos_yd": item.pos_yd,
            "pos_td": item.pos_td,
            "frozen_yd": item.frozen_yd,
            "frozen_td": item.frozen_td,
        }
        for item in result.records
        if str(item.instrument) == str(instrument)
    ]
    return rows, bool(result.complete)


def _open_position(rows: Sequence[Mapping[str, object]]) -> int:
    return sum(int(row.get("pos_yd") or 0) + int(row.get("pos_td") or 0) for row in rows)


def _wait_position(
    queries: CtpQueryAdapter, instrument: InstrumentId, *, want_flat: bool, timeout_s: float
) -> tuple[list[dict[str, object]], bool]:
    """轮询持仓直到仓位归零（或建起来）；柜台持仓查询可能滞后于成交通知."""
    deadline = time.monotonic() + max(1.0, timeout_s)
    rows, complete = _position_rows(queries, instrument)
    while time.monotonic() < deadline:
        rows, complete = _position_rows(queries, instrument)
        if (want_flat and _open_position(rows) == 0) or (not want_flat and _open_position(rows) > 0):
            break
        time.sleep(0.5)
    return rows, complete


def _funds(queries: CtpQueryAdapter) -> dict[str, object]:
    result = queries.query_account(queries.query_batch("account"))
    if not result.records:
        return {"complete": bool(result.complete), "records": 0}
    funds = result.records[0]
    return {
        "complete": bool(result.complete),
        "records": len(result.records),
        "balance": None if funds.balance is None else str(funds.balance),
        "margin": None if funds.margin is None else str(funds.margin),
        "available": None if funds.available_for_new_trades is None else str(funds.available_for_new_trades),
    }


def _submit(
    gateway: CtpTraderGateway,
    intent: OrderIntent,
    epoch: ControlEpoch,
    sink: Any,
    *,
    wait_s: float,
) -> dict[str, object]:
    """发送一笔委托并等待终局状态；返回证据块（不改状态、不推断成交结果）."""
    since = len(sink.events)
    send_result = gateway.submit(intent, epoch)
    evidence: dict[str, object] = {
        "client_order_id": intent.client_order_id,
        "side": str(intent.side),
        "offset": str(intent.offset),
        "order_type": str(intent.order_type),
        "quantity": int(intent.quantity),
        "state": str(send_result.state),
        "local_code": send_result.local_code,
        "send_evidence": send_result.evidence,
    }
    if intent.limit_price_ticks is not None:
        evidence["limit_price_ticks"] = int(intent.limit_price_ticks)
    if send_result.state == SendState.NOT_SENT:
        evidence["result"] = "refused locally; nothing was sent"
        return evidence
    if send_result.remote_identity is None:
        evidence["result"] = "no remote identity; the outcome stays unknown"
        return evidence
    identity = send_result.remote_identity
    ref = CtpOrderRef(
        front_id=int(identity.front_id or 0),
        session_id=int(identity.session_id or 0),
        order_ref=str(identity.order_ref),
    )
    terminal = {str(OrderStatus.FILLED), str(OrderStatus.CANCELLED), str(OrderStatus.REJECTED)}
    statuses = _wait_statuses(sink, ref, terminal, wait_s)
    evidence["order_ref"] = order_ref_evidence(ref)
    evidence["statuses"] = statuses
    evidence["filled_volume"] = _filled_volume(sink, intent.instrument, since)
    rejections = getattr(gateway, "counter_rejections", ())
    matching = [dict(item) for item in rejections if str(item.get("order_ref") or "") == str(ref.order_ref)]
    if matching:
        # 柜台为什么拒：错误码与报文。没有这一步就只看得到"被拒"，看不到"为何被拒"
        evidence["counter_rejection"] = matching[-1]
    evidence["result"] = (
        "filled"
        if str(OrderStatus.FILLED) in statuses
        else ("terminal" if terminal & set(statuses) else "no terminal status")
    )
    if evidence["result"] == "no terminal status":
        evidence["note"] = "the counter reported nothing for this request; it is neither filled nor confirmed refused"
    return evidence


def _close_until_flat(
    *,
    instrument: InstrumentId,
    quantity: int,
    account_id: str,
    controller_id: str,
    epoch: ControlEpoch,
    gateway: CtpTraderGateway,
    queries: CtpQueryAdapter,
    sink: Any,
    sell_ticks: int,
    wait_s: float,
    order: Sequence[tuple[Offset, str, str]],
    label: str,
) -> tuple[list[dict[str, object]], dict[str, object] | None, list[dict[str, object]], bool]:
    """按给定顺序平仓直到仓位归零；返回 ``(尝试记录, 成功的尝试, 最终持仓行, 是否已平)``."""
    attempts: list[dict[str, object]] = []
    confirm: dict[str, object] | None = None
    rows: list[dict[str, object]] = []
    for offset, flag, meaning in order:
        intent = OrderIntent(
            client_order_id=f"probe-{label}-{flag}-{datetime.now(timezone.utc).strftime('%H%M%S%f')}",
            account_id=account_id,
            strategy_id=controller_id,
            instrument=instrument,
            side=Side.SELL,
            offset=offset,
            quantity=int(quantity),
            order_type=OrderType.LIMIT,
            created_at=datetime.now(timezone.utc),
            limit_price_ticks=sell_ticks,
        )
        closed = _submit(gateway, intent, epoch, sink, wait_s=wait_s)
        rows, complete = _wait_position(queries, instrument, want_flat=True, timeout_s=wait_s)
        attempts.append(
            {
                "offset": str(offset),
                "offset_enum": offset,
                "flag": flag,
                "meaning": meaning,
                "order": closed,
                "position_after": {"rows": rows, "query_complete": complete},
                "open_position_after": _open_position(rows),
            }
        )
        if _open_position(rows) == 0:
            confirm = attempts[-1]
            break
    return attempts, confirm, rows, _open_position(rows) == 0


def probe_each_candidate_flag(
    *,
    instrument: InstrumentId,
    quantity: int,
    account_id: str,
    controller_id: str,
    epoch: ControlEpoch,
    gateway: CtpTraderGateway,
    queries: CtpQueryAdapter,
    sink: Any,
    buy_ticks: int,
    sell_ticks: int,
    wait_s: float,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """对每个候选开平标志各建一次仓再平一次：得到"有仓位时该标志被接受还是被拒"的完整证据.

    只按第一个可用标志平仓只能证明"它能用"，证明不了"别的不能用"；今昨仓映射要的正是后者，所以这项
    显式给每个候选都造出可平的仓位。返回 ``(每个标志的结果, 被接受的标志)``。
    """
    results: list[dict[str, object]] = []
    accepted: list[dict[str, object]] = []
    for offset, flag, meaning in CANDIDATE_OFFSETS:
        rows, _ = _position_rows(queries, instrument)
        opened: dict[str, object] | None = None
        if _open_position(rows) == 0:
            intent = OrderIntent(
                client_order_id=f"probe-flag-open-{flag}-{datetime.now(timezone.utc).strftime('%H%M%S%f')}",
                account_id=account_id,
                strategy_id=controller_id,
                instrument=instrument,
                side=Side.BUY,
                offset=Offset.OPEN,
                quantity=int(quantity),
                order_type=OrderType.LIMIT,
                created_at=datetime.now(timezone.utc),
                limit_price_ticks=buy_ticks,
            )
            opened = _submit(gateway, intent, epoch, sink, wait_s=wait_s)
            if opened.get("result") != "filled":
                results.append(
                    {"flag": flag, "meaning": meaning, "open": opened, "skipped": "the case could not be opened"}
                )
                continue
        position_before, _ = _wait_position(queries, instrument, want_flat=False, timeout_s=wait_s)
        close_intent = OrderIntent(
            client_order_id=f"probe-flag-close-{flag}-{datetime.now(timezone.utc).strftime('%H%M%S%f')}",
            account_id=account_id,
            strategy_id=controller_id,
            instrument=instrument,
            side=Side.SELL,
            offset=offset,
            quantity=int(quantity),
            order_type=OrderType.LIMIT,
            created_at=datetime.now(timezone.utc),
            limit_price_ticks=sell_ticks,
        )
        closed = _submit(gateway, close_intent, epoch, sink, wait_s=wait_s)
        rows_after, complete = _wait_position(queries, instrument, want_flat=True, timeout_s=wait_s)
        closed_position = _open_position(rows_after) == 0
        results.append(
            {
                "flag": flag,
                "meaning": meaning,
                "open": opened,
                "close": closed,
                "position_before_close": position_before,
                "position_after": {"rows": rows_after, "query_complete": complete},
                "closed_position": closed_position,
            }
        )
        if closed_position and closed.get("result") == "filled":
            accepted.append({"offset": offset, "offset_enum": offset, "flag": flag, "meaning": meaning})
    return results, accepted


def _single_cycle(
    *,
    instrument: InstrumentId,
    quantity: int,
    account_id: str,
    controller_id: str,
    epoch: ControlEpoch,
    gateway: CtpTraderGateway,
    queries: CtpQueryAdapter,
    sink: Any,
    wait_s: float,
    buy_ticks: int,
    sell_ticks: int,
    detail: dict[str, object],
    warnings: list[str],
    record: Any,
) -> tuple[dict[str, object] | None, int | None]:
    """一次"穿价建仓 → 按候选标志平仓"循环；返回 ``(确认的标志, 失败退出码)``."""
    stub = datetime.now(timezone.utc)
    opened = _submit(
        gateway,
        OrderIntent(
            client_order_id=f"probe-pos-open-{stub.strftime('%H%M%S%f')}",
            account_id=account_id,
            strategy_id=controller_id,
            instrument=instrument,
            side=Side.BUY,
            offset=Offset.OPEN,
            quantity=int(quantity),
            order_type=OrderType.LIMIT,
            created_at=stub,
            limit_price_ticks=buy_ticks,
        ),
        epoch,
        sink,
        wait_s=wait_s,
    )
    record("open_limit_at_ask", str(opened.get("result")), {"order": opened})
    if opened.get("result") != "filled":
        detail["error"] = "the crossing limit order did not fill; the offset probe needs a real position"
        return None, 3
    open_rows, open_complete = _wait_position(queries, instrument, want_flat=False, timeout_s=wait_s)
    detail["position_after_open"] = {"rows": open_rows, "query_complete": open_complete}
    if _open_position(open_rows) <= 0:
        warnings.append("柜台回报成交但持仓查询看不到仓位：持仓口径需要复核")

    attempts, closed_with, rows_after, flat = _close_until_flat(
        instrument=instrument,
        quantity=int(quantity),
        account_id=account_id,
        controller_id=controller_id,
        epoch=epoch,
        gateway=gateway,
        queries=queries,
        sink=sink,
        sell_ticks=sell_ticks,
        wait_s=wait_s,
        order=CANDIDATE_OFFSETS,
        label="close",
    )
    detail["close_attempts"] = attempts
    for attempt in attempts:
        order_detail = attempt.get("order") or {}
        if order_detail.get("result") != "filled":
            rejection = order_detail.get("counter_rejection") or {}
            warnings.append(
                f"{attempt['meaning']}（flag={attempt['flag']}）未被接受：status={order_detail.get('statuses')} "
                f"local_code={order_detail.get('local_code')} "
                f"counter_code={rejection.get('error_code')} {rejection.get('error_message') or ''}"
            )
    if not flat or closed_with is None:
        detail["error"] = "no candidate close flag closed the position; the counter needs manual review"
        detail["residual_check"] = _residual(queries, instrument)
        return None, 4
    detail["close_confirmed"] = {
        "offset": str(closed_with["offset"]),
        "flag": closed_with["flag"],
        "meaning": closed_with["meaning"],
        "position_before": detail["position_after_open"],
        "position_after": {"rows": rows_after},
    }
    return closed_with, None


def _matrix_cycle(
    *,
    instrument: InstrumentId,
    quantity: int,
    account_id: str,
    controller_id: str,
    epoch: ControlEpoch,
    gateway: CtpTraderGateway,
    queries: CtpQueryAdapter,
    sink: Any,
    wait_s: float,
    buy_ticks: int,
    sell_ticks: int,
    detail: dict[str, object],
    warnings: list[str],
    record: Any,
) -> tuple[dict[str, object] | None, int | None]:
    """每个候选标志各建一次仓再平一次，最后用可用的标志收尾；返回 ``(确认的标志, 失败退出码)``."""
    matrix, accepted = probe_each_candidate_flag(
        instrument=instrument,
        quantity=int(quantity),
        account_id=account_id,
        controller_id=controller_id,
        epoch=epoch,
        gateway=gateway,
        queries=queries,
        sink=sink,
        buy_ticks=buy_ticks,
        sell_ticks=sell_ticks,
        wait_s=wait_s,
    )
    detail["close_flag_matrix"] = matrix
    detail["accepted_close_flags"] = accepted
    record("close_flag_matrix", "measured", {"matrix": matrix})
    if not accepted:
        detail["error"] = "no candidate close flag closed a real position; the counter needs manual review"
        detail["residual_check"] = _residual(queries, instrument)
        return None, 4
    confirmed = {"offset": accepted[0]["offset"], "flag": accepted[0]["flag"], "meaning": accepted[0]["meaning"]}
    leftover, _ = _position_rows(queries, instrument)
    detail["position_after_matrix"] = leftover
    if _open_position(leftover) > 0:
        # 被拒的标志必然留下仓位：用已确认可用的标志收尾，并按同一套证据登记
        clean_attempts, cleaned, rows_cleaned, clean_flat = _close_until_flat(
            instrument=instrument,
            quantity=_open_position(leftover),
            account_id=account_id,
            controller_id=controller_id,
            epoch=epoch,
            gateway=gateway,
            queries=queries,
            sink=sink,
            sell_ticks=sell_ticks,
            wait_s=wait_s,
            order=[(confirmed["offset"], confirmed["flag"], confirmed["meaning"])],  # type: ignore[list-item]
            label="matrix-final",
        )
        detail["matrix_cleanup"] = {
            "attempts": clean_attempts,
            "position_after": {"rows": rows_cleaned},
            "flat": clean_flat,
        }
        record("flatten_after_matrix", "flat" if clean_flat else "residual", {"cleanup": detail["matrix_cleanup"]})
        if not clean_flat:
            detail["error"] = "the position left by a rejected flag could not be closed with the accepted flag"
            detail["residual_check"] = _residual(queries, instrument)
            return None, RESIDUAL_EXIT_CODE
        if cleaned is not None:
            confirmed = {
                "offset": cleaned["offset_enum"],
                "flag": cleaned["flag"],
                "meaning": cleaned["meaning"],
            }
    detail["close_confirmed"] = {
        "offset": str(confirmed["offset"]),
        "flag": confirmed["flag"],
        "meaning": confirmed["meaning"],
        "position_before": detail.get("position_after_open"),
        "position_after": {"rows": leftover},
    }
    return confirmed, None


def run_position_probe(
    *,
    instrument: InstrumentId,
    quantity: int,
    account_id: str,
    controller_id: str,
    epoch: ControlEpoch,
    gateway: CtpTraderGateway,
    queries: CtpQueryAdapter,
    sink: Any,
    price_tick: dict[str, Decimal],
    wait_s: float,
    market_order_probe: bool,
    probe_all_close_flags: bool = False,
    cross_ticks: int = 2,
    allow_non_trading_day: bool = False,
    expected_trading_day: date | None = None,
) -> tuple[dict[str, object], int]:
    """执行一次持仓 / 开平 / 市价探测；返回 ``(证据, 退出码)``."""
    steps: list[dict[str, object]] = []
    warnings: list[str] = []

    def record(name: str, status: str, payload: Mapping[str, object]) -> None:
        steps.append({"name": name, "status": status, **dict(payload)})

    detail: dict[str, object] = {
        "instrument": str(instrument),
        "quantity": int(quantity),
        "mode": "close_flag_matrix" if probe_all_close_flags else "single_cycle",
        "candidate_offsets": [
            {"offset": str(offset), "flag": flag, "meaning": meaning} for offset, flag, meaning in CANDIDATE_OFFSETS
        ],
        "probe_only_declarations": {
            "capability": "order_types.market_order 标为已核验仅用于本次探测（ref = probe:ctp_position_probe）",
            "offset_mapping": "平今 / 平仓 / 平昨候选标志按 CTP 头文件口径挂上，逐个试；登记里的核验状态不变",
        },
        "steps": steps,
        "warnings": warnings,
    }

    local_date = datetime.now(timezone(timedelta(hours=8))).date()
    if expected_trading_day is not None and gateway.trading_day != expected_trading_day:
        detail["error"] = "counter trading day differs from explicitly expected trading day; send is disabled"
        return detail, 2
    if (
        expected_trading_day is None
        and gateway.trading_day is not None
        and gateway.trading_day != local_date
        and not allow_non_trading_day
    ):
        # 会成交的探测必须落在柜台的交易时段：休市或环境滞后时成交与平仓都不可解释
        detail["error"] = (
            f"counter trading day {gateway.trading_day.isoformat()} differs from the local date "
            f"{local_date.isoformat()}; a filling probe needs a live session "
            "(use --allow-non-trading-day only for interface smoke tests)"
        )
        return detail, 2

    try:
        contract = queries.query_instrument(instrument.symbol)
        depth = queries.query_depth(instrument.symbol)
    except Exception as exc:
        detail["error"] = f"counter queries failed ({type(exc).__name__})"
        return detail, 1
    if not contract or not contract.get("PriceTick"):
        detail["error"] = "the counter reported no price tick for this contract; the probe refuses to guess"
        return detail, 2
    if not depth:
        detail["error"] = "no depth snapshot available; a crossing price cannot be derived"
        return detail, 2
    tick = Decimal(str(contract["PriceTick"]))
    price_tick["value"] = tick
    bid = depth.get("BidPrice1")
    ask = depth.get("AskPrice1")
    if not bid or not ask or Decimal(str(bid)) <= 0 or Decimal(str(ask)) <= 0:
        detail["error"] = "the snapshot has no two-sided quote (bid/ask); a filling probe is not meaningful"
        detail["depth"] = {key: depth.get(key) for key in ("BidPrice1", "BidVolume1", "AskPrice1", "AskVolume1")}
        return detail, 2
    # 穿价再加几跳：实测 openctp TTS 的做市模式要求"高于叫卖价"才立即成交，恰好等于卖一价可能挂住不成交
    # （快照与撮合盘口之间还会前进）。限价单因此仍然有价格上限，不是市价单。
    cross = max(1, int(cross_ticks))
    buy_ticks = int(Decimal(str(ask)) / tick) + cross
    sell_ticks = int(Decimal(str(bid)) / tick) - cross
    if sell_ticks <= 0 or buy_ticks <= 0:
        detail["error"] = "the two-sided quote is below one price tick after the crossing margin"
        return detail, 2
    detail["price_reference"] = {
        "bid": str(bid),
        "ask": str(ask),
        "price_tick": str(tick),
        "cross_ticks": cross,
        "buy_ticks": buy_ticks,
        "sell_ticks": sell_ticks,
        "source": "ReqQryDepthMarketData",
    }
    detail["funds_before"] = _funds(queries)
    if not gateway.mark_reconciled():
        detail["error"] = "the session is not reconciled; the probe refuses to send"
        return detail, 2

    # 0. 先把上一轮可能留下的仓位平掉：探测幂等，且能顺带量出可用的平仓标志
    rows_before, complete_before = _position_rows(queries, instrument)
    detail["position_before"] = {"rows": rows_before, "query_complete": complete_before}
    if _open_position(rows_before) > 0:
        attempts, confirm_before, rows_after, flat = _close_until_flat(
            instrument=instrument,
            quantity=_open_position(rows_before),
            account_id=account_id,
            controller_id=controller_id,
            epoch=epoch,
            gateway=gateway,
            queries=queries,
            sink=sink,
            sell_ticks=sell_ticks,
            wait_s=wait_s,
            order=CANDIDATE_OFFSETS,
            label="flat-residual",
        )
        detail["residual_cleanup"] = {
            "attempts": attempts,
            "position_after": rows_after,
            "flat": flat,
            "confirmed": confirm_before,
        }
        warnings.append("上一轮探测留下了残留仓位，本次已先按候选标志清理")
        record("flatten_residual_position", "flat" if flat else "residual", {"cleanup": detail["residual_cleanup"]})
        if not flat:
            detail["error"] = "the residual position from an earlier probe could not be closed; manual action needed"
            return detail, RESIDUAL_EXIT_CODE

    # 1. 建仓 → 平仓（两种模式：单一循环，或每个候选标志各测一次的矩阵）
    cycle = _matrix_cycle if probe_all_close_flags else _single_cycle
    closed_with, failure = cycle(
        instrument=instrument,
        quantity=int(quantity),
        account_id=account_id,
        controller_id=controller_id,
        epoch=epoch,
        gateway=gateway,
        queries=queries,
        sink=sink,
        wait_s=wait_s,
        buy_ticks=buy_ticks,
        sell_ticks=sell_ticks,
        detail=detail,
        warnings=warnings,
        record=record,
    )
    if failure is not None or closed_with is None:
        return detail, failure if failure is not None else 4

    # 2. 市价单探测（可选）
    if market_order_probe:
        market_intent = OrderIntent(
            client_order_id=f"probe-pos-market-{datetime.now(timezone.utc).strftime('%H%M%S%f')}",
            account_id=account_id,
            strategy_id=controller_id,
            instrument=instrument,
            side=Side.BUY,
            offset=Offset.OPEN,
            quantity=int(quantity),
            order_type=OrderType.MARKET,
            created_at=datetime.now(timezone.utc),
        )
        market_open = _submit(gateway, market_intent, epoch, sink, wait_s=wait_s)
        market_rows, market_complete = _wait_position(queries, instrument, want_flat=False, timeout_s=wait_s)
        market_detail: dict[str, object] = {
            "order": market_open,
            "position_after": {"rows": market_rows, "query_complete": market_complete},
        }
        if market_open.get("result") == "filled":
            close_intent = OrderIntent(
                client_order_id=f"probe-pos-marketclose-{datetime.now(timezone.utc).strftime('%H%M%S%f')}",
                account_id=account_id,
                strategy_id=controller_id,
                instrument=instrument,
                side=Side.SELL,
                offset=closed_with.get("offset_enum") or closed_with["offset"],
                quantity=int(quantity),
                order_type=OrderType.LIMIT,
                created_at=datetime.now(timezone.utc),
                limit_price_ticks=sell_ticks,
            )
            market_close = _submit(gateway, close_intent, epoch, sink, wait_s=wait_s)
            market_detail["close_with_confirmed_flag"] = {"flag": closed_with["flag"], "order": market_close}
            if market_close.get("result") != "filled":
                warnings.append("市价单建的仓没有用已确认的标志平掉；残留检查会给出结论")
        else:
            rejection = market_open.get("counter_rejection") or {}
            warnings.append(
                f"市价单未被接受（{market_open.get('result')}，counter_code={rejection.get('error_code')}）"
            )
        record("market_order_probe", str(market_open.get("result")), market_detail)
        detail["market_order_probe"] = market_detail
    else:
        detail["market_order_probe"] = None

    detail["funds_after"] = _funds(queries)
    detail["funds_delta_note"] = "余额差额含买卖价差与手续费，柜台不在这里分开给出：不当成纯手续费口径"
    residual = _residual(queries, instrument)
    detail["residual_check"] = residual
    if not residual["flat"]:
        detail["error"] = (
            f"a residual position of {residual['open_position_after']} lot(s) remains after the probe; "
            "close it manually and treat this run as failed"
        )
        return detail, RESIDUAL_EXIT_CODE
    market_result = ((detail.get("market_order_probe") or {}).get("order") or {}).get("result")
    detail["result"] = f"position opened and closed with {closed_with['meaning']}（flag={closed_with['flag']}）" + (
        "；市价单亦被柜台接受并成交" if market_result == "filled" else ""
    )
    return detail, 0


def _residual(queries: CtpQueryAdapter, instrument: InstrumentId) -> dict[str, object]:
    """收尾检查：再查一次持仓；返回是否已平掉."""
    rows, complete = _position_rows(queries, instrument)
    open_quantity = _open_position(rows)
    return {
        "rows": rows,
        "query_complete": complete,
        "open_position_after": open_quantity,
        "flat": open_quantity == 0,
    }
