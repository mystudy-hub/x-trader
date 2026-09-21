"""[Engine 层] BacktestEngine 事件驱动 Bar 回测引擎 (S3-02, FR-MATCH-01~05, FR-EXEC-01~03, FR-CAL-04/05, FR-VAL-06).

每根 Bar 的推进分相：
  A. 推进虚拟时钟到 open_time（途中触发到期定时器，空行情时段同样推进）；
     对 open_time 前已生效的订单做开盘候选撮合，回报入内核；
  B. 推进到 bar_end；网关做 Bar 结束估算，回报入内核；随后才把本 Bar 推给策略 (策略看不到本 Bar 撮合前的信息)；
  C. 记录逐 Bar 权益快照。
交易日切换：按日历 (若提供) 或 Bar 的交易日标记；先让未成交订单过期并释放预占，再按结算价结算全部持仓，
缺任何持仓合约的结算价时明确失败，不静默跳过。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from qh_trader.core.constants import (
    ExecutionPolicy,
    MissedExecutionPolicy,
    MissingRuleError,
    OrderStatus,
    PositionSide,
    QualityFlag,
)
from qh_trader.core.event import CanonicalEvent
from qh_trader.core.objects import Bar, InstrumentId, Trade
from qh_trader.core.ports import ExecutionPort, JournalPort, RuleStorePort, SessionGatePort
from qh_trader.domain.ledger import ClosedTradeRecord, SettlementPendingError
from qh_trader.domain.limits import ExchangeLimits
from qh_trader.domain.orders import Order
from qh_trader.domain.risk import RiskManager
from qh_trader.domain.smart_router import SmartRouter
from qh_trader.engine.base_engine import (
    SIMULATED_EVENT_PRIORITIES,
    BaseEngine,
    InstrumentEconomics,
    MissedExecution,
    RejectedIntent,
)


@dataclass(frozen=True, slots=True)
class EquitySnapshot:
    """逐 Bar 资产与持仓快照 (含未平仓估值)."""

    timestamp: datetime
    trading_day: date
    balance: Decimal
    total_equity: Decimal
    margin_used: Decimal
    realized_mtm_pnl: Decimal
    realized_trade_pnl: Decimal
    total_commission: Decimal
    long_position: int
    short_position: int
    mark_price: Decimal


@dataclass(frozen=True, slots=True)
class DegradedBar:
    """质量标记命中拒绝集合的 Bar：不用于撮合，策略仍可见并被告知 (A16, FR-LED-09)."""

    instrument: InstrumentId
    trading_day: date
    bar_end: datetime
    flags: str


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
    closed_trades: tuple[ClosedTradeRecord, ...] = ()
    rejected_intents: tuple[RejectedIntent, ...] = ()
    missed_executions: tuple[MissedExecution, ...] = ()
    unfilled_orders: tuple[Order, ...] = ()
    ledger_entries: tuple[Any, ...] = ()
    events: tuple[CanonicalEvent, ...] = ()
    strategy_of_order: Mapping[str, str] = field(default_factory=dict)
    first_trading_day: date | None = None
    last_trading_day: date | None = None
    bar_count: int = 0
    bar_interval: str | None = None
    settlement_source: str = "bar_close"
    event_priorities: Mapping[str, int] = field(default_factory=dict)
    degraded_bars: tuple[Any, ...] = ()
    execution_degradations: tuple[Any, ...] = ()
    quality_flag_counts: Mapping[str, int] = field(default_factory=dict)

    def canonical_hashes(self) -> dict[str, str]:
        """订单 / 成交 / 账本的规范化哈希 (A15, FR-VAL-07)。同环境同输入重跑必须一致."""
        orders = [
            {
                "client_order_id": o.client_order_id,
                "strategy_id": o.strategy_id,
                "instrument": str(o.instrument),
                "side": o.side.value,
                "offset": o.offset.value,
                "quantity": o.quantity,
                "order_type": o.order_type.value,
                "limit_price_ticks": o.limit_price_ticks,
                "created_at": o.intent.created_at.isoformat(),
                "status": o.status.value,
                "send_state": o.send_state.value,
                "cum_filled_qty": o.cum_filled_qty,
            }
            for o in self.orders
        ]
        trades = [
            {
                "trade_id": t.trade_id,
                "instrument": str(t.instrument),
                "trading_day": t.trading_day.isoformat(),
                "side": t.side.value,
                "offset": t.offset.value,
                "quantity": t.quantity,
                "price": str(t.price),
                "event_time": t.event_time.isoformat(),
            }
            for t in self.trades
        ]
        ledger = [
            {
                "kind": str(getattr(e, "kind", "")),
                "amount": str(getattr(e, "amount", "")),
                "reference": str(getattr(e, "reference", "")),
                "trading_day": str(getattr(e, "trading_day", "")),
            }
            for e in self.ledger_entries
        ]
        equity = [
            {"t": s.timestamp.isoformat(), "balance": str(s.balance), "equity": str(s.total_equity)}
            for s in self.equity_snapshots
        ]

        def digest(value: Any) -> str:
            return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

        return {
            "orders": digest(orders),
            "trades": digest(trades),
            "ledger": digest(ledger),
            "equity_curve": digest(equity),
        }


class BacktestEngine(BaseEngine):
    """事件驱动 Bar 回测引擎."""

    def __init__(
        self,
        account_id: str = "backtest-account",
        *,
        gateway: ExecutionPort,
        start_time: datetime | None = None,
        initial_capital: Decimal = Decimal("1000000.00"),
        journal: JournalPort | None = None,
        rule_store: RuleStorePort | None = None,
        session_gate: SessionGatePort | None = None,
        smart_router: SmartRouter | None = None,
        risk_manager: RiskManager | None = None,
        exchange_limits: ExchangeLimits | None = None,
        natural_person: bool = False,
        default_economics: InstrumentEconomics | None = None,
        execution_policy: ExecutionPolicy = ExecutionPolicy.NEXT_BAR_OPEN,
        missed_execution: MissedExecutionPolicy = MissedExecutionPolicy.DEFER,
        fixed_time_before_close: timedelta | None = None,
        commission_profile: str = "default",
        trading_days: Sequence[date] | None = None,
        reject_quality_flags: QualityFlag = QualityFlag.MISSING | QualityFlag.INVALID | QualityFlag.STALE,
        strict_data_quality: bool = False,
    ) -> None:
        init_time = start_time or datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
        super().__init__(
            account_id=account_id,
            start_time=init_time,
            initial_capital=initial_capital,
            trading_day=None,
            controller_id="backtest-controller",
            gateway=gateway,
            journal=journal,
            rule_store=rule_store,
            session_gate=session_gate,
            smart_router=smart_router,
            risk_manager=risk_manager,
            exchange_limits=exchange_limits,
            natural_person=natural_person,
            default_economics=default_economics,
            execution_policy=execution_policy,
            missed_execution=missed_execution,
            fixed_time_before_close=fixed_time_before_close,
            commission_profile=commission_profile,
        )
        self._trading_days = tuple(sorted(trading_days)) if trading_days else None
        self._reject_quality_flags = reject_quality_flags
        self._strict_data_quality = strict_data_quality
        self._degraded_bars: list[DegradedBar] = []
        self._quality_counts: dict[str, int] = {}
        self._snapshots: list[EquitySnapshot] = []
        self._executed_trades: list[Trade] = []
        self._last_close: dict[InstrumentId, Decimal] = {}
        self._settlement_source = "bar_close"

    # ------------------------------------------------------------------ 回报钩子
    def process_trade_event(self, event: CanonicalEvent[Trade]) -> None:
        before = len(self.order_manager.deduplicator.snapshot())
        super().process_trade_event(event)
        if len(self.order_manager.deduplicator.snapshot()) > before:
            self._executed_trades.append(event.payload)

    # ------------------------------------------------------------------ 交易日
    def _set_trading_day(self, day: date) -> None:
        self.ledger.current_trading_day = day
        self.position_manager.current_trading_day = day
        self.risk_manager.reset_daily_counters(day)
        if self.gateway is not None and hasattr(self.gateway, "set_trading_day"):
            self.gateway.set_trading_day(day)

    def _roll_trading_day(
        self,
        settled_day: date,
        new_day: date,
        settlement_prices: Mapping[InstrumentId, Decimal],
        at: datetime,
    ) -> None:
        # 1. 当日有效订单过期并释放预占 (GFD)
        if self.gateway is not None and hasattr(self.gateway, "expire_orders"):
            self.dispatch_gateway_events(self.gateway.expire_orders(at))
        # 2. 结算：缺持仓合约结算价时明确失败 (FR-CAL-05, A06)
        try:
            self.ledger.settle_day(dict(settlement_prices), new_trading_day=new_day, trading_day=settled_day)
        except SettlementPendingError as exc:
            raise MissingRuleError(
                f"settlement price missing for {[str(i) for i in exc.pending]} on {settled_day}; "
                "supply settlement records or bar closes for every held contract"
            ) from exc
        self._set_trading_day(new_day)

    def _settlement_prices(self, day: date, settlements: Mapping[tuple[InstrumentId, date], Decimal] | None) -> dict:
        prices: dict[InstrumentId, Decimal] = dict(self._last_close)
        if settlements:
            for (inst, sday), price in settlements.items():
                if sday == day:
                    prices[inst] = price
        return prices

    # ------------------------------------------------------------------ 主循环
    def run(
        self,
        bars: Sequence[Bar],
        *,
        price_limits: Mapping[InstrumentId | tuple[InstrumentId, date], tuple[Decimal, Decimal]] | None = None,
        settlement_prices: Mapping[tuple[InstrumentId, date], Decimal] | None = None,
        on_bar_processed: Any = None,
    ) -> BacktestResult:
        """执行完整 Bar 回测。settlement_prices 缺省时用当日最后一根 Bar 的收盘价并在结果中标注."""
        if not bars:
            raise ValueError("bars sequence cannot be empty")
        if settlement_prices:
            self._settlement_source = "official_settlement"

        sorted_bars = sorted(bars, key=lambda b: (b.open_time, b.bar_end, str(b.instrument)))
        for bar in sorted_bars:
            if not isinstance(bar.instrument, InstrumentId):
                raise TypeError("backtest bars must belong to actual contracts, not derived series")
            self.economics(bar.instrument)  # 缺经济参数时提前失败
            self._check_calendar_consistency(bar)
            self._classify_quality(bar)
        intervals = {b.interval for b in sorted_bars}

        for strat in self.strategies.values():
            strat.on_start()

        current_day = sorted_bars[0].meta.trading_day
        self._set_trading_day(current_day)
        last_end = sorted_bars[0].bar_start

        # 时间线：每根 Bar 产生开盘与收盘两个事件。同一时刻先处理所有收盘 (行情可见、策略决策)，
        # 再处理所有开盘 (撮合)，保证 Bar i 收盘产生的订单只能在 Bar i+1 开盘之后才可能成交。
        end_phase, open_phase = 0, 1
        timeline: list[tuple[datetime, int, int, Bar]] = []
        for index, bar in enumerate(sorted_bars):
            timeline.append((bar.open_time, open_phase, index, bar))
            timeline.append((bar.bar_end, end_phase, index, bar))
        timeline.sort(key=lambda item: (item[0], item[1], item[2]))
        open_times = sorted({bar.open_time for bar in sorted_bars})

        def next_open_after(at: datetime) -> datetime | None:
            """不早于 at 的下一根 Bar 开盘时刻 (含恰在 at 开盘的 Bar：同一瞬间先收盘后开盘)."""
            for candidate in open_times:
                if candidate >= at:
                    return candidate
            return None

        for at, phase, _, bar in timeline:
            bar_day = bar.meta.trading_day
            if phase == open_phase:
                if bar_day > current_day:
                    # 先在旧交易日末尾过期未成交订单并结算，再推进到新日 (新日定时器随后触发)
                    self._roll_trading_day(
                        current_day, bar_day, self._settlement_prices(current_day, settlement_prices), last_end
                    )
                    current_day = bar_day
                elif bar_day < current_day:
                    raise ValueError(f"bars are not in trading-day order: {bar_day} after {current_day}")

                limits = self._limits_for(bar, price_limits)
                if limits is not None:
                    self.set_price_limits(bar.instrument, limits[0], limits[1])
                else:
                    self.clear_price_limits(bar.instrument)

                # 阶段 A：开盘候选撮合 (质量标记命中拒绝集合的 Bar 不撮合，也不推断路径)
                self.advance_clock(at, next_bar_open=at)
                if self._is_degraded(bar):
                    continue
                if self.gateway is not None and hasattr(self.gateway, "match_bar"):
                    upper, lower = limits if limits is not None else (None, None)
                    self.dispatch_gateway_events(self.gateway.match_bar(bar, upper_limit=upper, lower_limit=lower))
                continue

            # 阶段 B：Bar 结束，行情可见
            self.advance_clock(at, next_bar_open=next_open_after(at))
            self.dispatch_gateway_events()
            self._last_close[bar.instrument] = bar.close
            self.set_mark_price(bar.instrument, bar.close)
            for strat in self.strategies.values():
                strat.on_bar(bar)
            self.dispatch_gateway_events()

            # 阶段 C：快照
            self._snapshots.append(self._snapshot(bar, current_day))
            last_end = max(last_end, bar.bar_end)
            if on_bar_processed is not None:
                on_bar_processed(bar, self._snapshots[-1])

        # 收尾：未发出的延后意图撤销、最后一日挂单过期、最后一次结算，权益口径与途中一致
        last = sorted_bars[-1]
        end_day = self._next_day(current_day)
        self.advance_clock(last.bar_end, next_bar_open=None)
        self.cancel_deferred_intents("backtest ended before the deferred execution time")
        self._roll_trading_day(
            current_day, end_day, self._settlement_prices(current_day, settlement_prices), last.bar_end
        )

        for strat in self.strategies.values():
            strat.on_stop()

        funds = self.ledger.get_funds_state(current_prices=dict(self._last_close), margin_rates=self._margin_rates())
        final_equity = funds.total_equity
        closed = tuple(
            record
            for inst in sorted(self.ledger._instrument_ledgers, key=str)  # noqa: SLF001
            for record in self.ledger._instrument_ledgers[inst].closed_records  # noqa: SLF001
        )
        orders = self.order_manager.orders()
        return BacktestResult(
            account_id=self.account_id,
            initial_capital=self.ledger.initial_capital,
            final_equity=final_equity,
            total_pnl=final_equity - self.ledger.initial_capital,
            total_commission=self.ledger.total_commission,
            total_trades=len(self._executed_trades),
            equity_snapshots=tuple(self._snapshots),
            trades=tuple(self._executed_trades),
            orders=orders,
            closed_trades=closed,
            rejected_intents=tuple(self.rejected_intents),
            missed_executions=tuple(self.missed_executions),
            unfilled_orders=tuple(
                o
                for o in orders
                if o.status in (OrderStatus.EXPIRED, OrderStatus.CANCELLED) and o.cum_filled_qty < o.quantity
            ),
            ledger_entries=tuple(self.ledger.entries),
            events=tuple(self.processed_events),
            strategy_of_order={o.client_order_id: self.strategy_of(o.client_order_id) for o in orders},
            first_trading_day=sorted_bars[0].meta.trading_day,
            last_trading_day=current_day,
            bar_count=len(sorted_bars),
            bar_interval=intervals.pop() if len(intervals) == 1 else None,
            settlement_source=self._settlement_source,
            event_priorities={k.value: v for k, v in SIMULATED_EVENT_PRIORITIES.items()},
            degraded_bars=tuple(self._degraded_bars),
            execution_degradations=tuple(getattr(self.gateway, "degradations", ())),
            quality_flag_counts=dict(self._quality_counts),
        )

    # ------------------------------------------------------------------ 数据质量与日历一致性
    def _classify_quality(self, bar: Bar) -> None:
        flags = bar.meta.quality_flags
        label = flags.name if flags.name is not None else str(int(flags))
        self._quality_counts[label] = self._quality_counts.get(label, 0) + 1
        if flags & self._reject_quality_flags:
            if self._strict_data_quality:
                raise MissingRuleError(
                    f"bar {bar.instrument} ending {bar.bar_end.isoformat()} carries rejected quality flags {label}"
                )
            self._degraded_bars.append(DegradedBar(bar.instrument, bar.meta.trading_day, bar.bar_end, label))

    def _is_degraded(self, bar: Bar) -> bool:
        return bool(bar.meta.quality_flags & self._reject_quality_flags)

    def _check_calendar_consistency(self, bar: Bar) -> None:
        """A05：Bar 的交易日与时段必须与版本化日历一致，不能按自然日推断."""
        if self._trading_days is not None and bar.meta.trading_day not in self._trading_days:
            raise MissingRuleError(
                f"bar {bar.instrument} claims trading day {bar.meta.trading_day} which the calendar does not list"
            )
        if self.session_gate is None:
            return
        listed = self.session_gate.trading_day_at(bar.instrument, bar.open_time)
        if listed is not None and listed != bar.meta.trading_day:
            raise MissingRuleError(
                f"bar {bar.instrument} opening {bar.open_time.isoformat()} belongs to calendar trading day "
                f"{listed}, but the record says {bar.meta.trading_day}"
            )

    # ------------------------------------------------------------------ 辅助
    def _next_day(self, day: date) -> date:
        if self._trading_days:
            later = [d for d in self._trading_days if d > day]
            if later:
                return later[0]
        from datetime import timedelta

        return day + timedelta(days=1)

    @staticmethod
    def _limits_for(bar: Bar, price_limits: Mapping | None) -> tuple[Decimal, Decimal] | None:
        if price_limits is None:
            return None
        found = price_limits.get((bar.instrument, bar.meta.trading_day))
        if found is None:
            found = price_limits.get(bar.instrument)
        return found

    def _snapshot(self, bar: Bar, day: date) -> EquitySnapshot:
        funds = self.ledger.get_funds_state(current_prices=dict(self._last_close), margin_rates=self._margin_rates())
        pos_l = self.position_manager.get_position(bar.instrument, PositionSide.LONG)
        pos_s = self.position_manager.get_position(bar.instrument, PositionSide.SHORT)
        return EquitySnapshot(
            timestamp=bar.bar_end,
            trading_day=day,
            balance=self.ledger.balance,
            total_equity=funds.total_equity,
            margin_used=funds.margin_used,
            realized_mtm_pnl=self.ledger.realized_mtm_pnl,
            realized_trade_pnl=self.ledger.realized_trade_pnl,
            total_commission=self.ledger.total_commission,
            long_position=pos_l.total_position,
            short_position=pos_s.total_position,
            mark_price=bar.close,
        )
