"""[Domain 层] 事件驱动账本、两种盈亏视图、结算版本与资金三字段模型 (S2-03, FR-LED-01~06, A06, A07, A08, A22).

核心规范:
1. 账本由不可变事件推导 (LedgerEntry)：成交费用、盯市平仓、结算、结算修订、外部现金流。
   外部现金流不计入策略收益。
2. 计价基准 (FR-LED-02) 按开仓批次维护：今仓批次基准为成交价，日终结算后基准更新为结算价；
   平仓按所平批次当前基准确认 mtm_close_pnl；日终只对剩余批次计提 (结算价 - 基准)。
3. 两种盈亏视图 (FR-LED-03):
   - mtm_close_pnl (逐日盯市平仓盈亏): 计入结存余额，与结算单对账
   - trade_close_pnl (逐笔交易平仓盈亏): 对冲原始开仓价，只供统计，绝不重复计入余额
   §4.4 手工账: 100 开仓 / 结算 110 (+100) / 次日 108 平仓 (盯市 -20, 逐笔 +80) / 权益净增 80。
4. 平今平昨批次归属：CLOSE_TODAY 只匹配当日开仓批次，CLOSE_YESTERDAY 只匹配上一交易日及更早批次，
   CLOSE 先昨后今；晚到的上一交易日平今成交按其原交易日处理。
5. 结算版本 (FR-CAL-05, A06)：每个 (交易日, 合约, 版本) 只生效一次；重复同版本为空操作；
   更高版本形成差额更正事件，不覆盖旧账；更低版本拒绝；结算价缺失时标记 SETTLEMENT_PENDING，
   不用收盘价替代，也不推进交易日。
6. 精度 (FR-LED-04)：盈亏与费用用 Decimal 全精度计算，只在记账入余额时舍入到最小货币单位。
7. 资金三字段 (FR-LED-05) 与逐单资金预占：
   - broker_available: 动态权益 - 占用保证金 - 冻结保证金 - 冻结费用
   - available_for_new_trades: 按账户浮盈使用规则 (FundsPolicy) 决定是否计入浮盈，浮亏总是扣减
   - margin_coverage_equity: 动态权益，只用于风险度，不与开仓额度相加
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from qh_trader.core.constants import Offset, Side
from qh_trader.core.objects import InstrumentId, Trade
from qh_trader.domain.positions import PositionDetail, PositionManager

D = Decimal


class SettlementPendingError(RuntimeError):
    """结算价尚未就绪 (SETTLEMENT_PENDING)，本次结算未应用任何变更。"""

    def __init__(self, trading_day: date, pending: tuple[InstrumentId, ...]) -> None:
        self.trading_day = trading_day
        self.pending = pending
        super().__init__(f"settlement pending for {trading_day}: {', '.join(str(i) for i in pending)}")


class SettlementVersionError(ValueError):
    """结算版本冲突：低于已生效版本，或同版本价格不一致。"""


class LedgerEntryKind(StrEnum):
    COMMISSION = "COMMISSION"
    MTM_CLOSE = "MTM_CLOSE"
    SETTLEMENT = "SETTLEMENT"
    SETTLEMENT_CORRECTION = "SETTLEMENT_CORRECTION"
    CASH_TRANSFER = "CASH_TRANSFER"


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """不可变账本事件 (FR-LED-01)。amount 已舍入到最小货币单位并已计入 balance."""

    seq: int
    kind: LedgerEntryKind
    trading_day: date
    amount: Decimal
    reference: str
    instrument: InstrumentId | None = None
    version: str | None = None


@dataclass
class PositionLot:
    """开仓持仓批次 (逐笔 FIFO 成本匹配与逐日计价基准)."""

    trade_id: str
    instrument: InstrumentId
    side: Side
    price: Decimal
    quantity: int
    open_date: date
    basis: Decimal
    commission: Decimal = Decimal("0.0")
    last_settled_day: date | None = None


@dataclass
class ClosedTradeRecord:
    """平仓交易记录 (同时保存两种视图，金额为未舍入全精度值)."""

    trade_id: str
    instrument: InstrumentId
    close_side: Side
    offset: Offset
    close_price: Decimal
    quantity: int
    multiplier: Decimal
    close_date: date

    mtm_benchmark_price: Decimal
    mtm_close_pnl: Decimal

    matched_open_price: Decimal
    trade_close_pnl: Decimal

    commission: Decimal = Decimal("0.0")
    matched_lot_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SettlementRecord:
    trading_day: date
    instrument: InstrumentId
    version: str
    settlement_price: Decimal
    net_quantity: int
    pnl: Decimal


class InstrumentLedger:
    """单个合约的明细账本 (双向开仓批次追踪与对冲)."""

    def __init__(self, instrument: InstrumentId, multiplier: Decimal = Decimal("10")) -> None:
        self.instrument = instrument
        self.multiplier = multiplier
        self._long_lots: list[PositionLot] = []
        self._short_lots: list[PositionLot] = []
        # 上一交易日结算价 (无批次的恢复场景兜底基准)
        self.pre_settlement_price: Decimal | None = None
        self.closed_records: list[ClosedTradeRecord] = []

    @property
    def open_long_lots(self) -> tuple[PositionLot, ...]:
        return tuple(self._long_lots)

    @property
    def open_short_lots(self) -> tuple[PositionLot, ...]:
        return tuple(self._short_lots)

    def _lots_for_close(self, close_side: Side) -> list[PositionLot]:
        return self._long_lots if close_side == Side.SELL else self._short_lots

    def add_open_lot(self, trade: Trade, current_trading_day: date | None, commission: Decimal) -> None:
        basis = trade.price
        settled_day: date | None = None
        if current_trading_day is not None and trade.trading_day < current_trading_day:
            # 上一交易日的迟到开仓：已跨过结算，基准应为上一结算价
            if self.pre_settlement_price is None:
                raise SettlementVersionError(
                    f"late open trade {trade.trade_id} for {trade.trading_day} needs a settlement price"
                )
            basis = self.pre_settlement_price
            settled_day = trade.trading_day
        lot = PositionLot(
            trade_id=trade.trade_id,
            instrument=self.instrument,
            side=trade.side,
            price=trade.price,
            quantity=trade.quantity,
            open_date=trade.trading_day,
            basis=basis,
            commission=commission,
            last_settled_day=settled_day,
        )
        (self._long_lots if trade.side == Side.BUY else self._short_lots).append(lot)

    def close_lot(
        self,
        trade: Trade,
        effective_offset: Offset,
        current_trading_day: date | None,
        commission: Decimal = Decimal("0.0"),
    ) -> ClosedTradeRecord:
        """执行平仓核算：按桶归属匹配批次，同时计算 mtm_close_pnl 与 trade_close_pnl."""
        qty = trade.quantity
        mult = self.multiplier
        side_sign = Decimal("1") if trade.side == Side.SELL else Decimal("-1")
        lots = self._lots_for_close(trade.side)
        today = current_trading_day if current_trading_day is not None else trade.trading_day

        def is_today(lot: PositionLot) -> bool:
            return lot.open_date >= today

        if effective_offset == Offset.CLOSE_TODAY:
            candidates = [lot for lot in lots if is_today(lot)]
        elif effective_offset == Offset.CLOSE_YESTERDAY:
            candidates = [lot for lot in lots if not is_today(lot)]
        elif effective_offset == Offset.CLOSE:
            candidates = [lot for lot in lots if not is_today(lot)] + [lot for lot in lots if is_today(lot)]
        else:
            raise ValueError(f"not a close offset: {effective_offset}")

        available = sum(lot.quantity for lot in candidates)
        if available < qty:
            raise ValueError(
                f"cannot match {qty} lots for {effective_offset} on {self.instrument}: matched lots hold {available}"
            )

        remaining = qty
        mtm_pnl = Decimal("0")
        trade_pnl = Decimal("0")
        matched_open_cost = Decimal("0")
        matched_basis_cost = Decimal("0")
        matched_ids: list[str] = []

        for lot in candidates:
            if remaining == 0:
                break
            take = min(remaining, lot.quantity)
            mtm_pnl += (trade.price - lot.basis) * D(take) * mult * side_sign
            trade_pnl += (trade.price - lot.price) * D(take) * mult * side_sign
            matched_open_cost += lot.price * D(take)
            matched_basis_cost += lot.basis * D(take)
            matched_ids.append(lot.trade_id)
            remaining -= take
            lot.quantity -= take
            if lot.quantity == 0:
                lots.remove(lot)

        record = ClosedTradeRecord(
            trade_id=trade.trade_id,
            instrument=self.instrument,
            close_side=trade.side,
            offset=trade.offset,
            close_price=trade.price,
            quantity=qty,
            multiplier=mult,
            close_date=trade.trading_day,
            mtm_benchmark_price=matched_basis_cost / D(qty),
            mtm_close_pnl=mtm_pnl,
            matched_open_price=matched_open_cost / D(qty),
            trade_close_pnl=trade_pnl,
            commission=commission,
            matched_lot_ids=tuple(matched_ids),
        )
        self.closed_records.append(record)
        return record

    def settle(self, trading_day: date, settlement_price: Decimal) -> tuple[Decimal, int]:
        """对剩余批次计提 (结算价 - 基准) 并把基准更新为结算价。返回 (盈亏, 净持仓手数)."""
        mult = self.multiplier
        pnl = Decimal("0")
        net = 0
        for lot in self._long_lots:
            pnl += (settlement_price - lot.basis) * D(lot.quantity) * mult
            lot.basis = settlement_price
            lot.last_settled_day = trading_day
            net += lot.quantity
        for lot in self._short_lots:
            pnl += (lot.basis - settlement_price) * D(lot.quantity) * mult
            lot.basis = settlement_price
            lot.last_settled_day = trading_day
            net -= lot.quantity
        self.pre_settlement_price = settlement_price
        return pnl, net

    def rebase_settled_lots(self, trading_day: date, old_price: Decimal, new_price: Decimal) -> None:
        """结算修订：把在该交易日结算过且基准仍为旧结算价的批次改为新结算价."""
        for lot in self._long_lots + self._short_lots:
            if lot.last_settled_day == trading_day and lot.basis == old_price:
                lot.basis = new_price
        if self.pre_settlement_price == old_price:
            self.pre_settlement_price = new_price

    def calculate_unrealized_pnl(
        self,
        current_price: Decimal,
        pos_long: PositionDetail,
        pos_short: PositionDetail,
    ) -> Decimal:
        """盘中持仓浮动盈亏：每个批次按当前计价基准 (今仓开仓价 / 昨仓上一结算价)."""
        mult = self.multiplier
        pnl = Decimal("0")
        lots_long = sum(lot.quantity for lot in self._long_lots)
        lots_short = sum(lot.quantity for lot in self._short_lots)
        for lot in self._long_lots:
            pnl += (current_price - lot.basis) * D(lot.quantity) * mult
        for lot in self._short_lots:
            pnl += (lot.basis - current_price) * D(lot.quantity) * mult
        # 无批次的恢复兜底：持仓有量但批次缺失时按上一结算价估值
        ref = self.pre_settlement_price if self.pre_settlement_price is not None else current_price
        extra_long = pos_long.total_position - lots_long
        extra_short = pos_short.total_position - lots_short
        if extra_long > 0:
            pnl += (current_price - ref) * D(extra_long) * mult
        if extra_short > 0:
            pnl += (ref - current_price) * D(extra_short) * mult
        return pnl


@dataclass(frozen=True, slots=True)
class FundsPolicy:
    """账户资金使用规则版本 (FR-LED-05 待确认项 11 的显式配置)."""

    version: str = "conservative-default"
    floating_profit_usable: bool = False


@dataclass
class FundsReservation:
    """逐单资金预占 (FR-ORD-03: 同一事务内记录订单意图、冻结持仓、预占保证金与费用)."""

    client_order_id: str
    margin: Decimal
    fee: Decimal


@dataclass
class AccountFundsState:
    """账户资金三字段与状态快照 (FR-LED-05)."""

    balance: Decimal
    total_equity: Decimal
    margin_used: Decimal
    frozen_margin: Decimal
    frozen_fee: Decimal
    unrealized_pnl: Decimal
    realized_mtm_pnl: Decimal
    realized_trade_pnl: Decimal
    total_commission: Decimal

    broker_available: Decimal
    available_for_new_trades: Decimal
    margin_coverage_equity: Decimal
    risk_ratio: Decimal
    funds_policy_version: str = "conservative-default"


class AccountLedger:
    """账户级事件账本聚合根."""

    def __init__(
        self,
        account_id: str,
        initial_capital: Decimal = Decimal("1000000.00"),
        currency_unit: Decimal = Decimal("0.01"),
        rounding: str = ROUND_HALF_UP,
        funds_policy: FundsPolicy | None = None,
        trading_day: date | None = None,
    ) -> None:
        if currency_unit <= 0:
            raise ValueError("currency_unit must be positive")
        self.account_id = account_id
        self.initial_capital = initial_capital
        self.currency_unit = currency_unit
        self.rounding = rounding
        self.funds_policy = funds_policy or FundsPolicy()
        self.current_trading_day: date | None = trading_day

        self.balance: Decimal = initial_capital
        self.total_commission: Decimal = Decimal("0")
        self.realized_mtm_pnl: Decimal = Decimal("0")
        self.realized_trade_pnl: Decimal = Decimal("0")
        self.external_cash_flow: Decimal = Decimal("0")

        self.position_manager: PositionManager = PositionManager(account_id, trading_day=trading_day)
        self._instrument_ledgers: dict[InstrumentId, InstrumentLedger] = {}
        self._funds_reservations: dict[str, FundsReservation] = {}
        self.entries: list[LedgerEntry] = []
        # (trading_day, instrument) -> 已生效结算记录列表 (按版本先后)
        self._settlements: dict[tuple[date, InstrumentId], list[SettlementRecord]] = {}
        self.settlement_pending: dict[date, tuple[InstrumentId, ...]] = {}

    # ------------------------------------------------------------------ 基础
    def round_money(self, amount: Decimal) -> Decimal:
        return (amount / self.currency_unit).quantize(Decimal(1), rounding=self.rounding) * self.currency_unit

    def _post(
        self,
        kind: LedgerEntryKind,
        amount: Decimal,
        reference: str,
        trading_day: date | None,
        instrument: InstrumentId | None = None,
        version: str | None = None,
    ) -> LedgerEntry:
        rounded = self.round_money(amount)
        entry = LedgerEntry(
            seq=len(self.entries) + 1,
            kind=kind,
            trading_day=trading_day or self.current_trading_day or date.min,
            amount=rounded,
            reference=reference,
            instrument=instrument,
            version=version,
        )
        self.entries.append(entry)
        self.balance += rounded
        return entry

    @property
    def frozen_margin(self) -> Decimal:
        return sum((r.margin for r in self._funds_reservations.values()), Decimal("0"))

    @property
    def frozen_fee(self) -> Decimal:
        return sum((r.fee for r in self._funds_reservations.values()), Decimal("0"))

    @property
    def strategy_pnl(self) -> Decimal:
        """剔除外部现金流后的账本盈亏 (FR-LED-01)."""
        return self.balance - self.initial_capital - self.external_cash_flow

    def get_instrument_ledger(
        self,
        instrument: InstrumentId,
        multiplier: Decimal = Decimal("10"),
    ) -> InstrumentLedger:
        if instrument not in self._instrument_ledgers:
            self._instrument_ledgers[instrument] = InstrumentLedger(instrument, multiplier)
        return self._instrument_ledgers[instrument]

    def set_pre_settlement_price(self, instrument: InstrumentId, price: Decimal) -> None:
        self.get_instrument_ledger(instrument).pre_settlement_price = price

    def settlements(self, trading_day: date, instrument: InstrumentId) -> tuple[SettlementRecord, ...]:
        return tuple(self._settlements.get((trading_day, instrument), ()))

    # ------------------------------------------------------------------ 外部现金流
    def transfer_cash(self, amount: Decimal, reference: str, trading_day: date | None = None) -> LedgerEntry:
        """入金 (正) / 出金 (负)，不计入策略收益."""
        entry = self._post(LedgerEntryKind.CASH_TRANSFER, amount, reference, trading_day)
        self.external_cash_flow += entry.amount
        return entry

    # ------------------------------------------------------------------ 资金预占
    def reserve_funds(self, client_order_id: str, margin: Decimal, fee: Decimal) -> FundsReservation:
        if client_order_id in self._funds_reservations:
            raise ValueError(f"duplicate funds reservation for order: {client_order_id}")
        if margin < 0 or fee < 0:
            raise ValueError("reserved margin and fee cannot be negative")
        res = FundsReservation(client_order_id=client_order_id, margin=margin, fee=fee)
        self._funds_reservations[client_order_id] = res
        return res

    def release_funds(self, client_order_id: str, fraction_remaining: Decimal = Decimal("0")) -> None:
        """释放预占；fraction_remaining 为仍需保留的比例 (未入账成交部分)."""
        res = self._funds_reservations.get(client_order_id)
        if res is None:
            return
        if fraction_remaining <= 0:
            self._funds_reservations.pop(client_order_id, None)
            return
        res.margin *= fraction_remaining
        res.fee *= fraction_remaining

    def get_funds_reservation(self, client_order_id: str) -> FundsReservation | None:
        return self._funds_reservations.get(client_order_id)

    # ------------------------------------------------------------------ 成交
    def on_trade(
        self,
        trade: Trade,
        commission: Decimal = Decimal("0"),
        multiplier: Decimal = Decimal("10"),
        client_order_id: str | None = None,
    ) -> ClosedTradeRecord | None:
        """处理去重后的真实成交入账 (更新持仓、资金与两种盈亏视图)."""
        inst = trade.instrument
        ledger = self.get_instrument_ledger(inst, multiplier)
        if self.current_trading_day is None:
            self.current_trading_day = trade.trading_day
            self.position_manager.current_trading_day = trade.trading_day

        if commission != 0:
            self.total_commission += commission
            self._post(
                LedgerEntryKind.COMMISSION,
                -commission,
                f"trade:{trade.trade_id}",
                trade.trading_day,
                instrument=inst,
            )

        record: ClosedTradeRecord | None = None
        if trade.offset == Offset.OPEN:
            self.position_manager.apply_trade(trade, client_order_id=client_order_id)
            ledger.add_open_lot(trade, self.current_trading_day, commission)
        else:
            effective = self.position_manager.effective_offset(trade)
            self.position_manager.apply_trade(trade, client_order_id=client_order_id)
            record = ledger.close_lot(trade, effective, self.current_trading_day, commission)
            self.realized_mtm_pnl += record.mtm_close_pnl
            self._post(
                LedgerEntryKind.MTM_CLOSE,
                record.mtm_close_pnl,
                f"trade:{trade.trade_id}",
                trade.trading_day,
                instrument=inst,
            )
            # 逐笔平仓盈亏只供统计，绝不进入 balance
            self.realized_trade_pnl += record.trade_close_pnl

        if client_order_id is not None:
            res = self._funds_reservations.get(client_order_id)
            pos_res = self.position_manager.get_reservation(client_order_id)
            if res is not None:
                if pos_res is None:
                    self._funds_reservations.pop(client_order_id, None)
                else:
                    self.release_funds(client_order_id, D(pos_res.remaining_qty) / D(pos_res.quantity))
        return record

    # ------------------------------------------------------------------ 结算
    def _instruments_with_positions(self) -> list[InstrumentId]:
        result: list[InstrumentId] = []
        for inst, ledger in self._instrument_ledgers.items():
            pos_l, pos_s = self.position_manager.get_both_positions(inst)
            if pos_l.total_position > 0 or pos_s.total_position > 0 or ledger.open_long_lots or ledger.open_short_lots:
                result.append(inst)
        return result

    def settle_day(
        self,
        settlement_prices: dict[InstrumentId, Decimal],
        new_trading_day: date,
        trading_day: date | None = None,
        version: str = "v1",
    ) -> Decimal:
        """日终结算 (FR-CAL-05/06, A06)。

        对所有剩余持仓按官方结算价计提到余额，更新次日计价基准，然后一次性推进交易日。
        同一 (交易日, 版本) 重复调用为空操作；缺少任何持仓合约的结算价时抛出
        SettlementPendingError 且不做任何变更。返回本次结转的总结算盈亏 (已舍入)。
        """
        settle_day = trading_day if trading_day is not None else self.current_trading_day
        if settle_day is None:
            raise ValueError("settle_day needs a trading_day when the ledger has no current trading day")
        if new_trading_day <= settle_day:
            raise ValueError(f"new trading day {new_trading_day} must be after settled day {settle_day}")

        held = self._instruments_with_positions()
        already = [inst for inst in held if any(r.version == version for r in self.settlements(settle_day, inst))]
        if already and len(already) == len(held):
            # 重复的日终任务：不重复结算、不重复转换持仓
            self.position_manager.advance_trading_day(new_trading_day)
            self.current_trading_day = new_trading_day
            return Decimal("0")

        pending = tuple(inst for inst in held if inst not in settlement_prices and inst not in already)
        if pending:
            self.settlement_pending[settle_day] = pending
            raise SettlementPendingError(settle_day, pending)
        self.settlement_pending.pop(settle_day, None)

        total = Decimal("0")
        for inst in held:
            if inst in already:
                continue
            existing = self.settlements(settle_day, inst)
            if existing:
                raise SettlementVersionError(
                    f"{inst} on {settle_day} already settled with version {existing[-1].version}; "
                    "use revise_settlement for corrections"
                )
            ledger = self._instrument_ledgers[inst]
            pnl, net = ledger.settle(settle_day, settlement_prices[inst])
            entry = self._post(
                LedgerEntryKind.SETTLEMENT,
                pnl,
                f"settlement:{settle_day}:{inst}:{version}",
                settle_day,
                instrument=inst,
                version=version,
            )
            self._settlements.setdefault((settle_day, inst), []).append(
                SettlementRecord(settle_day, inst, version, settlement_prices[inst], net, entry.amount)
            )
            total += entry.amount

        self.realized_mtm_pnl = Decimal("0")
        self.position_manager.advance_trading_day(new_trading_day)
        self.current_trading_day = new_trading_day
        return total

    def revise_settlement(
        self,
        trading_day: date,
        instrument: InstrumentId,
        settlement_price: Decimal,
        version: str,
    ) -> LedgerEntry | None:
        """结算修订：以带版本的差额更正事件处理，不覆盖旧账、不重复计入 (FR-CAL-05, A06).

        返回更正事件；同版本同价重复到达返回 None。
        """
        history = self._settlements.get((trading_day, instrument))
        if not history:
            raise SettlementVersionError(f"no settlement for {instrument} on {trading_day} to revise")
        latest = history[-1]
        if version == latest.version:
            if settlement_price != latest.settlement_price:
                raise SettlementVersionError(
                    f"settlement version {version} for {instrument} on {trading_day} "
                    "already applied with a different price"
                )
            return None
        if version < latest.version:
            raise SettlementVersionError(f"settlement version {version} is older than applied version {latest.version}")
        ledger = self._instrument_ledgers[instrument]
        delta = (settlement_price - latest.settlement_price) * D(latest.net_quantity) * ledger.multiplier
        ledger.rebase_settled_lots(trading_day, latest.settlement_price, settlement_price)
        entry = self._post(
            LedgerEntryKind.SETTLEMENT_CORRECTION,
            delta,
            f"settlement-correction:{trading_day}:{instrument}:{latest.version}->{version}",
            trading_day,
            instrument=instrument,
            version=version,
        )
        history.append(
            SettlementRecord(trading_day, instrument, version, settlement_price, latest.net_quantity, entry.amount)
        )
        return entry

    # ------------------------------------------------------------------ 资金
    def get_funds_state(
        self,
        current_prices: dict[InstrumentId, Decimal] | None = None,
        margin_rates: dict[InstrumentId, Decimal] | None = None,
    ) -> AccountFundsState:
        """获取当前账户资金与三字段状态快照."""
        current_prices = current_prices or {}
        margin_rates = margin_rates or {}

        unrealized = Decimal("0")
        total_margin = Decimal("0")
        for inst, ledger in self._instrument_ledgers.items():
            pos_l, pos_s = self.position_manager.get_both_positions(inst)
            price = current_prices.get(inst)
            if price is None:
                price = ledger.pre_settlement_price if ledger.pre_settlement_price is not None else Decimal("0")
            unrealized += ledger.calculate_unrealized_pnl(price, pos_l, pos_s)
            rate = margin_rates.get(inst, Decimal("0.10"))
            mult = ledger.multiplier
            total_margin += price * D(pos_l.total_position + pos_s.total_position) * mult * rate

        total_equity = self.balance + unrealized
        frozen_margin = self.frozen_margin
        frozen_fee = self.frozen_fee

        broker_avail = total_equity - total_margin - frozen_margin - frozen_fee
        if self.funds_policy.floating_profit_usable:
            effective_equity = total_equity
        else:
            effective_equity = min(self.balance, total_equity)
        avail_new_trades = max(Decimal("0"), effective_equity - total_margin - frozen_margin - frozen_fee)
        risk_ratio = (total_margin / total_equity) if total_equity > 0 else Decimal("1.0")

        return AccountFundsState(
            balance=self.balance,
            total_equity=total_equity,
            margin_used=total_margin,
            frozen_margin=frozen_margin,
            frozen_fee=frozen_fee,
            unrealized_pnl=unrealized,
            realized_mtm_pnl=self.realized_mtm_pnl,
            realized_trade_pnl=self.realized_trade_pnl,
            total_commission=self.total_commission,
            broker_available=broker_avail,
            available_for_new_trades=avail_new_trades,
            margin_coverage_equity=total_equity,
            risk_ratio=risk_ratio,
            funds_policy_version=self.funds_policy.version,
        )
