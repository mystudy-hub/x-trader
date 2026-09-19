"""[Domain 层] 持仓四字段模型、逐单预占不变量与跨日转换 (S2-02, FR-ORD-03, FR-CAL-06).

主要职责:
1. 维护 pos_yd, pos_td, frozen_yd, frozen_td 四字段持仓不变量
2. 逐单预占 (PositionReservation) 与释放机制；聚合冻结量只由逐单预占记录汇总变化
3. 终态订单成交量大于已入账成交量时，保留差额冻结 (terminal_before_fills)
4. 真实去重成交扣减持仓与冻结；没有预占记录的成交不消耗其他订单的冻结
5. 交易日切换时的今昨仓一次性结转 (pos_yd += pos_td, pos_td = 0)，重复调用幂等
6. 晚到回报按其原交易日处理：跨日后到达的上一交易日成交，按当时的今仓桶 (现已转为昨仓) 入账
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from qh_trader.core.constants import Offset, PositionSide, Side
from qh_trader.core.objects import InstrumentId, Position, Trade


class PositionAccountingError(ValueError):
    """持仓入账与预占不一致，必须进入对账而不是静默处理。"""


def target_position_side(side: Side, offset: Offset) -> PositionSide:
    """成交或委托影响的持仓方向：开仓同向；平仓反向 (卖平多头，买平空头)."""
    if offset == Offset.OPEN:
        return PositionSide.LONG if side == Side.BUY else PositionSide.SHORT
    return PositionSide.LONG if side == Side.SELL else PositionSide.SHORT


@dataclass
class PositionDetail:
    """单方向持仓明细实体 (多头 LONG 或 空头 SHORT)."""

    instrument: InstrumentId
    side: PositionSide
    hedge_flag: str = "SPECULATION"

    pos_yd: int = 0
    pos_td: int = 0
    frozen_yd: int = 0
    frozen_td: int = 0

    open_cost: Decimal = Decimal("0.0")
    position_cost: Decimal = Decimal("0.0")

    def __post_init__(self) -> None:
        self._validate_invariants()

    def _validate_invariants(self) -> None:
        """硬约束: 0 <= frozen <= pos."""
        if self.pos_yd < 0 or self.pos_td < 0:
            raise ValueError(f"positions cannot be negative: yd={self.pos_yd}, td={self.pos_td}")
        if self.frozen_yd < 0 or self.frozen_td < 0:
            raise ValueError(f"frozen quantities cannot be negative: yd={self.frozen_yd}, td={self.frozen_td}")
        if self.frozen_yd > self.pos_yd:
            raise ValueError(f"frozen_yd ({self.frozen_yd}) cannot exceed pos_yd ({self.pos_yd})")
        if self.frozen_td > self.pos_td:
            raise ValueError(f"frozen_td ({self.frozen_td}) cannot exceed pos_td ({self.pos_td})")

    @property
    def total_position(self) -> int:
        return self.pos_yd + self.pos_td

    @property
    def total_frozen(self) -> int:
        return self.frozen_yd + self.frozen_td

    @property
    def available_yd(self) -> int:
        return self.pos_yd - self.frozen_yd

    @property
    def available_td(self) -> int:
        return self.pos_td - self.frozen_td

    @property
    def total_available(self) -> int:
        return self.available_yd + self.available_td

    def to_position_snapshot(self) -> Position:
        """导出不可变 Core 级值对象快照."""
        self._validate_invariants()
        return Position(
            instrument=self.instrument,
            side=self.side,
            hedge_flag=self.hedge_flag,
            pos_yd=self.pos_yd,
            pos_td=self.pos_td,
            frozen_yd=self.frozen_yd,
            frozen_td=self.frozen_td,
        )

    def freeze_for_close(self, quantity: int, offset: Offset) -> tuple[int, int]:
        """平仓委托冻结持仓.

        CLOSE_YESTERDAY / CLOSE_TODAY 只冻结对应桶；统一平仓 CLOSE 先冻结昨仓再冻结今仓
        (与不区分平今平昨的交易所"先平昨"的持仓归属一致)。
        返回 (frozen_yd_allocated, frozen_td_allocated).
        """
        if quantity <= 0:
            raise ValueError(f"quantity must be positive: {quantity}")

        if offset == Offset.CLOSE_YESTERDAY:
            if self.available_yd < quantity:
                raise ValueError(
                    f"insufficient yesterday position: available={self.available_yd}, requested={quantity}"
                )
            allocated_yd, allocated_td = quantity, 0
        elif offset == Offset.CLOSE_TODAY:
            if self.available_td < quantity:
                raise ValueError(f"insufficient today position: available={self.available_td}, requested={quantity}")
            allocated_yd, allocated_td = 0, quantity
        elif offset == Offset.CLOSE:
            if self.total_available < quantity:
                raise ValueError(
                    f"insufficient total available position: available={self.total_available}, requested={quantity}"
                )
            allocated_yd = min(self.available_yd, quantity)
            allocated_td = quantity - allocated_yd
        else:
            raise ValueError(f"cannot freeze for offset: {offset}")

        self.frozen_yd += allocated_yd
        self.frozen_td += allocated_td
        self._validate_invariants()
        return allocated_yd, allocated_td

    def release_freeze(self, frozen_yd_to_release: int, frozen_td_to_release: int) -> None:
        """释放平仓冻结 (如撤单或废单确认不会成交的部分)."""
        if frozen_yd_to_release < 0 or frozen_td_to_release < 0:
            raise ValueError("released quantities cannot be negative")
        if frozen_yd_to_release > self.frozen_yd or frozen_td_to_release > self.frozen_td:
            raise PositionAccountingError(
                f"cannot release more than frozen: release=({frozen_yd_to_release}, {frozen_td_to_release}), "
                f"frozen=({self.frozen_yd}, {self.frozen_td})"
            )
        self.frozen_yd -= frozen_yd_to_release
        self.frozen_td -= frozen_td_to_release
        self._validate_invariants()

    def apply_fill(
        self,
        quantity: int,
        offset: Offset,
        frozen_yd_consumed: int = 0,
        frozen_td_consumed: int = 0,
    ) -> tuple[int, int]:
        """成交入账更新持仓，返回实际扣减的 (yd, td) 数量.

        开仓增加今仓；平仓扣减持仓。冻结只按调用方显式给出的逐单预占消耗量扣减，
        没有预占记录的成交不消耗聚合冻结 (FR-ORD-03: 冻结量由逐单预占记录汇总)。
        """
        if quantity <= 0:
            raise ValueError(f"fill quantity must be positive: {quantity}")
        if frozen_yd_consumed < 0 or frozen_td_consumed < 0:
            raise ValueError("consumed frozen quantities cannot be negative")
        if frozen_yd_consumed + frozen_td_consumed > quantity:
            raise ValueError("consumed frozen quantity cannot exceed fill quantity")

        if offset == Offset.OPEN:
            self.pos_td += quantity
            self._validate_invariants()
            return 0, 0

        if offset == Offset.CLOSE_TODAY:
            take_yd, take_td = 0, quantity
        elif offset == Offset.CLOSE_YESTERDAY:
            take_yd, take_td = quantity, 0
        elif offset == Offset.CLOSE:
            # 预占已经决定了桶归属；未预占部分先平昨再平今
            free_part = quantity - frozen_yd_consumed - frozen_td_consumed
            free_yd = max(0, min(self.available_yd, free_part))
            take_yd = frozen_yd_consumed + free_yd
            take_td = quantity - take_yd
        else:
            raise ValueError(f"unsupported fill offset: {offset}")

        if take_td > self.pos_td:
            raise PositionAccountingError(
                f"cannot close more today position than exists: pos_td={self.pos_td}, fill={take_td}"
            )
        if take_yd > self.pos_yd:
            raise PositionAccountingError(
                f"cannot close more yesterday position than exists: pos_yd={self.pos_yd}, fill={take_yd}"
            )
        if frozen_td_consumed > self.frozen_td or frozen_yd_consumed > self.frozen_yd:
            raise PositionAccountingError(
                f"reservation consumed more than frozen: consumed=({frozen_yd_consumed}, {frozen_td_consumed}), "
                f"frozen=({self.frozen_yd}, {self.frozen_td})"
            )
        # 未预占的成交必须落在未冻结的可用份额上，否则会侵占其他订单的预占
        if take_td - frozen_td_consumed > self.pos_td - self.frozen_td:
            raise PositionAccountingError(
                f"unreserved today fill ({take_td - frozen_td_consumed}) exceeds available today "
                f"({self.pos_td - self.frozen_td}); other orders' reservations must not be consumed"
            )
        if take_yd - frozen_yd_consumed > self.pos_yd - self.frozen_yd:
            raise PositionAccountingError(
                f"unreserved yesterday fill ({take_yd - frozen_yd_consumed}) exceeds available yesterday "
                f"({self.pos_yd - self.frozen_yd}); other orders' reservations must not be consumed"
            )

        self.pos_td -= take_td
        self.pos_yd -= take_yd
        self.frozen_td -= frozen_td_consumed
        self.frozen_yd -= frozen_yd_consumed
        self._validate_invariants()
        return take_yd, take_td

    def advance_trading_day(self) -> None:
        """交易日切换 (FR-CAL-06):

        今仓并入昨仓，今仓与今仓冻结清零，昨仓冻结继承隔夜未成交平仓单的冻结.
        """
        self.pos_yd += self.pos_td
        self.pos_td = 0
        self.frozen_yd += self.frozen_td
        self.frozen_td = 0
        self._validate_invariants()


@dataclass
class PositionReservation:
    """订单在持仓上的预占记录."""

    client_order_id: str
    instrument: InstrumentId
    side: Side
    offset: Offset
    quantity: int
    target_pos_side: PositionSide
    frozen_yd: int = 0
    frozen_td: int = 0
    accounted_fill_qty: int = 0
    # 预占建立时的交易日；跨日后原今仓桶已转为昨仓桶
    trading_day: date | None = None

    @property
    def remaining_qty(self) -> int:
        return max(0, self.quantity - self.accounted_fill_qty)

    @property
    def total_frozen(self) -> int:
        return self.frozen_yd + self.frozen_td


class PositionManager:
    """账户持仓聚合根 (管理所有合约的双向持仓与逐单预占)."""

    def __init__(self, account_id: str, trading_day: date | None = None) -> None:
        self.account_id = account_id
        self.current_trading_day: date | None = trading_day
        # (instrument, PositionSide) -> PositionDetail
        self._positions: dict[tuple[InstrumentId, PositionSide], PositionDetail] = {}
        # client_order_id -> PositionReservation
        self._reservations: dict[str, PositionReservation] = {}

    # ------------------------------------------------------------------ 查询
    def get_position(self, instrument: InstrumentId, side: PositionSide) -> PositionDetail:
        key = (instrument, side)
        if key not in self._positions:
            self._positions[key] = PositionDetail(instrument=instrument, side=side)
        return self._positions[key]

    def get_both_positions(self, instrument: InstrumentId) -> tuple[PositionDetail, PositionDetail]:
        return (
            self.get_position(instrument, PositionSide.LONG),
            self.get_position(instrument, PositionSide.SHORT),
        )

    def get_reservation(self, client_order_id: str) -> PositionReservation | None:
        return self._reservations.get(client_order_id)

    def reservations(self) -> tuple[PositionReservation, ...]:
        return tuple(self._reservations.values())

    def all_positions(self) -> tuple[PositionDetail, ...]:
        return tuple(self._positions.values())

    def frozen_by_reservations(self, instrument: InstrumentId, side: PositionSide) -> tuple[int, int]:
        """逐单预占记录汇总的冻结量 (用于校验聚合冻结不变量)."""
        yd = td = 0
        for res in self._reservations.values():
            if res.instrument == instrument and res.target_pos_side == side:
                yd += res.frozen_yd
                td += res.frozen_td
        return yd, td

    def verify_frozen_invariant(self) -> None:
        """聚合冻结必须等于逐单预占汇总 (FR-ORD-03)."""
        for (inst, side), pos in self._positions.items():
            yd, td = self.frozen_by_reservations(inst, side)
            if (yd, td) != (pos.frozen_yd, pos.frozen_td):
                raise PositionAccountingError(
                    f"frozen invariant broken for {inst}/{side}: aggregate=({pos.frozen_yd}, {pos.frozen_td}), "
                    f"reservations=({yd}, {td})"
                )

    # ------------------------------------------------------------------ 预占
    def reserve_for_order(
        self,
        client_order_id: str,
        instrument: InstrumentId,
        side: Side,
        offset: Offset,
        quantity: int,
    ) -> PositionReservation:
        """为报单冻结持仓份额 (开仓不冻结现有持仓；平仓冻结对应方向持仓)."""
        if client_order_id in self._reservations:
            raise ValueError(f"duplicate reservation for order: {client_order_id}")
        if quantity <= 0:
            raise ValueError(f"quantity must be positive: {quantity}")

        target_pos_side = target_position_side(side, offset)
        f_yd, f_td = 0, 0
        if offset != Offset.OPEN:
            pos = self.get_position(instrument, target_pos_side)
            f_yd, f_td = pos.freeze_for_close(quantity, offset)

        reservation = PositionReservation(
            client_order_id=client_order_id,
            instrument=instrument,
            side=side,
            offset=offset,
            quantity=quantity,
            target_pos_side=target_pos_side,
            frozen_yd=f_yd,
            frozen_td=f_td,
            trading_day=self.current_trading_day,
        )
        self._reservations[client_order_id] = reservation
        return reservation

    def release_reservation(self, client_order_id: str) -> None:
        """明确未发送 / 本地拒绝：整单释放 (FR-ORD-06 NOT_SENT)."""
        self.on_order_canceled_or_rejected(client_order_id, cumulative_fill_qty=0)

    def on_order_canceled_or_rejected(
        self,
        client_order_id: str,
        cumulative_fill_qty: int = 0,
    ) -> None:
        """处理订单终态取消/拒绝时的预占释放 (A02 / terminal_before_fills 关键逻辑).

        若 cumulative_fill_qty > accounted_fill_qty，保留未入账成交的冻结份额，
        仅释放已确认不会再成交的撤销份额 (quantity - cumulative_fill_qty).
        """
        res = self._reservations.get(client_order_id)
        if not res:
            return

        confirmed_fill = min(res.quantity, max(res.accounted_fill_qty, cumulative_fill_qty))
        # 还需要为尚未入账的成交保留的冻结
        keep_frozen = max(0, confirmed_fill - res.accounted_fill_qty)
        release_total = max(0, res.total_frozen - keep_frozen)

        if release_total > 0:
            pos = self.get_position(res.instrument, res.target_pos_side)
            release_td = min(res.frozen_td, release_total)
            release_yd = min(res.frozen_yd, release_total - release_td)
            res.frozen_td -= release_td
            res.frozen_yd -= release_yd
            pos.release_freeze(release_yd, release_td)

        if res.accounted_fill_qty >= confirmed_fill and res.total_frozen == 0:
            self._reservations.pop(client_order_id, None)

    # ------------------------------------------------------------------ 成交
    def effective_offset(self, trade: Trade, client_order_id: str | None = None) -> Offset:
        """晚到回报按其原交易日处理 (FR-CAL-06)：

        上一交易日的今仓在跨日后已转为昨仓，其平今成交应扣昨仓桶。
        跨日结转也会改写预占的平今/平昨归属；若成交在结转后到达，
        必须以预占实际冻结的仓桶为准，否则账本与持仓会各算一套桶。
        """
        if (
            self.current_trading_day is not None
            and trade.trading_day < self.current_trading_day
            and trade.offset == Offset.CLOSE_TODAY
        ):
            return Offset.CLOSE_YESTERDAY
        if client_order_id is not None:
            reservation = self._reservations.get(client_order_id)
            if (
                reservation is not None
                and trade.offset != reservation.offset
                and {trade.offset, reservation.offset} <= {Offset.CLOSE_TODAY, Offset.CLOSE_YESTERDAY}
            ):
                return reservation.offset
        return trade.offset

    def apply_trade(self, trade: Trade, client_order_id: str | None = None) -> tuple[int, int]:
        """将真实成交扣减到持仓与逐单预占，返回实际扣减的 (yd, td).

        调用方必须已经完成去重；本方法只处理唯一成交。
        """
        inst = trade.instrument
        target_side = target_position_side(trade.side, trade.offset)
        pos = self.get_position(inst, target_side)
        offset = self.effective_offset(trade, client_order_id)
        qty = trade.quantity

        f_yd_consumed = 0
        f_td_consumed = 0
        res: PositionReservation | None = self._reservations.get(client_order_id) if client_order_id else None

        if res is not None and offset != Offset.OPEN:
            if res.instrument != inst or res.target_pos_side != target_side:
                raise PositionAccountingError(f"trade {trade.trade_id} does not match reservation {client_order_id}")
            res.accounted_fill_qty += qty
            if offset == Offset.CLOSE_TODAY:
                f_td_consumed = min(res.frozen_td, qty)
            elif offset == Offset.CLOSE_YESTERDAY:
                f_yd_consumed = min(res.frozen_yd, qty)
            else:
                f_yd_consumed = min(res.frozen_yd, qty)
                f_td_consumed = min(res.frozen_td, qty - f_yd_consumed)
            res.frozen_yd -= f_yd_consumed
            res.frozen_td -= f_td_consumed

        if offset == Offset.OPEN:
            if self.current_trading_day is not None and trade.trading_day < self.current_trading_day:
                # 上一交易日的开仓成交跨日后直接计入昨仓
                pos.pos_yd += qty
                pos._validate_invariants()
                taken = (0, 0)
            else:
                taken = pos.apply_fill(quantity=qty, offset=Offset.OPEN)
            if res is not None:
                res.accounted_fill_qty += qty
        else:
            taken = pos.apply_fill(
                quantity=qty,
                offset=offset,
                frozen_yd_consumed=f_yd_consumed,
                frozen_td_consumed=f_td_consumed,
            )

        if res is not None and res.accounted_fill_qty >= res.quantity and res.total_frozen == 0:
            self._reservations.pop(client_order_id, None)
        return taken

    # ------------------------------------------------------------------ 跨日
    def advance_trading_day(self, new_trading_day: date) -> bool:
        """跨日今昨转换 (FR-CAL-06)，同一交易日重复调用幂等，返回是否实际执行了转换."""
        if self.current_trading_day is not None:
            if new_trading_day == self.current_trading_day:
                return False
            if new_trading_day < self.current_trading_day:
                raise ValueError(
                    f"trading day cannot move backwards: current={self.current_trading_day}, new={new_trading_day}"
                )
        for pos in self._positions.values():
            pos.advance_trading_day()
        # 预占中的今仓冻结同样结转为昨仓冻结；平今预占从此对应昨仓桶
        for res in self._reservations.values():
            res.frozen_yd += res.frozen_td
            res.frozen_td = 0
            if res.offset == Offset.CLOSE_TODAY:
                res.offset = Offset.CLOSE_YESTERDAY
        self.current_trading_day = new_trading_day
        return True
