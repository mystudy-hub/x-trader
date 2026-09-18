"""[Domain 层] 原子风控、自成交防范、熔断状态机与控制代次校验 (S2-06, S2-10, FR-RISK-01~07, A08, A09, A23).

主要职责:
1. 控制代次 (ControlEpoch: controller_id + epoch) 校验：开仓、平仓、撤单、改单及恢复命令
   一律拒绝旧代次；旧代次的真实成交仍客观入账 (由订单/成交事实分支处理，本模块不拦截事实) (A23)
2. 熔断状态机 (RiskState): NORMAL -> REDUCE_ONLY -> CANCELING -> FLATTENING -> HALTED，只能前进；
   重置必须同时满足异常原因消除且账户一致 (FR-RISK-05)
3. 普通开仓冷却只阻断开仓，绝不阻断平仓 / 撤单 (A09)
4. 清仓请求 (flatten_requested) 绝不等同于清仓完成；剩余风险持续可见 (unresolved_flatten)
5. 自成交防范 (FR-RISK-04)：相反方向且价格交叉的活动委托；市价单保守视为与任意反向活动单交叉
6. 原子检查与预占 (FR-RISK-01, A08)：所有检查通过后才同时冻结持仓与预占资金；任一失败则不预占
7. 持久化形态计数器 (FR-RISK-02)：按 (交易日, 合约) 计数，可快照 / 恢复，跨日重置幂等；
   开仓计数在委托被接受时触发，拒绝 / 撤单释放未成交部分
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any

from qh_trader.core.clock import utc_timestamp
from qh_trader.core.constants import Offset, OrderType, PositionSide, Side
from qh_trader.core.objects import ContractSpec, ControlEpoch, InstrumentId, OrderIntent
from qh_trader.domain.ledger import AccountFundsState, AccountLedger, FundsReservation
from qh_trader.domain.limits import ExchangeLimits
from qh_trader.domain.orders import Order
from qh_trader.domain.positions import PositionDetail, PositionManager, PositionReservation, target_position_side


class RiskViolationError(ValueError):
    """风控检查拒绝异常."""


class EpochViolationError(ValueError):
    """控制代次失效异常."""


class RiskStateTransitionError(ValueError):
    """非法的风控状态转换 (只能前进，重置需满足前置条件)."""


class RiskState(StrEnum):
    """熔断与风控运行状态 (FR-RISK-05)."""

    NORMAL = "NORMAL"
    REDUCE_ONLY = "REDUCE_ONLY"
    CANCELING = "CANCELING"
    FLATTENING = "FLATTENING"
    HALTED = "HALTED"


RISK_STATE_ORDER: tuple[RiskState, ...] = (
    RiskState.NORMAL,
    RiskState.REDUCE_ONLY,
    RiskState.CANCELING,
    RiskState.FLATTENING,
    RiskState.HALTED,
)

COUNTER_SNAPSHOT_VERSION = 1


@dataclass(frozen=True, slots=True)
class RiskEvent:
    """风控状态变化审计记录."""

    at: datetime
    from_state: RiskState
    to_state: RiskState
    reason: str


@dataclass(frozen=True, slots=True)
class RemainingRisk:
    instrument: InstrumentId
    side: PositionSide
    lots: int
    frozen: int


@dataclass
class _Counters:
    # (trading_day, instrument) -> lots / count
    open_lots: dict[tuple[date, str], int] = field(default_factory=dict)
    cancels: dict[tuple[date, str], int] = field(default_factory=dict)


def _epoch_value(command_epoch: int | ControlEpoch) -> int:
    return command_epoch.epoch if isinstance(command_epoch, ControlEpoch) else command_epoch


class RiskManager:
    """原子事前风控与风险状态机聚合根."""

    def __init__(
        self,
        account_id: str,
        control: ControlEpoch | None = None,
        limits: ExchangeLimits | None = None,
        trading_day: date | None = None,
        initial_epoch: int | None = None,
        controller_id: str = "controller-0",
    ) -> None:
        self.account_id = account_id
        if control is None:
            control = ControlEpoch(controller_id=controller_id, epoch=1 if initial_epoch is None else initial_epoch)
        self.control: ControlEpoch = control
        self.risk_state: RiskState = RiskState.NORMAL
        self.risk_events: list[RiskEvent] = []
        self.limits: ExchangeLimits = limits or ExchangeLimits()
        self.trading_day: date | None = trading_day

        self.open_cooldown_until: datetime | None = None
        self.flatten_requested: bool = False
        self.flatten_reason: str | None = None

        self._counters = _Counters()

    # ------------------------------------------------------------------ 控制代次
    @property
    def current_epoch(self) -> int:
        return self.control.epoch

    @property
    def controller_id(self) -> str:
        return self.control.controller_id

    def advance_epoch(self, new_epoch: int, controller_id: str | None = None) -> ControlEpoch:
        """推进控制代次 (严格递增，绝不回退或复用)."""
        if new_epoch <= self.control.epoch:
            raise EpochViolationError(
                f"new epoch ({new_epoch}) must be greater than current ({self.control.epoch}); epochs are never reused"
            )
        self.control = ControlEpoch(controller_id=controller_id or self.control.controller_id, epoch=new_epoch)
        return self.control

    def assume_control(self, control: ControlEpoch) -> ControlEpoch:
        """新控制者接管：持久化新 controller_id 与更高代次后，旧代次命令一律拒绝."""
        return self.advance_epoch(control.epoch, control.controller_id)

    def check_command_epoch(self, command_epoch: int | ControlEpoch, command: str = "command") -> None:
        """校验交易命令代次 (FR-RISK-07, A23)。旧代次或未来代次的命令都必须被拒绝."""
        epoch = _epoch_value(command_epoch)
        if epoch != self.control.epoch:
            raise EpochViolationError(
                f"{command} epoch mismatch: command_epoch={epoch}, current_epoch={self.control.epoch} "
                f"(controller={self.control.controller_id})"
            )
        if isinstance(command_epoch, ControlEpoch) and command_epoch.controller_id != self.control.controller_id:
            raise EpochViolationError(
                f"{command} issued by {command_epoch.controller_id}, but controller is {self.control.controller_id}"
            )

    def check_cancel_command(
        self,
        order: Order,
        command_epoch: int | ControlEpoch,
        trading_day: date | None = None,
    ) -> None:
        """撤单命令：代次校验 + 撤单额度硬限制。任何风控状态与冷却都不阻断撤单 (A09)."""
        self.check_command_epoch(command_epoch, "cancel")
        if order.is_terminal:
            raise RiskViolationError(f"cannot cancel terminal order {order.client_order_id}: {order.status}")
        day = self._day(trading_day)
        self.limits.check_cancel(order.instrument, day, self.today_cancels_count(order.instrument, day))

    def check_amend_command(
        self,
        order: Order,
        command_epoch: int | ControlEpoch,
        new_quantity: int | None = None,
        new_limit_price_ticks: int | None = None,
        trading_day: date | None = None,
        price_band: tuple[int, int] | None = None,
        now: datetime | None = None,
    ) -> None:
        """改单命令：视为撤旧发新，需通过代次、撤单额度、价格带；增加风险的改动受熔断与冷却约束."""
        self.check_command_epoch(command_epoch, "amend")
        if order.is_terminal:
            raise RiskViolationError(f"cannot amend terminal order {order.client_order_id}: {order.status}")
        day = self._day(trading_day)
        self.limits.check_cancel(order.instrument, day, self.today_cancels_count(order.instrument, day))
        if new_limit_price_ticks is not None and price_band is not None:
            lower, upper = price_band
            if new_limit_price_ticks < lower or new_limit_price_ticks > upper:
                raise RiskViolationError(f"amended price {new_limit_price_ticks} outside price band [{lower}, {upper}]")
        increases_risk = order.offset == Offset.OPEN and (
            new_quantity is None or new_quantity > order.leaves_qty or new_limit_price_ticks is not None
        )
        if increases_risk:
            self._check_open_allowed(order.client_order_id, now)

    def check_recovery_command(self, command_epoch: int | ControlEpoch) -> None:
        """恢复交易 / 对账处置等控制命令同样必须持有当前代次 (A23)."""
        self.check_command_epoch(command_epoch, "recovery")

    # ------------------------------------------------------------------ 熔断状态机
    def escalate(self, reason: str, target: RiskState | None = None, at: datetime | None = None) -> RiskState:
        """只允许前进：默认进入下一状态；显式 target 必须严格晚于当前状态."""
        idx = RISK_STATE_ORDER.index(self.risk_state)
        if target is None:
            if idx + 1 >= len(RISK_STATE_ORDER):
                raise RiskStateTransitionError(f"already at terminal risk state {self.risk_state}")
            target = RISK_STATE_ORDER[idx + 1]
        elif RISK_STATE_ORDER.index(target) <= idx:
            raise RiskStateTransitionError(
                f"risk state can only move forward: {self.risk_state} -> {target} is not allowed"
            )
        self._transition(target, reason, at)
        return self.risk_state

    def reset_risk_state(
        self,
        cause_cleared: bool,
        account_consistent: bool,
        reason: str = "manual reset",
        at: datetime | None = None,
    ) -> None:
        """重置为 NORMAL：必须异常原因消除且账户一致 (FR-RISK-05 规则 5)."""
        if not (cause_cleared is True and account_consistent is True):
            raise RiskStateTransitionError(
                f"reset refused: cause_cleared={cause_cleared}, account_consistent={account_consistent}; "
                "both must be true"
            )
        if self.risk_state == RiskState.NORMAL:
            return
        self._transition(RiskState.NORMAL, reason, at)
        self.flatten_requested = False
        self.flatten_reason = None

    def _transition(self, target: RiskState, reason: str, at: datetime | None = None) -> None:
        stamp = utc_timestamp(at) if at is not None else datetime.now(timezone.utc)
        self.risk_events.append(RiskEvent(stamp, self.risk_state, target, reason))
        self.risk_state = target

    # ------------------------------------------------------------------ 冷却与清仓可见性
    def enter_open_cooldown(self, until: datetime) -> None:
        """普通开仓冷却：只阻断开仓，不阻断平仓与撤单 (A09)."""
        self.open_cooldown_until = utc_timestamp(until)

    def in_open_cooldown(self, now: datetime | None) -> bool:
        if self.open_cooldown_until is None:
            return False
        if now is None:
            return True
        return utc_timestamp(now) < self.open_cooldown_until

    def request_flatten(self, reason: str) -> None:
        """记录清仓请求；这只是请求，不代表仓位归零."""
        self.flatten_requested = True
        self.flatten_reason = reason

    @staticmethod
    def remaining_risk(position_manager: PositionManager) -> tuple[RemainingRisk, ...]:
        return tuple(
            RemainingRisk(p.instrument, p.side, p.total_position, p.total_frozen)
            for p in position_manager.all_positions()
            if p.total_position > 0
        )

    def flattened(self, position_manager: PositionManager) -> bool:
        return not self.remaining_risk(position_manager)

    def unresolved_flatten(self, position_manager: PositionManager) -> bool:
        """清仓已请求但仍有剩余仓位 (FR-RISK-05 规则 4)."""
        return self.flatten_requested and not self.flattened(position_manager)

    # ------------------------------------------------------------------ 计数器
    def _day(self, trading_day: date | None) -> date:
        day = trading_day if trading_day is not None else self.trading_day
        if day is None:
            raise ValueError("trading_day is required: counters and limits are keyed by trading day")
        return day

    def today_open_lots(self, instrument: InstrumentId, trading_day: date | None = None) -> int:
        return self._counters.open_lots.get((self._day(trading_day), str(instrument)), 0)

    def today_cancels_count(self, instrument: InstrumentId, trading_day: date | None = None) -> int:
        return self._counters.cancels.get((self._day(trading_day), str(instrument)), 0)

    def on_order_accepted(self, order: OrderIntent, trading_day: date | None = None) -> None:
        """开仓计数触发点：委托被柜台接受."""
        if order.offset == Offset.OPEN:
            key = (self._day(trading_day), str(order.instrument))
            self._counters.open_lots[key] = self._counters.open_lots.get(key, 0) + order.quantity

    def on_order_rejected(
        self,
        order: OrderIntent,
        was_accepted: bool = False,
        trading_day: date | None = None,
    ) -> None:
        """拒绝：若曾被接受计数，则回退整单开仓计数."""
        if was_accepted and order.offset == Offset.OPEN:
            self._decrement_open(order.instrument, order.quantity, trading_day)

    def on_order_canceled(
        self,
        order: OrderIntent | InstrumentId,
        unfilled_qty: int = 0,
        trading_day: date | None = None,
    ) -> None:
        """撤单成功：累加撤单计数；开仓单未成交部分从开仓计数中扣回."""
        day = self._day(trading_day)
        instrument = order if isinstance(order, InstrumentId) else order.instrument
        key = (day, str(instrument))
        self._counters.cancels[key] = self._counters.cancels.get(key, 0) + 1
        if isinstance(order, OrderIntent) and order.offset == Offset.OPEN and unfilled_qty > 0:
            self._decrement_open(instrument, unfilled_qty, day)

    def _decrement_open(self, instrument: InstrumentId, qty: int, trading_day: date | None) -> None:
        key = (self._day(trading_day), str(instrument))
        self._counters.open_lots[key] = max(0, self._counters.open_lots.get(key, 0) - qty)

    def reset_daily_counters(self, new_trading_day: date) -> bool:
        """跨日：切换计数交易日。同一交易日重复调用幂等 (A26)，历史日计数保留供持久化对账."""
        if self.trading_day == new_trading_day:
            return False
        if self.trading_day is not None and new_trading_day < self.trading_day:
            raise ValueError(f"trading day cannot move backwards: {self.trading_day} -> {new_trading_day}")
        self.trading_day = new_trading_day
        return True

    def snapshot_counters(self) -> dict[str, Any]:
        """可序列化的计数快照 (随事件持久化，重启不清零)."""
        return {
            "version": COUNTER_SNAPSHOT_VERSION,
            "trading_day": self.trading_day.isoformat() if self.trading_day else None,
            "open_lots": [
                {"trading_day": d.isoformat(), "instrument": inst, "lots": lots}
                for (d, inst), lots in sorted(self._counters.open_lots.items())
            ],
            "cancels": [
                {"trading_day": d.isoformat(), "instrument": inst, "count": n}
                for (d, inst), n in sorted(self._counters.cancels.items())
            ],
        }

    def restore_counters(self, snapshot: dict[str, Any]) -> None:
        if snapshot.get("version") != COUNTER_SNAPSHOT_VERSION:
            raise ValueError(f"unsupported counter snapshot version: {snapshot.get('version')}")
        counters = _Counters()
        for row in snapshot.get("open_lots", []):
            counters.open_lots[(date.fromisoformat(row["trading_day"]), row["instrument"])] = int(row["lots"])
        for row in snapshot.get("cancels", []):
            counters.cancels[(date.fromisoformat(row["trading_day"]), row["instrument"])] = int(row["count"])
        self._counters = counters
        day = snapshot.get("trading_day")
        if day is not None:
            self.trading_day = date.fromisoformat(day)

    # ------------------------------------------------------------------ 事前风控
    def _check_open_allowed(self, client_order_id: str, now: datetime | None) -> None:
        if self.risk_state != RiskState.NORMAL:
            raise RiskViolationError(f"risk state is {self.risk_state}; open orders are blocked: {client_order_id}")
        if self.in_open_cooldown(now):
            raise RiskViolationError(f"open cooldown active until {self.open_cooldown_until}: {client_order_id}")

    def check_order(
        self,
        order: OrderIntent,
        command_epoch: int | ControlEpoch,
        funds: AccountFundsState,
        current_pos: PositionDetail,
        active_orders: list[Order] | None = None,
        estimated_margin_needed: Decimal = Decimal("0.00"),
        trading_day: date | None = None,
        price_band: tuple[int, int] | None = None,
        contract_spec: ContractSpec | None = None,
        natural_person: bool = False,
        now: datetime | None = None,
        pending_open_lots: int = 0,
    ) -> None:
        """事前风控统一校验入口 (FR-RISK-01)，无副作用.

        若未通过校验，抛出 RiskViolationError / EpochViolationError / LimitViolationError.
        """
        # 1. 控制代次
        self.check_command_epoch(command_epoch, "order")
        day = self._day(trading_day)

        # 2. 熔断状态机与冷却 (FR-RISK-05): HALTED 阻断一切新委托；其余状态只阻断开仓
        if self.risk_state == RiskState.HALTED:
            raise RiskViolationError(f"risk state is HALTED; new orders are blocked: {order.client_order_id}")
        if order.offset == Offset.OPEN:
            self._check_open_allowed(order.client_order_id, now)

        # 3. 交易所硬约束 (对平仓同样生效的价格带；开仓限额含在途开仓)
        self.limits.check_order(
            order,
            day,
            current_open_lots_today=self.today_open_lots(order.instrument, day),
            current_holding_lots=current_pos.total_position + pending_open_lots,
            price_band=price_band,
            contract_spec=contract_spec,
            natural_person=natural_person,
        )

        # 4. 开仓资金充足性 (FR-LED-05)；可用资金已扣除在途预占
        if order.offset == Offset.OPEN and estimated_margin_needed > funds.available_for_new_trades:
            raise RiskViolationError(
                f"insufficient available funds for open: needed={estimated_margin_needed}, "
                f"available={funds.available_for_new_trades}"
            )

        # 5. 自成交防范 (FR-RISK-04)
        if active_orders:
            self._check_self_trade(order, active_orders)

    def _check_self_trade(self, order: OrderIntent, active_orders: list[Order]) -> None:
        """同合约、相反方向、价格可能交叉的活动委托 (含发送中 / 撤单未确认 / 未知状态)."""
        for active in active_orders:
            if not active.is_active or active.instrument != order.instrument or active.side == order.side:
                continue
            new_price = order.limit_price_ticks
            old_price = active.limit_price_ticks
            # 任一方为市价单：保守视为必然交叉
            if order.order_type == OrderType.MARKET or active.order_type == OrderType.MARKET or old_price is None:
                raise RiskViolationError(
                    f"self-trade prevented: market order crosses active opposite order {active.client_order_id}"
                )
            assert new_price is not None
            if order.side == Side.BUY and new_price >= old_price:
                raise RiskViolationError(
                    f"self-trade prevented: new buy price ({new_price}) >= "
                    f"existing sell order ({active.client_order_id}, price={old_price})"
                )
            if order.side == Side.SELL and new_price <= old_price:
                raise RiskViolationError(
                    f"self-trade prevented: new sell price ({new_price}) <= "
                    f"existing buy order ({active.client_order_id}, price={old_price})"
                )

    # ------------------------------------------------------------------ 原子检查与预占
    def check_and_reserve(
        self,
        intent: OrderIntent,
        command_epoch: int | ControlEpoch,
        ledger: AccountLedger,
        position_manager: PositionManager,
        active_orders: list[Order],
        margin: Decimal,
        fee: Decimal,
        trading_day: date,
        funds: AccountFundsState | None = None,
        price_band: tuple[int, int] | None = None,
        contract_spec: ContractSpec | None = None,
        natural_person: bool = False,
        now: datetime | None = None,
    ) -> tuple[PositionReservation, FundsReservation]:
        """检查与预占原子执行 (A08)：全部检查通过后才同时冻结持仓与预占资金；任一失败则什么都不预占.

        在途开仓预占 (未成交) 计入持仓限额；在途资金预占已经通过 ledger.frozen_margin
        从 available_for_new_trades 中扣除。
        """
        if position_manager.get_reservation(intent.client_order_id) is not None:
            raise RiskViolationError(f"order {intent.client_order_id} already has a position reservation")
        if ledger.get_funds_reservation(intent.client_order_id) is not None:
            raise RiskViolationError(f"order {intent.client_order_id} already has a funds reservation")

        target_side = target_position_side(intent.side, intent.offset)
        current_pos = position_manager.get_position(intent.instrument, target_side)
        pending_open = sum(
            r.remaining_qty
            for r in position_manager.reservations()
            if r.offset == Offset.OPEN and r.instrument == intent.instrument and r.target_pos_side == target_side
        )
        funds_state = funds if funds is not None else ledger.get_funds_state()

        self.check_order(
            intent,
            command_epoch,
            funds_state,
            current_pos,
            active_orders=active_orders,
            estimated_margin_needed=margin + fee,
            trading_day=trading_day,
            price_band=price_band,
            contract_spec=contract_spec,
            natural_person=natural_person,
            now=now,
            pending_open_lots=pending_open,
        )

        if intent.offset != Offset.OPEN and current_pos.total_available < intent.quantity:
            raise RiskViolationError(
                f"insufficient closable position for {intent.client_order_id}: "
                f"available={current_pos.total_available}, requested={intent.quantity}"
            )

        pos_res = position_manager.reserve_for_order(
            intent.client_order_id, intent.instrument, intent.side, intent.offset, intent.quantity
        )
        try:
            funds_res = ledger.reserve_funds(intent.client_order_id, margin, fee)
        except Exception:
            position_manager.release_reservation(intent.client_order_id)
            raise
        return pos_res, funds_res
