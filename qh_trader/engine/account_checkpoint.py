"""[Engine 层] 账户内核检查点：S2 领域聚合的导出、恢复与已结算历史退役 (S5-04, R11, FR-LED-01, FR-REC-03).

实盘账户事实只增不减时，暂存与发布的成本随运行时长增长 (06 R11)。日终结算完成后，实盘账户模型用本模块把
内核状态写成检查点，替代已结算的事实前缀：

- 检查点只含 Core 值对象与基本类型，Journal 编解码器可原样持久化；
- 检查点覆盖领域对象的全部内部状态，因此直接读写私有字段；字段完整性由逐字段比对测试守护，领域对象新增
  字段而检查点未覆盖时测试失败。墙钟审计时刻 (``Order.updated_at``、``SendAttempt.attempted_at``、
  ``UnlinkedTrade.received_at``、``ExternalOrderRecord.first_seen_at``) 不进入检查点：重放时它们本来就取
  当前时间，恢复时同样取当前时间；
- 退役只删除已结算的历史：保留 ``keep_from`` (最近已结算交易日) 及之后的账本条目、平仓记录、结算记录与
  风控计数；终态且已全部入账、无预占、无对账标记的委托在上一检查点时就已满足条件的，才在本检查点退役
  (即成为终态后的第二个日终边界)，退役的本地单号由调用方保留以拒绝复用。
"""

# ruff: noqa: SLF001

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import date, datetime, timezone
from typing import Any

from qh_trader.core.objects import InstrumentId
from qh_trader.domain.ledger import (
    AccountLedger,
    ClosedTradeRecord,
    FundsPolicy,
    FundsReservation,
    InstrumentLedger,
    LedgerEntry,
    LedgerEntryKind,
    PositionLot,
    SettlementRecord,
)
from qh_trader.domain.orders import (
    ExternalOrderRecord,
    Order,
    OrderManager,
    SendAttempt,
    TradeDeduplicator,
    UnlinkedTrade,
    _format_trade_key,
)
from qh_trader.domain.positions import PositionDetail, PositionManager, PositionReservation
from qh_trader.domain.risk import RiskEvent, RiskManager, RiskState

Data = Mapping[str, Any]


class CheckpointFormatError(ValueError):
    """检查点内容与当前内核构造参数或格式不一致；不能用它恢复账户."""


# ---------------------------------------------------------------------- 账本
def dump_ledger(ledger: AccountLedger) -> dict[str, Any]:
    return {
        "initial_capital": ledger.initial_capital,
        "currency_unit": ledger.currency_unit,
        "rounding": ledger.rounding,
        "funds_policy": {
            "version": ledger.funds_policy.version,
            "floating_profit_usable": ledger.funds_policy.floating_profit_usable,
        },
        "current_trading_day": ledger.current_trading_day,
        "balance": ledger.balance,
        "total_commission": ledger.total_commission,
        "realized_mtm_pnl": ledger.realized_mtm_pnl,
        "realized_trade_pnl": ledger.realized_trade_pnl,
        "external_cash_flow": ledger.external_cash_flow,
        "entry_seq": ledger.entry_seq,
        "entries": tuple(
            {
                "seq": entry.seq,
                "kind": entry.kind.value,
                "trading_day": entry.trading_day,
                "amount": entry.amount,
                "reference": entry.reference,
                "instrument": entry.instrument,
                "version": entry.version,
            }
            for entry in ledger.entries
        ),
        "instruments": tuple(_dump_instrument_ledger(item) for item in ledger._instrument_ledgers.values()),
        "funds_reservations": tuple(
            {"client_order_id": item.client_order_id, "margin": item.margin, "fee": item.fee}
            for item in ledger._funds_reservations.values()
        ),
        "settlements": tuple(
            {
                "trading_day": record.trading_day,
                "instrument": record.instrument,
                "version": record.version,
                "settlement_price": record.settlement_price,
                "net_quantity": record.net_quantity,
                "pnl": record.pnl,
            }
            for records in ledger._settlements.values()
            for record in records
        ),
        "settlement_pending": tuple(
            {"trading_day": day, "instruments": tuple(instruments)}
            for day, instruments in ledger.settlement_pending.items()
        ),
    }


def _dump_lot(lot: PositionLot) -> dict[str, Any]:
    return {
        "trade_id": lot.trade_id,
        "side": lot.side,
        "price": lot.price,
        "quantity": lot.quantity,
        "open_date": lot.open_date,
        "basis": lot.basis,
        "commission": lot.commission,
        "last_settled_day": lot.last_settled_day,
    }


def _dump_instrument_ledger(ledger: InstrumentLedger) -> dict[str, Any]:
    return {
        "instrument": ledger.instrument,
        "multiplier": ledger.multiplier,
        "pre_settlement_price": ledger.pre_settlement_price,
        "long_lots": tuple(_dump_lot(lot) for lot in ledger._long_lots),
        "short_lots": tuple(_dump_lot(lot) for lot in ledger._short_lots),
        "closed_records": tuple(
            {
                "trade_id": record.trade_id,
                "close_side": record.close_side,
                "offset": record.offset,
                "close_price": record.close_price,
                "quantity": record.quantity,
                "multiplier": record.multiplier,
                "close_date": record.close_date,
                "mtm_benchmark_price": record.mtm_benchmark_price,
                "mtm_close_pnl": record.mtm_close_pnl,
                "matched_open_price": record.matched_open_price,
                "trade_close_pnl": record.trade_close_pnl,
                "commission": record.commission,
                "matched_lot_ids": tuple(record.matched_lot_ids),
            }
            for record in ledger.closed_records
        ),
    }


def load_ledger(data: Data, ledger: AccountLedger) -> None:
    """把检查点写入按同一开立参数新建的账本；构造参数不一致即失败，不以检查点悄悄覆盖代码口径."""
    policy = FundsPolicy(data["funds_policy"]["version"], data["funds_policy"]["floating_profit_usable"])
    constructed = (ledger.initial_capital, ledger.currency_unit, ledger.rounding, ledger.funds_policy)
    if constructed != (data["initial_capital"], data["currency_unit"], data["rounding"], policy):
        raise CheckpointFormatError("checkpoint ledger parameters differ from the kernel construction")
    ledger.current_trading_day = data["current_trading_day"]
    ledger.balance = data["balance"]
    ledger.total_commission = data["total_commission"]
    ledger.realized_mtm_pnl = data["realized_mtm_pnl"]
    ledger.realized_trade_pnl = data["realized_trade_pnl"]
    ledger.external_cash_flow = data["external_cash_flow"]
    ledger.entry_seq = data["entry_seq"]
    ledger.entries = [
        LedgerEntry(
            seq=item["seq"],
            kind=LedgerEntryKind(item["kind"]),
            trading_day=item["trading_day"],
            amount=item["amount"],
            reference=item["reference"],
            instrument=item["instrument"],
            version=item["version"],
        )
        for item in data["entries"]
    ]
    ledger._instrument_ledgers = {}
    for item in data["instruments"]:
        instrument_ledger = InstrumentLedger(item["instrument"], item["multiplier"])
        instrument_ledger.pre_settlement_price = item["pre_settlement_price"]
        instrument_ledger._long_lots = [_load_lot(lot, item["instrument"]) for lot in item["long_lots"]]
        instrument_ledger._short_lots = [_load_lot(lot, item["instrument"]) for lot in item["short_lots"]]
        instrument_ledger.closed_records = [
            ClosedTradeRecord(instrument=item["instrument"], **dict(record)) for record in item["closed_records"]
        ]
        ledger._instrument_ledgers[item["instrument"]] = instrument_ledger
    ledger._funds_reservations = {
        item["client_order_id"]: FundsReservation(item["client_order_id"], item["margin"], item["fee"])
        for item in data["funds_reservations"]
    }
    ledger._settlements = {}
    for item in data["settlements"]:
        ledger._settlements.setdefault((item["trading_day"], item["instrument"]), []).append(SettlementRecord(**item))
    ledger.settlement_pending = {item["trading_day"]: tuple(item["instruments"]) for item in data["settlement_pending"]}


def _load_lot(data: Data, instrument: InstrumentId) -> PositionLot:
    return PositionLot(instrument=instrument, **dict(data))


# ---------------------------------------------------------------------- 持仓
def dump_positions(positions: PositionManager) -> dict[str, Any]:
    return {
        "current_trading_day": positions.current_trading_day,
        "positions": tuple(
            {
                "instrument": detail.instrument,
                "side": detail.side,
                "hedge_flag": detail.hedge_flag,
                "pos_yd": detail.pos_yd,
                "pos_td": detail.pos_td,
                "frozen_yd": detail.frozen_yd,
                "frozen_td": detail.frozen_td,
                "open_cost": detail.open_cost,
                "position_cost": detail.position_cost,
            }
            for detail in positions._positions.values()
        ),
        "reservations": tuple(
            {
                "client_order_id": item.client_order_id,
                "instrument": item.instrument,
                "side": item.side,
                "offset": item.offset,
                "quantity": item.quantity,
                "target_pos_side": item.target_pos_side,
                "frozen_yd": item.frozen_yd,
                "frozen_td": item.frozen_td,
                "accounted_fill_qty": item.accounted_fill_qty,
                "trading_day": item.trading_day,
            }
            for item in positions._reservations.values()
        ),
    }


def load_positions(data: Data, positions: PositionManager) -> None:
    positions.current_trading_day = data["current_trading_day"]
    positions._positions = {}
    for item in data["positions"]:
        detail = PositionDetail(**dict(item))
        positions._positions[(detail.instrument, detail.side)] = detail
    positions._reservations = {
        item["client_order_id"]: PositionReservation(**dict(item)) for item in data["reservations"]
    }
    positions.verify_frozen_invariant()


# ---------------------------------------------------------------------- 委托
def dump_orders(orders: OrderManager) -> dict[str, Any]:
    return {
        "orders": tuple(_dump_order(order) for order in orders._orders_by_client_id.values()),
        "exchange_index": tuple(
            {"exchange": exchange, "exchange_order_id": exchange_order_id, "client_order_id": client_order_id}
            for (exchange, exchange_order_id), client_order_id in orders._client_id_by_exchange_order.items()
        ),
        "session_index": tuple(
            {"front_id": front_id, "session_id": session_id, "order_ref": order_ref, "client_order_id": client_order_id}
            for (front_id, session_id, order_ref), client_order_id in orders._client_id_by_session.items()
        ),
        "trade_keys": tuple(orders.deduplicator.snapshot()),
        "unlinked": tuple(
            {"trade": item.trade, "resolved": item.resolved, "linked_client_order_id": item.linked_client_order_id}
            for item in orders.unlinked_trades
        ),
        "external": tuple(
            {"identity": record.identity, "updates": tuple(record.updates)}
            for record in orders.external_orders.values()
        ),
    }


def _dump_order(order: Order) -> dict[str, Any]:
    return {
        "intent": order.intent,
        "status": order.status,
        "send_state": order.send_state,
        "send_evidence": order.send_evidence,
        "send_attempts": tuple(
            {"state": attempt.state, "evidence": attempt.evidence, "local_code": attempt.local_code}
            for attempt in order.send_attempts
        ),
        "identity": order.identity,
        "command_epoch": order.command_epoch,
        "cum_filled_qty": order.cum_filled_qty,
        "accounted_filled_qty": order.accounted_filled_qty,
        "cancel_pending": order.cancel_pending,
        "cancel_reject_reason": order.cancel_reject_reason,
        "reconciliation_required": order.reconciliation_required,
        "reconciliation_reason": order.reconciliation_reason,
        "child_order_ids": tuple(order.child_order_ids),
        "trades": tuple(order.trades),
        "created_at": order.created_at,
    }


def load_orders(data: Data, orders: OrderManager) -> None:
    orders._orders_by_client_id = {}
    for item in data["orders"]:
        values = dict(item)
        values["send_attempts"] = [
            SendAttempt(datetime.now(timezone.utc), attempt["state"], attempt["evidence"], attempt["local_code"])
            for attempt in values["send_attempts"]
        ]
        values["child_order_ids"] = list(values["child_order_ids"])
        values["trades"] = list(values["trades"])
        order = Order(**values)
        orders._orders_by_client_id[order.client_order_id] = order
    orders._client_id_by_exchange_order = {
        (item["exchange"], item["exchange_order_id"]): item["client_order_id"] for item in data["exchange_index"]
    }
    orders._client_id_by_session = {
        (item["front_id"], item["session_id"], item["order_ref"]): item["client_order_id"]
        for item in data["session_index"]
    }
    orders.deduplicator = TradeDeduplicator(data["trade_keys"])
    orders.unlinked_trades = [
        UnlinkedTrade(
            trade=item["trade"], resolved=item["resolved"], linked_client_order_id=item["linked_client_order_id"]
        )
        for item in data["unlinked"]
    ]
    orders.external_orders = {}
    for item in data["external"]:
        record = ExternalOrderRecord(identity=item["identity"], updates=list(item["updates"]))
        orders.external_orders[orders._external_key(record.identity)] = record


# ---------------------------------------------------------------------- 风控
def dump_risk(risk: RiskManager) -> dict[str, Any]:
    return {
        "risk_state": risk.risk_state.value,
        "risk_events": tuple(
            {
                "at": event.at,
                "from_state": event.from_state.value,
                "to_state": event.to_state.value,
                "reason": event.reason,
            }
            for event in risk.risk_events
        ),
        "trading_day": risk.trading_day,
        "open_cooldown_until": risk.open_cooldown_until,
        "flatten_requested": risk.flatten_requested,
        "flatten_reason": risk.flatten_reason,
        "counters": risk.snapshot_counters(),
    }


def load_risk(data: Data, risk: RiskManager) -> None:
    risk.risk_state = RiskState(data["risk_state"])
    risk.risk_events = [
        RiskEvent(item["at"], RiskState(item["from_state"]), RiskState(item["to_state"]), item["reason"])
        for item in data["risk_events"]
    ]
    risk.open_cooldown_until = data["open_cooldown_until"]
    risk.flatten_requested = data["flatten_requested"]
    risk.flatten_reason = data["flatten_reason"]
    risk.restore_counters(_plain(data["counters"]))
    risk.trading_day = data["trading_day"]


def _plain(value: Any) -> Any:
    """``restore_counters`` 接受普通 dict / list；检查点读回的是冻结映射与元组."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


# ---------------------------------------------------------------------- 退役
def retirable_orders(orders: OrderManager, positions: PositionManager, ledger: AccountLedger) -> frozenset[str]:
    """终态、柜台成交已全部入账、无撤单在途、无对账标记、无持仓与资金预占的委托."""
    return frozenset(
        order.client_order_id
        for order in orders._orders_by_client_id.values()
        if order.is_terminal
        and order.unaccounted_fill_qty == 0
        and not order.cancel_pending
        and not order.reconciliation_required
        and positions.get_reservation(order.client_order_id) is None
        and ledger.get_funds_reservation(order.client_order_id) is None
    )


def retire_history(
    ledger: AccountLedger,
    orders: OrderManager,
    positions: PositionManager,
    risk: RiskManager,
    *,
    keep_from: date,
    candidates: Collection[str],
) -> tuple[frozenset[str], frozenset[str]]:
    """原地删除 ``keep_from`` 之前的已结算历史，并退役上一检查点就已可退役的委托.

    返回 (本次退役的本地单号, 下一检查点的退役候选)。
    """
    ledger.entries = [entry for entry in ledger.entries if entry.trading_day >= keep_from]
    for instrument_ledger in ledger._instrument_ledgers.values():
        instrument_ledger.closed_records = [
            record for record in instrument_ledger.closed_records if record.close_date >= keep_from
        ]
    ledger._settlements = {key: records for key, records in ledger._settlements.items() if key[0] >= keep_from}
    counters = risk.snapshot_counters()
    for name in ("open_lots", "cancels"):
        counters[name] = [row for row in counters[name] if date.fromisoformat(row["trading_day"]) >= keep_from]
    risk.restore_counters(counters)

    eligible = retirable_orders(orders, positions, ledger)
    retired = frozenset(candidate for candidate in candidates if candidate in eligible)
    removed_keys: set[str] = set()
    for client_order_id in retired:
        order = orders._orders_by_client_id.pop(client_order_id)
        removed_keys.update(_format_trade_key(trade) for trade in order.trades)
    orders._client_id_by_exchange_order = {
        key: value for key, value in orders._client_id_by_exchange_order.items() if value not in retired
    }
    orders._client_id_by_session = {
        key: value for key, value in orders._client_id_by_session.items() if value not in retired
    }
    orders.unlinked_trades = [
        item for item in orders.unlinked_trades if not (item.resolved and item.linked_client_order_id in retired)
    ]
    if removed_keys:
        orders.deduplicator = TradeDeduplicator(
            [key for key in orders.deduplicator.snapshot() if key not in removed_keys]
        )
    return retired, eligible - retired
