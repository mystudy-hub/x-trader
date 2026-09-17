"""[Domain 层] 事件驱动账本、两种盈亏视图与资金三字段模型 (S2-03, FR-LED-01~06, A06, A07, A08, A22).

核心规范:
1. 一套账本两种盈亏视图:
   - mtm_close_pnl (逐日盯市平仓盈亏): 今仓按开仓价、昨仓按上一结算价，计入实际结存余额与结算单对账
   - trade_close_pnl (逐笔交易平仓盈亏): 对冲原始开仓价，用于胜率与策略归因，绝不重复计入账本余额
2. 跨日结算 (settle_day):
   - 剩余持仓按官方结算价结转浮动盈亏至结存余额，并更新次日计价基准
   - 严格对齐规划 §4.4 手工账 (+100 结算入账 / -20 盯市平仓 / +80 逐笔平仓 / 权益合计增加 80)
3. 资金三字段 (FR-LED-05):
   - broker_available (柜台可用): 动态权益 - 保证金 - 委托预占
   - available_for_new_trades (开仓额度): 研究模式保守口径 (浮盈不增开仓额度，浮亏扣减)
   - margin_coverage_equity (保证金覆盖权益): 动态权益与风险度计算
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from qh_trader.core.constants import Offset, PositionSide, Side
from qh_trader.core.objects import InstrumentId, Trade
from qh_trader.domain.positions import PositionDetail, PositionManager

D = Decimal


@dataclass(frozen=True, slots=True)
class PositionLot:
    """开仓持仓批次 (用于逐笔 FIFO 成本匹配)."""

    trade_id: str
    instrument: InstrumentId
    side: Side
    price: Decimal
    quantity: int
    open_date: date
    commission: Decimal = Decimal("0.0")


@dataclass
class ClosedTradeRecord:
    """平仓交易记录 (同时保存两种视图)."""

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


class InstrumentLedger:
    """单个合约的明细账本 (双向开仓批次追踪与对冲)."""

    def __init__(self, instrument: InstrumentId, multiplier: Decimal = Decimal("10")) -> None:
        self.instrument = instrument
        self.multiplier = multiplier

        # 批次队列用于逐笔 FIFO 盈亏匹配 (多头开仓批次 / 空头开仓批次)
        self._long_lots: deque[PositionLot] = deque()
        self._short_lots: deque[PositionLot] = deque()

        # 上一交易日结算价 (用于昨仓逐日盯市计算)
        self.pre_settlement_price: Decimal | None = None
        # 当日结算价 (收盘后确定)
        self.settlement_price: Decimal | None = None

        self.closed_records: list[ClosedTradeRecord] = []

    @property
    def open_long_lots(self) -> tuple[PositionLot, ...]:
        return tuple(self._long_lots)

    @property
    def open_short_lots(self) -> tuple[PositionLot, ...]:
        return tuple(self._short_lots)

    def add_open_lot(self, trade: Trade) -> None:
        lot = PositionLot(
            trade_id=trade.trade_id,
            instrument=self.instrument,
            side=trade.side,
            price=trade.price,
            quantity=trade.quantity,
            open_date=trade.trading_day,
        )
        if trade.side == Side.BUY:
            self._long_lots.append(lot)
        else:
            self._short_lots.append(lot)

    def close_lot(
        self,
        trade: Trade,
        pos_detail: PositionDetail,
    ) -> ClosedTradeRecord:
        """执行平仓核算，同时计算 mtm_close_pnl 与 trade_close_pnl."""
        qty = trade.quantity
        mult = self.multiplier
        side_sign = Decimal("1") if trade.side == Side.SELL else Decimal("-1")

        # 1. 计算逐日盯市平仓盈亏 (mtm_close_pnl)
        # 今仓按开仓价，昨仓按上一结算价
        if trade.offset == Offset.CLOSE_YESTERDAY:
            if self.pre_settlement_price is None:
                raise ValueError("closing yesterday position requires pre_settlement_price")
            mtm_benchmark = self.pre_settlement_price
        elif trade.offset == Offset.CLOSE_TODAY:
            # 今仓基准价: 取最近今仓批次的开仓价
            lots = self._long_lots if trade.side == Side.SELL else self._short_lots
            mtm_benchmark = lots[0].price if lots else trade.price
        else:
            mtm_benchmark = self.pre_settlement_price or trade.price

        mtm_pnl = (trade.price - mtm_benchmark) * Decimal(qty) * mult * side_sign

        # 2. 计算逐笔平仓盈亏 (trade_close_pnl): FIFO 匹配原始开仓批次
        lots = self._long_lots if trade.side == Side.SELL else self._short_lots
        remaining_to_close = qty
        total_trade_pnl = Decimal("0.0")
        total_matched_open_cost = Decimal("0.0")

        while remaining_to_close > 0 and lots:
            head = lots[0]
            matched_qty = min(remaining_to_close, head.quantity)
            pnl_piece = (trade.price - head.price) * Decimal(matched_qty) * mult * side_sign
            total_trade_pnl += pnl_piece
            total_matched_open_cost += head.price * Decimal(matched_qty)

            remaining_to_close -= matched_qty
            if matched_qty == head.quantity:
                lots.popleft()
            else:
                # 部分扣减批次
                lots[0] = PositionLot(
                    trade_id=head.trade_id,
                    instrument=head.instrument,
                    side=head.side,
                    price=head.price,
                    quantity=head.quantity - matched_qty,
                    open_date=head.open_date,
                    commission=head.commission,
                )

        matched_avg_open = (
            total_matched_open_cost / Decimal(qty) if qty > 0 else (mtm_benchmark)
        )

        record = ClosedTradeRecord(
            trade_id=trade.trade_id,
            instrument=self.instrument,
            close_side=trade.side,
            offset=trade.offset,
            close_price=trade.price,
            quantity=qty,
            multiplier=mult,
            close_date=trade.trading_day,
            mtm_benchmark_price=mtm_benchmark,
            mtm_close_pnl=mtm_pnl,
            matched_open_price=matched_avg_open,
            trade_close_pnl=total_trade_pnl,
        )
        self.closed_records.append(record)
        return record

    def calculate_unrealized_pnl(
        self,
        current_price: Decimal,
        pos_long: PositionDetail,
        pos_short: PositionDetail,
    ) -> Decimal:
        """计算盘中持仓浮动盈亏 (今仓对开仓价，昨仓对上一结算价)."""
        mult = self.multiplier
        pnl = Decimal("0.0")

        # 多头今仓
        if pos_long.pos_td > 0:
            avg_open = (
                sum(lot.price * Decimal(lot.quantity) for lot in self._long_lots)
                / Decimal(sum(lot.quantity for lot in self._long_lots))
                if self._long_lots
                else current_price
            )
            pnl += (current_price - avg_open) * Decimal(pos_long.pos_td) * mult

        # 多头昨仓
        if pos_long.pos_yd > 0:
            ref = self.pre_settlement_price or current_price
            pnl += (current_price - ref) * Decimal(pos_long.pos_yd) * mult

        # 空头今仓
        if pos_short.pos_td > 0:
            avg_open = (
                sum(lot.price * Decimal(lot.quantity) for lot in self._short_lots)
                / Decimal(sum(lot.quantity for lot in self._short_lots))
                if self._short_lots
                else current_price
            )
            pnl += (avg_open - current_price) * Decimal(pos_short.pos_td) * mult

        # 空头昨仓
        if pos_short.pos_yd > 0:
            ref = self.pre_settlement_price or current_price
            pnl += (ref - current_price) * Decimal(pos_short.pos_yd) * mult

        return pnl


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


class AccountLedger:
    """账户级事件账本聚合根."""

    def __init__(
        self,
        account_id: str,
        initial_capital: Decimal = Decimal("1000000.00"),
        currency_unit: Decimal = Decimal("0.01"),
    ) -> None:
        self.account_id = account_id
        self.initial_capital = initial_capital
        self.currency_unit = currency_unit

        # 核心资金余额 (随出入金、已结转结算盈亏、盯市平仓盈亏和手续费更新)
        self.balance: Decimal = initial_capital
        self.total_commission: Decimal = Decimal("0.00")
        self.realized_mtm_pnl: Decimal = Decimal("0.00")
        self.realized_trade_pnl: Decimal = Decimal("0.00")

        self.frozen_margin: Decimal = Decimal("0.00")
        self.frozen_fee: Decimal = Decimal("0.00")

        self.position_manager: PositionManager = PositionManager(account_id)
        self._instrument_ledgers: dict[InstrumentId, InstrumentLedger] = {}

    def get_instrument_ledger(
        self,
        instrument: InstrumentId,
        multiplier: Decimal = Decimal("10"),
    ) -> InstrumentLedger:
        if instrument not in self._instrument_ledgers:
            self._instrument_ledgers[instrument] = InstrumentLedger(instrument, multiplier)
        return self._instrument_ledgers[instrument]

    def set_pre_settlement_price(self, instrument: InstrumentId, price: Decimal) -> None:
        ledger = self.get_instrument_ledger(instrument)
        ledger.pre_settlement_price = price

    def on_trade(
        self,
        trade: Trade,
        commission: Decimal = Decimal("0.00"),
        multiplier: Decimal = Decimal("10"),
        client_order_id: str | None = None,
    ) -> ClosedTradeRecord | None:
        """处理真实成交入账 (更新持仓、资金与两种盈亏视图)."""
        inst = trade.instrument
        ledger = self.get_instrument_ledger(inst, multiplier)

        # 1. 扣减手续费
        self.total_commission += commission
        self.balance -= commission

        record: ClosedTradeRecord | None = None
        # 2. 开仓 vs 平仓处理
        if trade.offset == Offset.OPEN:
            ledger.add_open_lot(trade)
            self.position_manager.apply_trade(trade, client_order_id=client_order_id)
        else:
            # 平仓
            target_side = PositionSide.LONG if trade.side == Side.SELL else PositionSide.SHORT
            pos = self.position_manager.get_position(inst, target_side)
            record = ledger.close_lot(trade, pos)

            # 扣减持仓与冻结
            self.position_manager.apply_trade(trade, client_order_id=client_order_id)

            # 更新盯市平仓盈亏入账至 balance
            self.realized_mtm_pnl += record.mtm_close_pnl
            self.balance += record.mtm_close_pnl

            # 累计逐笔平仓盈亏 (只供统计分析，绝不重复加到 balance!)
            self.realized_trade_pnl += record.trade_close_pnl

        return record

    def settle_day(
        self,
        settlement_prices: dict[InstrumentId, Decimal],
        new_trading_day: date,
    ) -> Decimal:
        """日终结算结转 (FR-CAL-05, FR-CAL-06, A06, §4.4 样例).

        对所有未平持仓，按今日结算价与计价基准计算结算盈亏，结转入 balance；
        更新次日计价基准为结算价；执行今昨仓结转。
        返回今日结转的总结算盈亏.
        """
        day_settlement_pnl = Decimal("0.00")

        for inst, ledger in self._instrument_ledgers.items():
            settle_price = settlement_prices.get(inst)
            if settle_price is None:
                continue

            ledger.settlement_price = settle_price
            mult = ledger.multiplier
            pos_long, pos_short = self.position_manager.get_both_positions(inst)

            # 多头结算:
            # 今仓: (settle - open_price)
            if pos_long.pos_td > 0:
                if ledger.open_long_lots:
                    for lot in ledger.open_long_lots:
                        pnl = (settle_price - lot.price) * Decimal(lot.quantity) * mult
                        day_settlement_pnl += pnl
                else:
                    # 极端恢复场景兜底: 若无批次则使用基准价计算
                    ref = ledger.pre_settlement_price or settle_price
                    pnl = (settle_price - ref) * Decimal(pos_long.pos_td) * mult
                    day_settlement_pnl += pnl

            # 昨仓: (settle - pre_settle)
            if pos_long.pos_yd > 0:
                ref = ledger.pre_settlement_price or settle_price
                pnl = (settle_price - ref) * Decimal(pos_long.pos_yd) * mult
                day_settlement_pnl += pnl

            # 空头结算:
            if pos_short.pos_td > 0:
                if ledger.open_short_lots:
                    for lot in ledger.open_short_lots:
                        pnl = (lot.price - settle_price) * Decimal(lot.quantity) * mult
                        day_settlement_pnl += pnl
                else:
                    ref = ledger.pre_settlement_price or settle_price
                    pnl = (ref - settle_price) * Decimal(pos_short.pos_td) * mult
                    day_settlement_pnl += pnl

            if pos_short.pos_yd > 0:
                ref = ledger.pre_settlement_price or settle_price
                pnl = (ref - settle_price) * Decimal(pos_short.pos_yd) * mult
                day_settlement_pnl += pnl

            # 更新基准价：今日结算价成为次日的上一结算价
            ledger.pre_settlement_price = settle_price
            ledger.settlement_price = None

        # 将未实现结算盈亏结转入结存余额
        self.balance += day_settlement_pnl
        # 重置当日盯市平仓盈亏
        self.realized_mtm_pnl = Decimal("0.00")

        # 今昨仓跨日转换
        self.position_manager.advance_trading_day(new_trading_day)

        return day_settlement_pnl

    def get_funds_state(
        self,
        current_prices: dict[InstrumentId, Decimal] | None = None,
        margin_rates: dict[InstrumentId, Decimal] | None = None,
    ) -> AccountFundsState:
        """获取当前账户资金与三字段状态快照."""
        current_prices = current_prices or {}
        margin_rates = margin_rates or {}

        # 1. 计算未实现浮动盯市盈亏
        unrealized = Decimal("0.00")
        total_margin = Decimal("0.00")

        for inst, ledger in self._instrument_ledgers.items():
            price = current_prices.get(inst, ledger.pre_settlement_price or Decimal("0.0"))
            pos_l, pos_s = self.position_manager.get_both_positions(inst)
            unrealized += ledger.calculate_unrealized_pnl(price, pos_l, pos_s)

            # 简单保证金估算: 价格 * 手数 * 乘数 * 保证金率
            rate = margin_rates.get(inst, Decimal("0.10"))
            mult = ledger.multiplier
            margin_l = price * Decimal(pos_l.total_position) * mult * rate
            margin_s = price * Decimal(pos_s.total_position) * mult * rate
            total_margin += margin_l + margin_s

        # 动态权益 = balance + unrealized
        total_equity = self.balance + unrealized

        # 字段 1: broker_available (柜台可用)
        broker_avail = total_equity - total_margin - self.frozen_margin - self.frozen_fee

        # 字段 2: available_for_new_trades (开仓额度: 保守口径，浮盈不增加开仓额度，浮亏扣减)
        effective_equity = min(self.balance, total_equity)
        avail_new_trades = max(Decimal("0.00"), effective_equity - total_margin - self.frozen_margin - self.frozen_fee)

        # 字段 3: margin_coverage_equity (保证金覆盖权益)
        margin_coverage = total_equity
        risk_ratio = (total_margin / total_equity) if total_equity > 0 else Decimal("1.0")

        return AccountFundsState(
            balance=self.balance,
            total_equity=total_equity,
            margin_used=total_margin,
            frozen_margin=self.frozen_margin,
            frozen_fee=self.frozen_fee,
            unrealized_pnl=unrealized,
            realized_mtm_pnl=self.realized_mtm_pnl,
            realized_trade_pnl=self.realized_trade_pnl,
            total_commission=self.total_commission,
            broker_available=broker_avail,
            available_for_new_trades=avail_new_trades,
            margin_coverage_equity=margin_coverage,
            risk_ratio=risk_ratio,
        )
