"""[Domain 层] 持仓四字段模型、逐单预占不变量与跨日转换 (S2-02, FR-ORD-03, FR-CAL-06).

主要职责:
1. 维护 pos_yd, pos_td, frozen_yd, frozen_td 四字段持仓不变量
2. 逐单预占 (PositionReservation) 与释放机制
3. 终态订单成交量大于已入账成交量时，保留差额冻结 (terminal_before_fills)
4. 真实去重成交扣减持仓与冻结
5. 交易日切换时的今昨仓结转 (pos_yd += pos_td, pos_td = 0)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from qh_trader.core.constants import Offset, PositionSide, Side
from qh_trader.core.objects import InstrumentId, Position, Trade


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

        返回 (frozen_yd_allocated, frozen_td_allocated).
        """
        if quantity <= 0:
            raise ValueError(f"quantity must be positive: {quantity}")

        allocated_yd = 0
        allocated_td = 0

        if offset == Offset.CLOSE_YESTERDAY:
            if self.available_yd < quantity:
                raise ValueError(
                    f"insufficient yesterday position: available={self.available_yd}, requested={quantity}"
                )
            self.frozen_yd += quantity
            allocated_yd = quantity
        elif offset == Offset.CLOSE_TODAY:
            if self.available_td < quantity:
                raise ValueError(f"insufficient today position: available={self.available_td}, requested={quantity}")
            self.frozen_td += quantity
            allocated_td = quantity
        else:
            raise ValueError(f"cannot freeze for offset: {offset}")

        self._validate_invariants()
        return allocated_yd, allocated_td

    def release_freeze(self, frozen_yd_to_release: int, frozen_td_to_release: int) -> None:
        """释放平仓冻结 (如撤单或废单确认不会成交的部分)."""
        if frozen_yd_to_release < 0 or frozen_td_to_release < 0:
            raise ValueError("released quantities cannot be negative")
        self.frozen_yd = max(0, self.frozen_yd - frozen_yd_to_release)
        self.frozen_td = max(0, self.frozen_td - frozen_td_to_release)
        self._validate_invariants()

    def apply_fill(
        self,
        quantity: int,
        offset: Offset,
        frozen_yd_consumed: int = 0,
        frozen_td_consumed: int = 0,
    ) -> None:
        """成交入账更新持仓.

        开仓增加今仓；平仓扣减持仓并消耗对应的冻结.
        """
        if quantity <= 0:
            raise ValueError(f"fill quantity must be positive: {quantity}")

        if offset == Offset.OPEN:
            self.pos_td += quantity
        elif offset == Offset.CLOSE_TODAY:
            if self.pos_td < quantity:
                raise ValueError(f"cannot close more today position than exists: pos_td={self.pos_td}, fill={quantity}")
            self.pos_td -= quantity
            consumed_td = frozen_td_consumed if frozen_td_consumed > 0 else min(self.frozen_td, quantity)
            self.frozen_td = max(0, self.frozen_td - consumed_td)
            remaining = quantity - consumed_td
            if remaining > 0 and (frozen_yd_consumed > 0 or self.frozen_td == 0):
                self.frozen_yd = max(0, self.frozen_yd - min(self.frozen_yd, remaining))
        elif offset == Offset.CLOSE_YESTERDAY:
            if self.pos_yd < quantity:
                raise ValueError(
                    f"cannot close more yesterday position than exists: pos_yd={self.pos_yd}, fill={quantity}"
                )
            self.pos_yd -= quantity
            consumed_yd = frozen_yd_consumed if frozen_yd_consumed > 0 else min(self.frozen_yd, quantity)
            self.frozen_yd = max(0, self.frozen_yd - consumed_yd)
            remaining = quantity - consumed_yd
            if remaining > 0 and (frozen_td_consumed > 0 or self.frozen_yd == 0):
                self.frozen_td = max(0, self.frozen_td - min(self.frozen_td, remaining))
        else:
            raise ValueError(f"unsupported fill offset: {offset}")

        self._validate_invariants()

    def advance_trading_day(self) -> None:
        """交易日切换 (FR-CAL-06):

        今仓并入昨仓，今仓与今仓冻结清零，昨仓冻结继承隔夜未成交平仓单的冻结.
        """
        self.pos_yd += self.pos_td
        self.pos_td = 0
        # 隔夜保留的今仓冻结并入昨仓冻结
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

    @property
    def remaining_qty(self) -> int:
        return max(0, self.quantity - self.accounted_fill_qty)


class PositionManager:
    """账户持仓聚合根 (管理所有合约的双向持仓与逐单预占)."""

    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        # (instrument, PositionSide) -> PositionDetail
        self._positions: dict[tuple[InstrumentId, PositionSide], PositionDetail] = {}
        # client_order_id -> PositionReservation
        self._reservations: dict[str, PositionReservation] = {}

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

        f_yd, f_td = 0, 0
        # 平仓目标持仓方向: 卖平平多头(LONG)，买平平空头(SHORT)
        if side == Side.SELL:
            target_pos_side = PositionSide.LONG
        else:
            target_pos_side = PositionSide.SHORT

        if offset in {Offset.CLOSE_TODAY, Offset.CLOSE_YESTERDAY}:
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
        )
        self._reservations[client_order_id] = reservation
        return reservation

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

        # 柜台确认成交的手数 (不能超过原委托量)
        confirmed_fill = min(res.quantity, max(res.accounted_fill_qty, cumulative_fill_qty))
        # 真正被撤单、永远不会再成交的手数
        canceled_qty = max(0, res.quantity - confirmed_fill)

        if canceled_qty > 0 and (res.frozen_yd > 0 or res.frozen_td > 0):
            pos = self.get_position(res.instrument, res.target_pos_side)
            # 比例或优先释放已撤销部分的冻结
            # 优先释放今仓冻结还是昨仓冻结取决于当初分配
            release_td = min(res.frozen_td, canceled_qty)
            release_yd = min(res.frozen_yd, canceled_qty - release_td)

            res.frozen_td -= release_td
            res.frozen_yd -= release_yd
            pos.release_freeze(release_yd, release_td)

        # 若所有成交都已入账且不再有剩余冻结，移除预占记录
        if res.accounted_fill_qty >= confirmed_fill and res.frozen_yd == 0 and res.frozen_td == 0:
            self._reservations.pop(client_order_id, None)

    def apply_trade(self, trade: Trade, client_order_id: str | None = None) -> None:
        """将真实成交扣减到持仓与逐单预占."""
        inst = trade.instrument
        # 确定持仓方向
        if trade.offset == Offset.OPEN:
            target_side = PositionSide.LONG if trade.side == Side.BUY else PositionSide.SHORT
        else:
            target_side = PositionSide.LONG if trade.side == Side.SELL else PositionSide.SHORT

        pos = self.get_position(inst, target_side)

        f_yd_consumed = 0
        f_td_consumed = 0

        res: PositionReservation | None = None
        if client_order_id:
            res = self._reservations.get(client_order_id)

        if res is not None and trade.offset != Offset.OPEN:
            # 消耗预占中的冻结 (支持跨桶补扣，避免幽灵冻结)
            qty = trade.quantity
            res.accounted_fill_qty += qty
            if res.offset == Offset.CLOSE_TODAY:
                f_td_consumed = min(res.frozen_td, qty)
                rem = qty - f_td_consumed
                f_yd_consumed = min(res.frozen_yd, rem)
            elif res.offset == Offset.CLOSE_YESTERDAY:
                f_yd_consumed = min(res.frozen_yd, qty)
                rem = qty - f_yd_consumed
                f_td_consumed = min(res.frozen_td, rem)
            else:
                f_yd_consumed = min(res.frozen_yd, qty)
                rem = qty - f_yd_consumed
                f_td_consumed = min(res.frozen_td, rem)

            res.frozen_yd -= f_yd_consumed
            res.frozen_td -= f_td_consumed

            if res.accounted_fill_qty >= res.quantity and res.frozen_yd == 0 and res.frozen_td == 0:
                self._reservations.pop(client_order_id, None)

        pos.apply_fill(
            quantity=trade.quantity,
            offset=trade.offset,
            frozen_yd_consumed=f_yd_consumed,
            frozen_td_consumed=f_td_consumed,
        )

    def advance_trading_day(self, new_trading_day: date) -> None:
        """跨日今昨转换 (FR-CAL-06)."""
        for pos in self._positions.values():
            pos.advance_trading_day()
        # 预占中的今仓冻结同样结转为昨仓冻结
        for res in self._reservations.values():
            res.frozen_yd += res.frozen_td
            res.frozen_td = 0
