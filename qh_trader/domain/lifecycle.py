"""[Domain 层] 交易日生命周期与结算门禁管理器 (S2-09, FR-CAL-04~06, FR-CAL-11~12, A26).

核心规则:
1. 登录成功不自动恢复交易，须完成前置检查与对账 (A26);
2. 半结算保护 (Semi-Settlement Hold, F10):
   若持仓已切换而资金未结转，标记为待核对快照，不覆盖最后一致状态，严格禁止新增风险;
3. 结算门禁 (Settlement Gate):
   重复日终任务不重复结算，未就绪状态即使越过开盘时刻仍保持门禁，禁止进入 TRADING.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class LifecyclePhase(StrEnum):
    INITIALIZING = "INITIALIZING"
    READY = "READY"
    TRADING = "TRADING"
    SETTLEMENT_PENDING = "SETTLEMENT_PENDING"
    SETTLED = "SETTLED"
    CLOSED = "CLOSED"
    SEMI_SETTLED_HOLD = "SEMI_SETTLED_HOLD"  # 半结算状态：禁止新增风险


@dataclass
class DailyLifecycleManager:
    """交易日生命周期管理器与结算门禁."""

    current_trading_day: date
    phase: LifecyclePhase = LifecyclePhase.INITIALIZING
    is_settlement_complete: bool = False
    semi_settled_warning: bool = False

    def on_login_success(self) -> None:
        """柜台登录成功 (A26: 登录成功不自动恢复交易)."""
        if self.phase == LifecyclePhase.INITIALIZING:
            # 保持 INITIALIZING，等待对账与自检通过后显式进入 READY
            pass

    def on_reconciliation_passed(self) -> None:
        """对账自检全部通过，进入就绪状态."""
        if self.phase in {LifecyclePhase.INITIALIZING, LifecyclePhase.CLOSED, LifecyclePhase.SETTLED}:
            self.phase = LifecyclePhase.READY

    def on_market_open(self) -> None:
        """开盘事件触发 (门禁检查)."""
        if self.phase == LifecyclePhase.READY:
            self.phase = LifecyclePhase.TRADING
        else:
            # 未处于 READY 状态（如半结算、对账未过），严格保持门禁，禁止进入 TRADING
            pass

    def on_market_close(self) -> None:
        """收盘事件触发."""
        if self.phase == LifecyclePhase.TRADING:
            self.phase = LifecyclePhase.SETTLEMENT_PENDING

    def on_semi_settlement_detected(self, reason: str = "") -> None:
        """检测到半结算异常 (持仓已切但资金未结转, F10, A26).

        立即转入 SEMI_SETTLED_HOLD，禁止新增风险，保护最后一致快照.
        """
        self.phase = LifecyclePhase.SEMI_SETTLED_HOLD
        self.semi_settled_warning = True

    def on_settlement_confirmed(self, settled_day: date) -> None:
        """官方结算单或结算数据确认到位."""
        if settled_day != self.current_trading_day:
            return
        self.is_settlement_complete = True
        self.phase = LifecyclePhase.SETTLED
        self.semi_settled_warning = False

    def advance_trading_day(self, new_trading_day: date) -> None:
        """推进到新交易日 (结算门禁生效)."""
        if new_trading_day <= self.current_trading_day:
            raise ValueError(f"new trading day ({new_trading_day}) must be > current ({self.current_trading_day})")

        # 若未完成结算就尝试推进交易日，保持门禁阻止直接交易
        self.current_trading_day = new_trading_day
        self.is_settlement_complete = False
        self.semi_settled_warning = False
        self.phase = LifecyclePhase.INITIALIZING

    def can_accept_new_risk(self) -> bool:
        """是否允许接受增加风险的新报单."""
        return self.phase == LifecyclePhase.TRADING and not self.semi_settled_warning
