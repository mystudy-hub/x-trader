"""[Engine 层] BacktestEngine 单品种/多品种事件驱动 Bar 回测引擎 (S3-02, FR-MATCH-01~05, FR-EXEC-01~03).

核心特性：
1. 确定性事件推进：通过 VirtualClock 与 Bar 时间戳严格分相推进（开盘撮合 -> 收盘行情分发 -> 策略决策）；
2. 彻底杜绝未来信息：策略在 bar.bar_end 时刻接收行情并决策，产生的订单最早在下一时点/下一 Bar 开盘撮合；
3. 严格集成 S2 交易内核：持仓预占、双盈亏事件账本、每日结算结转 (settle_day) 与四字段资金模型；
4. 产出结构化 BacktestResult 与逐 Bar 资产快照，供绩效分析与报告生成使用；
5. 架构纯洁性：仅依赖 Core 与 Domain 层，通过 ExecutionPort 与 JournalPort 注入外部适配器。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from qh_trader.core.constants import (
    EventKind,
    PositionSide,
)
from qh_trader.core.objects import Bar, InstrumentId, Trade
from qh_trader.core.ports import ExecutionPort, JournalPort, RuleStorePort
from qh_trader.domain.orders import Order
from qh_trader.engine.base_engine import BaseEngine


@dataclass(frozen=True, slots=True)
class EquitySnapshot:
    """逐 Bar 资产与持仓快照."""
    timestamp: datetime
    trading_day: date
    balance: Decimal
    total_equity: Decimal
    realized_mtm_pnl: Decimal
    realized_trade_pnl: Decimal
    total_commission: Decimal
    long_position: int
    short_position: int
    mark_price: Decimal


@dataclass(frozen=True)
class BacktestResult:
    """回测结果输出结构."""
    account_id: str
    initial_capital: Decimal
    final_equity: Decimal
    total_pnl: Decimal
    total_commission: Decimal
    total_trades: int
    equity_snapshots: tuple[EquitySnapshot, ...]
    trades: tuple[Trade, ...]
    orders: tuple[Order, ...]


class BacktestEngine(BaseEngine):
    """事件驱动 Bar 回测引擎."""

    def __init__(
        self,
        account_id: str = "backtest-account",
        *,
        gateway: ExecutionPort,
        start_time: datetime | None = None,
        initial_capital: Decimal = Decimal("1000000.00"),
        contract_multiplier: Decimal = Decimal("10"),
        commission_per_lot: Decimal = Decimal("5.0"),
        margin_ratio: Decimal = Decimal("0.1"),
        journal: JournalPort | None = None,
        rule_store: RuleStorePort | None = None,
    ) -> None:
        init_time = start_time or datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)

        super().__init__(
            account_id=account_id,
            start_time=init_time,
            initial_capital=initial_capital,
            trading_day=init_time.date(),
            controller_id="backtest-controller",
            gateway=gateway,
            journal=journal,
            rule_store=rule_store,
            contract_multiplier=contract_multiplier,
            commission_per_lot=commission_per_lot,
            margin_ratio=margin_ratio,
        )

        self._snapshots: list[EquitySnapshot] = []
        self._executed_trades: list[Trade] = []

    def process_trade_event(self, event) -> None:
        super().process_trade_event(event)
        self._executed_trades.append(event.payload)

    def run(
        self,
        bars: Sequence[Bar],
        *,
        price_limits: Mapping[InstrumentId | tuple[InstrumentId, date], tuple[Decimal, Decimal]] | None = None,
    ) -> BacktestResult:
        """执行完整 Bar 回测."""
        if not bars:
            raise ValueError("bars sequence cannot be empty")

        # 1. 确保按时间先后排序
        sorted_bars = sorted(bars, key=lambda b: (b.bar_start, b.open_time, b.bar_end))

        # 2. 策略启动
        for strat in self.strategies.values():
            strat.on_start()

        first_bar = sorted_bars[0]
        current_trading_day = first_bar.meta.trading_day
        self.ledger.current_trading_day = current_trading_day
        self.position_manager.current_trading_day = current_trading_day
        if hasattr(self.gateway, "set_trading_day"):
            self.gateway.set_trading_day(current_trading_day)

        last_bar: Bar | None = None

        # 3. 逐 Bar 事件流循环
        for bar in sorted_bars:
            bar_trading_day = bar.meta.trading_day

            # 跨日检测与日终结算结转
            if bar_trading_day > current_trading_day and last_bar is not None:
                self.ledger.settle_day(
                    {last_bar.instrument: last_bar.close},
                    new_trading_day=bar_trading_day,
                )
                current_trading_day = bar_trading_day
                if hasattr(self.gateway, "set_trading_day"):
                    self.gateway.set_trading_day(current_trading_day)

            # 阶段 A：时钟推进至开盘，并执行撮合 (针对在 bar.open_time 之前已存在的委托)
            self.clock.advance_to(bar.open_time)
            if hasattr(self.gateway, "match_bar"):
                upper_l, lower_l = None, None
                if price_limits is not None:
                    lim = price_limits.get((bar.instrument, bar_trading_day)) or price_limits.get(bar.instrument)
                    if lim is not None:
                        upper_l, lower_l = lim
                match_events = self.gateway.match_bar(bar, upper_limit=upper_l, lower_limit=lower_l)
                for evt in match_events:
                    if evt.kind == EventKind.TRADE_REPORT:
                        self.process_trade_event(evt)
                    elif evt.kind == EventKind.ORDER_REPORT:
                        self.process_order_event(evt)

            # 阶段 B：时钟推进至 Bar 结束时刻，收盘信息完整生成，向策略推送行情
            self.clock.advance_to(bar.bar_end)
            for strat in self.strategies.values():
                strat.on_bar(bar)

            # 阶段 C：计算当前时刻动态权益并记录快照
            mark_prices = {bar.instrument: bar.close}
            funds_state = self.ledger.get_funds_state(current_prices=mark_prices)
            tot_equity = funds_state.total_equity
            pos_l = self.position_manager.get_position(bar.instrument, PositionSide.LONG)
            pos_s = self.position_manager.get_position(bar.instrument, PositionSide.SHORT)

            snapshot = EquitySnapshot(
                timestamp=bar.bar_end,
                trading_day=bar_trading_day,
                balance=self.ledger.balance,
                total_equity=tot_equity,
                realized_mtm_pnl=self.ledger.realized_mtm_pnl,
                realized_trade_pnl=self.ledger.realized_trade_pnl,
                total_commission=self.ledger.total_commission,
                long_position=pos_l.total_position,
                short_position=pos_s.total_position,
                mark_price=bar.close,
            )
            self._snapshots.append(snapshot)
            last_bar = bar

        # 4. 回测结束日终结算
        if last_bar is not None and self.ledger.current_trading_day is not None:
            next_day = self.ledger.current_trading_day + timedelta(days=1)
            try:
                self.ledger.settle_day(
                    {last_bar.instrument: last_bar.close},
                    new_trading_day=next_day,
                )
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning("final backtest day settlement skipped: %s", exc)

        # 5. 策略停止
        for strat in self.strategies.values():
            strat.on_stop()

        final_equity = self._snapshots[-1].total_equity if self._snapshots else self.ledger.balance
        total_pnl = final_equity - self.ledger.initial_capital

        return BacktestResult(
            account_id=self.account_id,
            initial_capital=self.ledger.initial_capital,
            final_equity=final_equity,
            total_pnl=total_pnl,
            total_commission=self.ledger.total_commission,
            total_trades=len(self._executed_trades),
            equity_snapshots=tuple(self._snapshots),
            trades=tuple(self._executed_trades),
            orders=self.order_manager.orders(),
        )
