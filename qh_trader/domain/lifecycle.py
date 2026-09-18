"""[Domain 层] 交易日生命周期、领域事件与结算门禁 (S2-09, FR-CAL-04/05/06/12, A26).

核心规则:
1. 运行阶段与合约交易阶段、连接状态、风控状态分别管理；本管理器只维护账户运行阶段。
2. 收盘按合约登记 (SessionClosed)；只有全部已启用合约都收盘才进入 POST_CLOSE (FR-CAL-04)。
3. 结算数据就绪产生带交易日与版本的 SettlementReady；未就绪为 SETTLEMENT_PENDING，不用收盘价替代 (FR-CAL-05)。
4. 今昨转换只在已确认的 TradingDayAdvanced 中一次性执行 (FR-CAL-06)。
5. 半结算保护：结算中收到的查询保存为待核对原始快照 (交易日、批次、完整性)；持仓已切换而资金未结转的
   快照不覆盖最后一致的可交易快照；此时禁止新增风险，撤单 / 减仓与回报、对账继续 (FR-CAL-12)。
6. 日终任务按 (账户, 交易日, 任务类型, 版本) 幂等执行；重启不重复结算、转换持仓或重置计数。
7. 登录成功不自动恢复交易；越过预计开盘仍未就绪时保持门禁并告警。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from qh_trader.core.objects import InstrumentId, Position


class LifecyclePhase(StrEnum):
    INITIALIZING = "INITIALIZING"
    RECONCILING = "RECONCILING"
    READY = "READY"  # PRE_OPEN，已就绪等待开盘
    TRADING = "TRADING"  # RUNNING
    POST_CLOSE = "POST_CLOSE"
    SETTLEMENT_PENDING = "SETTLEMENT_PENDING"  # SETTLEMENT_IN_PROGRESS
    SETTLED = "SETTLED"
    MAINTENANCE = "MAINTENANCE"
    SEMI_SETTLED_HOLD = "SEMI_SETTLED_HOLD"  # 半结算状态：禁止新增风险
    CLOSED = "CLOSED"


# ---------------------------------------------------------------------- 领域事件
@dataclass(frozen=True, slots=True)
class SessionClosed:
    """FR-CAL-04：某合约的适用时段已结束."""

    instrument: InstrumentId
    trading_day: date
    closed_at: datetime
    calendar_version: str


@dataclass(frozen=True, slots=True)
class SettlementReady:
    """FR-CAL-05：官方结算数据经完整性检查后就绪."""

    trading_day: date
    version: str
    settlement_prices: dict[InstrumentId, Decimal]
    source_id: str
    published_at: datetime


@dataclass(frozen=True, slots=True)
class TradingDayAdvanced:
    """FR-CAL-06：已确认的交易日推进事件."""

    previous_trading_day: date
    new_trading_day: date
    settlement_version: str
    confirmed_at: datetime


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """查询得到的账户快照 (原始，待核对)."""

    trading_day: date
    batch_id: str
    complete: bool
    positions: tuple[Position, ...]
    balance: Decimal | None
    funds_settled_for: date | None  # 资金已结转到哪个交易日
    positions_rolled_for: date | None  # 持仓今昨已切换到哪个交易日
    captured_at: datetime

    @property
    def is_semi_settled(self) -> bool:
        return self.positions_rolled_for != self.funds_settled_for


@dataclass(frozen=True, slots=True)
class ExpectedWindow:
    """带来源的预计窗口 (维护 / 结算)，不是硬编码时刻."""

    kind: str
    start: datetime
    end: datetime
    source_id: str


@dataclass
class DailyLifecycleManager:
    """交易日生命周期管理器与结算门禁."""

    current_trading_day: date
    account_id: str = "default"
    phase: LifecyclePhase = LifecyclePhase.INITIALIZING
    enabled_instruments: set[InstrumentId] = field(default_factory=set)
    closed_instruments: set[InstrumentId] = field(default_factory=set)
    is_settlement_complete: bool = False
    settled_version: str | None = None
    semi_settled_warning: bool = False
    logged_in: bool = False
    gate_alarms: list[str] = field(default_factory=list)
    expected_windows: list[ExpectedWindow] = field(default_factory=list)

    last_consistent_snapshot: AccountSnapshot | None = None
    pending_snapshots: list[AccountSnapshot] = field(default_factory=list)
    completed_tasks: dict[tuple[str, date, str, str], Any] = field(default_factory=dict)
    events: list[Any] = field(default_factory=list)

    # ------------------------------------------------------------------ 登录与对账
    def on_login_success(self) -> None:
        """柜台登录成功 (A26: 登录成功不自动恢复交易，也不证明新交易日数据完整)."""
        self.logged_in = True
        if self.phase == LifecyclePhase.INITIALIZING:
            self.phase = LifecyclePhase.RECONCILING

    def on_reconciliation_passed(self) -> None:
        """对账自检全部通过，进入就绪状态."""
        if self.semi_settled_warning or self.pending_snapshots:
            raise ValueError("cannot become READY while semi-settled or pending snapshots remain")
        if self.phase in {
            LifecyclePhase.INITIALIZING,
            LifecyclePhase.RECONCILING,
            LifecyclePhase.CLOSED,
            LifecyclePhase.SETTLED,
            LifecyclePhase.MAINTENANCE,
        }:
            self.phase = LifecyclePhase.READY

    # ------------------------------------------------------------------ 开收盘
    def on_market_open(self, now: datetime | None = None) -> bool:
        """开盘事件触发 (门禁检查)。未就绪时保持门禁并告警，返回是否进入 TRADING."""
        if self.phase == LifecyclePhase.READY and not self.semi_settled_warning:
            self.phase = LifecyclePhase.TRADING
            return True
        stamp = now.isoformat() if now else "unknown time"
        self.gate_alarms.append(f"market open at {stamp} while phase={self.phase}; gate kept")
        return False

    def on_session_closed(self, event: SessionClosed) -> bool:
        """某合约收盘 (FR-CAL-04)；全部已启用合约收盘后才进入 POST_CLOSE。返回是否账户级收盘."""
        if event.trading_day != self.current_trading_day:
            self.gate_alarms.append(
                f"session close for {event.instrument} on {event.trading_day} != {self.current_trading_day}"
            )
            return False
        self.events.append(event)
        self.closed_instruments.add(event.instrument)
        if self.enabled_instruments and not self.enabled_instruments.issubset(self.closed_instruments):
            return False
        if self.phase == LifecyclePhase.TRADING:
            self.phase = LifecyclePhase.POST_CLOSE
        return True

    def on_market_close(self) -> None:
        """账户级收盘 (所有品种均已结束交易时由调用方确认)."""
        if self.phase == LifecyclePhase.TRADING:
            self.phase = LifecyclePhase.POST_CLOSE

    # ------------------------------------------------------------------ 结算
    def on_settlement_pending(self, reason: str = "") -> None:
        if self.phase in {LifecyclePhase.POST_CLOSE, LifecyclePhase.TRADING}:
            self.phase = LifecyclePhase.SETTLEMENT_PENDING

    def on_settlement_ready(
        self, event: SettlementReady, settle: Callable[[SettlementReady], Any] | None = None
    ) -> Any:
        """结算就绪 (FR-CAL-05)：按 (账户, 交易日, settle, 版本) 幂等执行结算."""
        if event.trading_day != self.current_trading_day:
            self.gate_alarms.append(
                f"settlement for {event.trading_day} ignored while current day is {self.current_trading_day}"
            )
            return None
        self.events.append(event)
        result = self.run_once("settle", event.trading_day, event.version, lambda: settle(event) if settle else None)
        self.is_settlement_complete = True
        self.settled_version = event.version
        self.semi_settled_warning = False
        if self.phase != LifecyclePhase.SEMI_SETTLED_HOLD:
            self.phase = LifecyclePhase.SETTLED
        return result

    def on_settlement_confirmed(self, settled_day: date, version: str = "v1") -> None:
        """官方结算确认应答 (不证明新交易日数据完整)."""
        if settled_day != self.current_trading_day:
            self.gate_alarms.append(f"settlement confirmation for {settled_day} != {self.current_trading_day}")
            return
        self.is_settlement_complete = True
        self.settled_version = version
        self.semi_settled_warning = False
        self.phase = LifecyclePhase.SETTLED

    def advance_trading_day(
        self,
        new_trading_day: date,
        settlement_version: str | None = None,
        convert: Callable[[TradingDayAdvanced], Any] | None = None,
        confirmed_at: datetime | None = None,
    ) -> TradingDayAdvanced | None:
        """推进到新交易日 (FR-CAL-06)：需要结算已完成；同一目标日重复调用幂等."""
        if new_trading_day == self.current_trading_day:
            return None
        if new_trading_day < self.current_trading_day:
            raise ValueError(f"new trading day ({new_trading_day}) must be > current ({self.current_trading_day})")
        if not self.is_settlement_complete:
            raise ValueError(
                f"cannot advance to {new_trading_day}: settlement for {self.current_trading_day} incomplete"
            )
        version = settlement_version or self.settled_version or "v1"
        event = TradingDayAdvanced(
            previous_trading_day=self.current_trading_day,
            new_trading_day=new_trading_day,
            settlement_version=version,
            confirmed_at=confirmed_at or datetime.now().astimezone(),
        )
        self.run_once("advance", self.current_trading_day, version, lambda: convert(event) if convert else None)
        self.events.append(event)
        self.current_trading_day = new_trading_day
        self.is_settlement_complete = False
        self.settled_version = None
        self.semi_settled_warning = False
        self.closed_instruments.clear()
        self.phase = LifecyclePhase.INITIALIZING
        return event

    # ------------------------------------------------------------------ 幂等任务
    def run_once(self, task_kind: str, trading_day: date, version: str, task: Callable[[], Any]) -> Any:
        """按 (账户, 交易日, 任务类型, 版本) 幂等执行；重复调用返回首次结果."""
        key = (self.account_id, trading_day, task_kind, version)
        if key in self.completed_tasks:
            return self.completed_tasks[key]
        result = task()
        self.completed_tasks[key] = result
        return result

    def has_completed(self, task_kind: str, trading_day: date, version: str) -> bool:
        return (self.account_id, trading_day, task_kind, version) in self.completed_tasks

    # ------------------------------------------------------------------ 快照与半结算
    def submit_query_snapshot(self, snapshot: AccountSnapshot) -> bool:
        """结算期间或恢复期间收到的查询快照。返回是否成为最后一致快照."""
        if not snapshot.complete or snapshot.trading_day != self.current_trading_day or snapshot.is_semi_settled:
            self.pending_snapshots.append(snapshot)
            if snapshot.is_semi_settled:
                self.on_semi_settlement_detected(
                    f"positions rolled for {snapshot.positions_rolled_for}, "
                    f"funds settled for {snapshot.funds_settled_for}"
                )
            return False
        self.last_consistent_snapshot = snapshot
        return True

    def on_semi_settlement_detected(self, reason: str = "") -> None:
        """持仓已切但资金未结转 (F10, A26)：转入 SEMI_SETTLED_HOLD，保护最后一致快照."""
        self.phase = LifecyclePhase.SEMI_SETTLED_HOLD
        self.semi_settled_warning = True
        self.gate_alarms.append(f"semi-settled snapshot: {reason}")

    def on_snapshot_verified(self, snapshot: AccountSnapshot) -> None:
        """结算单所属交易日与完整性核验完成后，发布一致快照并清除半结算保护."""
        if not snapshot.complete or snapshot.is_semi_settled:
            raise ValueError("only complete, fully settled snapshots can be published as consistent")
        self.last_consistent_snapshot = snapshot
        self.pending_snapshots = [s for s in self.pending_snapshots if s.batch_id != snapshot.batch_id]
        if not any(s.is_semi_settled for s in self.pending_snapshots):
            self.semi_settled_warning = False
            if self.phase == LifecyclePhase.SEMI_SETTLED_HOLD:
                self.phase = LifecyclePhase.SETTLED if self.is_settlement_complete else LifecyclePhase.RECONCILING

    # ------------------------------------------------------------------ 维护窗口
    def register_window(self, window: ExpectedWindow) -> None:
        self.expected_windows.append(window)

    def on_maintenance_started(self, source_id: str) -> None:
        if self.phase in {LifecyclePhase.POST_CLOSE, LifecyclePhase.SETTLEMENT_PENDING, LifecyclePhase.SETTLED}:
            self.phase = LifecyclePhase.MAINTENANCE

    def on_maintenance_ended(self, now: datetime) -> None:
        if self.phase == LifecyclePhase.MAINTENANCE:
            self.phase = LifecyclePhase.RECONCILING
        for w in self.expected_windows:
            if w.kind == "maintenance" and now > w.end:
                self.gate_alarms.append(f"maintenance ended after expected window from {w.source_id}")

    # ------------------------------------------------------------------ 门禁查询
    def can_accept_new_risk(self) -> bool:
        """是否允许接受增加风险的新报单."""
        return self.phase == LifecyclePhase.TRADING and not self.semi_settled_warning

    def can_reduce_risk(self) -> bool:
        """撤单 / 减仓在登录后任何阶段都允许提交，仍须通过控制权、状态与资金检查 (FR-CAL-12)."""
        return self.logged_in or self.phase in {LifecyclePhase.TRADING, LifecyclePhase.READY}

    @property
    def observation_active(self) -> bool:
        """行情采集、回报处理、对账与告警始终运行."""
        return True
