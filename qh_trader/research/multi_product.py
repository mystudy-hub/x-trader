"""[Research 层] 多品种组合回测、样本外评价与移仓归因 (S4-06, FR-VAL-01/02/03, FR-CON-07).

设计要点：
1. **信号在连续序列上，成交在实际主力合约上**：每个品种用当时可见的主力映射拼接连续序列
   计算双均线信号，但委托只发给当日主力实际合约，绝不把派生序列当作成交标的 (FR-CON-01/03)。
2. **移仓真实执行**：主力切换且有持仓时，`RollManager` 生成两腿委托 (先平后开)，
   由策略提交给引擎并按实际成交推进；两腿手续费逐笔入账，合约切换价差本身不产生账本现金盈亏。
3. **样本外**：按时间切分拟合窗与保留窗，拟合窗只用于信号参数选择，保留窗不参与调参 (FR-VAL-01)。
4. **归因**：分品种成交贡献、移仓价差归因与费用分解写入报告；价差归因是信息项，不重复计入收益。

研究模式声明：经济参数来自 `product_registry` 的研究假设；结算价使用当日收盘价
(`BAR_CLOSE_ASSUMPTION`)，不是官方结算价；成交额缺失时以显式质量标记处理。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from qh_trader.analysis.performance import PerformanceMetrics, calculate_performance
from qh_trader.core.constants import ExecutionPolicy, Offset, OrderStatus, PositionSide
from qh_trader.core.objects import Bar, InstrumentId, Trade
from qh_trader.data.calendar import TradingCalendar, project_product_calendar
from qh_trader.data.continuous import AdjustmentMethod, ContinuousBar, ContinuousSeriesBuilder
from qh_trader.data.dominant_contract import DominantContractResolver, build_dominant_mappings
from qh_trader.data.product_registry import ProductSpec, get_product_spec
from qh_trader.data.session_gate import CalendarSessionGate
from qh_trader.data.storage import ParquetDataStorage, normalize_interval
from qh_trader.domain.rollover import LegOrderPolicy, RollManager, RollState, RollTask
from qh_trader.engine.backtest_engine import BacktestEngine, BacktestResult
from qh_trader.engine.base_engine import InstrumentEconomics
from qh_trader.gateway.simulated_gateway import SimulatedGateway
from qh_trader.strategy.base import StrategyBase

# 结算价来源标记：研究模式用当日收盘价，不是官方结算价 (FR-LED-09/FR-VAL-03)。
BAR_CLOSE_SETTLEMENT = "BAR_CLOSE_ASSUMPTION"

# S4-05 生成的紧凑时段模板；运行时按品种投影到本次交易的合约集合。
DEFAULT_SESSION_TEMPLATE = Path("config/sessions_s4_2024v1.json")


@dataclass(frozen=True, slots=True)
class ProductDataset:
    """一个品种的实际合约行情、主力映射与连续序列."""

    spec: ProductSpec
    contracts: tuple[InstrumentId, ...]
    bars: tuple[Bar, ...]
    resolver: DominantContractResolver
    continuous: tuple[ContinuousBar, ...]
    continuous_close_by_day: Mapping[date, Decimal]
    raw_close_by_key: Mapping[tuple[InstrumentId, date], Decimal]
    first_day: date
    last_day: date

    def dominant_on(self, day: date, *, at: datetime | None = None) -> InstrumentId:
        reference = at if at is not None else _day_decision_time(day)
        return self.resolver.dominant(self.spec.product_id, reference).value


def _day_decision_time(day: date) -> datetime:
    """日盘结束后的决策时刻 (15:00 Asia/Shanghai)，用于查询当时可见的主力."""
    from zoneinfo import ZoneInfo

    return datetime(day.year, day.month, day.day, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def load_product_dataset(
    product: str,
    *,
    storage: ParquetDataStorage,
    interval: str = "1d",
    adjustment: AdjustmentMethod = AdjustmentMethod.DIFF,
) -> ProductDataset:
    """读取一个品种的全部实际合约 Bar，构建主力映射与无跳空连续序列."""
    spec = get_product_spec(product)
    snapshot = storage.capture_snapshot()
    interval = normalize_interval(interval)
    symbols = sorted(
        {
            str(entry["instrument"]).split(".", 1)[1]
            for entry in snapshot.datasets.values()
            if entry.get("kind") == "bar"
            and entry.get("interval") == interval
            and str(entry.get("instrument", "")).startswith(f"{spec.exchange.value}.")
        }
    )
    metric = "".join(ch for ch in spec.product if ch.isalpha())
    contracts: list[InstrumentId] = []
    bars_by_instrument: dict[InstrumentId, tuple[Bar, ...]] = {}
    for symbol in symbols:
        # 只接受 4 位交割月的实际合约；主连/连续序列 (如 rb0) 绝不能作为成交标的 (FR-CON-01)
        if not re.fullmatch(r"[A-Za-z]{1,3}\d{4}", symbol):
            continue
        if "".join(ch for ch in symbol if ch.isalpha()).casefold() != metric.casefold():
            continue
        instrument = InstrumentId(spec.exchange, symbol)
        bars = tuple(storage.read_bars(instrument, interval, snapshot=snapshot))
        if not bars:
            continue
        contracts.append(instrument)
        bars_by_instrument[instrument] = bars
    if not contracts:
        raise ValueError(f"no ingested actual-contract bars for product {product}")

    all_bars = tuple(
        sorted(
            (bar for seq in bars_by_instrument.values() for bar in seq),
            key=lambda bar: (bar.bar_end, str(bar.instrument)),
        )
    )
    resolver = build_dominant_mappings(spec.product_id, all_bars, confirm_days=2)
    builder = ContinuousSeriesBuilder(resolver, method=adjustment)
    continuous = tuple(builder.build_series(bars_by_instrument))

    raw_close: dict[tuple[InstrumentId, date], Decimal] = {}
    for instrument, seq in bars_by_instrument.items():
        for bar in seq:
            raw_close[(instrument, bar.meta.trading_day)] = bar.close

    continuous_close = {item.raw_bar.meta.trading_day: item.adjusted_close for item in continuous}
    days = sorted({bar.meta.trading_day for bar in all_bars})
    return ProductDataset(
        spec=spec,
        contracts=tuple(sorted(contracts, key=str)),
        bars=all_bars,
        resolver=resolver,
        continuous=continuous,
        continuous_close_by_day=continuous_close,
        raw_close_by_key=raw_close,
        first_day=days[0],
        last_day=days[-1],
    )


@dataclass(frozen=True, slots=True)
class RollRecord:
    """一次完成的移仓事实，用于归因 (不重复计入账本现金盈亏)."""

    product: str
    from_instrument: InstrumentId
    to_instrument: InstrumentId
    side: PositionSide
    quantity: int
    from_price: Decimal
    to_price: Decimal
    multiplier: Decimal
    commission: Decimal
    start_leg1: datetime | None
    completed_at: datetime | None

    @property
    def spread_cost(self) -> Decimal:
        """移仓价差成本 (正数表示对多头/空头均为成本方向的绝对值)."""
        if self.side == PositionSide.LONG:
            return (self.to_price - self.from_price) * Decimal(self.quantity) * self.multiplier
        return (self.from_price - self.to_price) * Decimal(self.quantity) * self.multiplier

    @property
    def exposure_calendar_days(self) -> int:
        """两腿之间的自然日数：先平后开时不可消除的暴露窗口，逐笔披露."""
        if self.start_leg1 is None or self.completed_at is None:
            return 0
        return max(0, (self.completed_at.date() - self.start_leg1.date()).days)


class RollAwareTrendStrategy(StrategyBase):
    """在连续序列上产生双均线信号，在实际主力合约上成交，并真实执行移仓."""

    def __init__(
        self,
        *,
        strategy_id: str,
        context,
        dataset: ProductDataset,
        fast_window: int,
        slow_window: int,
        order_size: int,
        account_id: str,
        policy: LegOrderPolicy = LegOrderPolicy.CLOSE_FIRST,
    ) -> None:
        super().__init__(strategy_id, context)
        if fast_window <= 0 or slow_window <= 0 or fast_window >= slow_window:
            raise ValueError("fast_window must be positive and smaller than slow_window")
        if order_size <= 0:
            raise ValueError("order_size must be positive")
        self.dataset = dataset
        self.fast_window = fast_window
        self.slow_window = slow_window
        self.order_size = order_size
        self.roll_manager = RollManager(account_id)
        self.policy = policy
        self._closes: list[Decimal] = []
        self._last_day: date | None = None
        self._target = 0
        self._held_contract: InstrumentId | None = None
        self._roll_task: RollTask | None = None
        self._roll_started_at: datetime | None = None
        self.roll_records: list[RollRecord] = []
        self._inflight: set[InstrumentId] = set()

    # ------------------------------------------------------------------ 策略回调
    def on_bar(self, bar: Bar) -> None:
        day = bar.meta.trading_day
        # 连续序列信号：同一交易日只记录一次，且只用当日可见的连续收盘价 (无未来信息)
        if day != self._last_day and day in self.dataset.continuous_close_by_day:
            self._closes.append(self.dataset.continuous_close_by_day[day])
            self._last_day = day

        dominant = self.dataset.dominant_on(day, at=bar.bar_end)
        if self._roll_task is not None:
            return  # 两腿未完成前不做任何新信号交易

        held = self._held_contract
        position = self._position_on(held) if held is not None else 0
        if held is not None and held != dominant:
            if position != 0:
                # 主力切换且仍有持仓：真实移仓 (先平旧合约，再开新合约)
                self._start_roll(held, dominant, position, bar.bar_end)
                return
            self._held_contract = dominant

        signal = self._signal()
        if signal is None:
            return
        self._target = signal * self.order_size
        self._held_contract = dominant
        self._apply_target(dominant)

    def on_order(self, order) -> None:
        # 引擎可能把 CLOSE 改写成 CLOSE_YESTERDAY 或拆成子单，父单号不再出现在回报里；
        # 因此按合约跟踪在途委托，且只在终态释放，避免同一 Bar 内重复下单。
        if order.status in (
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        ):
            self._inflight.discard(order.instrument)

    def on_trade(self, trade: Trade) -> None:
        identity = trade.order_identity
        client_order_id = identity.client_order_id if identity else None
        if not client_order_id:
            return
        self._inflight.discard(trade.instrument)
        task = self._roll_task
        if task is not None and not task.is_done:
            # 按合约归属判定腿序：引擎改写平今/平昨或拆子单后父单号不再可靠
            if trade.instrument == task.from_instrument:
                self.roll_manager.on_fill(task, trade, leg=1)
            elif trade.instrument == task.to_instrument:
                self.roll_manager.on_fill(task, trade, leg=2)
            else:
                return
            if task.state == RollState.COMPLETED:
                self._finish_roll(task, trade)
            else:
                # 第一腿成交后立即送出第二腿，避免两腿之间出现无保护暴露
                self._advance_roll(trade.event_time)
            return
        client_task = self.roll_manager.task_for_order(client_order_id) if client_order_id else None
        if client_task is None:
            return
        self.roll_manager.on_trade(trade, client_order_id)

    # ------------------------------------------------------------------ 内部
    def _position_on(self, instrument: InstrumentId) -> int:
        return int(self.context.get_position(instrument))

    def _signal(self) -> int | None:
        if len(self._closes) < self.slow_window:
            return None
        fast = sum(self._closes[-self.fast_window :]) / Decimal(self.fast_window)
        slow = sum(self._closes[-self.slow_window :]) / Decimal(self.slow_window)
        if fast > slow:
            return 1
        if fast < slow:
            return -1
        return 0

    def _apply_target(self, instrument: InstrumentId) -> None:
        """按目标净持仓调整当日主力合约，反手拆成先平后开两步 (同一时刻不发出必然被拒的平仓单)."""
        if self._inflight:
            return
        current = self._position_on(instrument)
        target = self._target
        if current == target:
            return
        if current == 0:
            side = self.buy if target > 0 else self.sell
            self._submit(side, instrument, abs(target), Offset.OPEN)
        elif current > 0:
            if target >= 0:
                if target > current:
                    self._submit(self.buy, instrument, target - current, Offset.OPEN)
                else:
                    self._submit(self.sell, instrument, current - target, Offset.CLOSE)
            else:
                self._submit(self.sell, instrument, current, Offset.CLOSE)
        else:
            if target <= 0:
                if target < current:
                    self._submit(self.sell, instrument, current - target, Offset.OPEN)
                else:
                    self._submit(self.buy, instrument, current - target, Offset.CLOSE)
            else:
                self._submit(self.buy, instrument, -current, Offset.CLOSE)

    def _submit(self, action, instrument: InstrumentId, quantity: int, offset: Offset) -> None:
        if quantity <= 0:
            return
        action(instrument, quantity, offset)
        self._inflight.add(instrument)

    def _start_roll(
        self, from_instrument: InstrumentId, to_instrument: InstrumentId, position: int, now: datetime
    ) -> None:
        side = PositionSide.LONG if position > 0 else PositionSide.SHORT
        self._roll_started_at = now
        self._roll_task = self.roll_manager.create_roll_task(
            self.dataset.spec.product_id,
            from_instrument,
            to_instrument,
            side,
            abs(position),
            batch_size=abs(position),
            policy=self.policy,
        )
        self._advance_roll(now)

    def _advance_roll(self, now: datetime) -> None:
        task = self._roll_task
        if task is None:
            return
        intent = self.roll_manager.plan_next_order(task, now)
        if intent is None:
            return
        leg = 1 if intent.instrument == task.from_instrument else 2
        client_order_id = self.context.send_order(
            intent.instrument,
            intent.side,
            intent.offset,
            intent.quantity,
            intent.order_type,
            strategy_id=self.strategy_id,
        )
        self.roll_manager.bind_submitted_order(task, leg, client_order_id)
        self._inflight.add(intent.instrument)

    def _finish_roll(self, task: RollTask, trade: Trade) -> None:
        self.roll_records.append(
            RollRecord(
                product=self.dataset.spec.product,
                from_instrument=task.from_instrument,
                to_instrument=task.to_instrument,
                side=task.position_side,
                quantity=task.total_quantity,
                from_price=task.leg1_avg_price,
                to_price=task.leg2_avg_price,
                multiplier=self.dataset.spec.multiplier,
                commission=task.total_commission,
                start_leg1=self._roll_started_at,
                completed_at=trade.event_time,
            )
        )
        self._held_contract = task.to_instrument
        self._target = task.total_quantity if task.position_side == PositionSide.LONG else -task.total_quantity
        self._roll_task = None
        self._roll_started_at = None


@dataclass(frozen=True, slots=True)
class ProductRun:
    spec: ProductSpec
    dataset: ProductDataset
    result: BacktestResult
    metrics: PerformanceMetrics
    roll_records: tuple[RollRecord, ...]


@dataclass(frozen=True, slots=True)
class SliceMetrics:
    """组合权益切片指标 (样本内/外评价，口径与单品种一致)."""

    label: str
    start: date | None
    end: date | None
    initial_capital: Decimal
    final_equity: Decimal
    total_return: Decimal
    annualized_return: Decimal
    max_drawdown_percent: Decimal
    sharpe_ratio: Decimal
    trading_days: int


@dataclass(frozen=True, slots=True)
class MultiProductRun:
    products: Mapping[str, ProductRun]
    portfolio_equity: tuple[tuple[date, Decimal], ...]
    sample_start: date
    sample_end: date
    train_end: date
    initial_capital: Decimal = Decimal(0)


def slice_metrics(
    run: MultiProductRun,
    *,
    start: date | None,
    end: date | None,
    label: str,
    annual_trading_days: int = 242,
    rf_rate: Decimal = Decimal("0.02"),
) -> SliceMetrics:
    """在组合权益上按时间切片计算指标，不重算成交成本 (FR-VAL-01)."""
    import math

    equity = [
        (day, value)
        for day, value in run.portfolio_equity
        if (start is None or day >= start) and (end is None or day < end)
    ]
    if not equity or run.initial_capital <= 0:
        return SliceMetrics(
            label,
            start,
            end,
            run.initial_capital,
            run.initial_capital,
            Decimal(0),
            Decimal(0),
            Decimal(0),
            Decimal(0),
            0,
        )
    first_day, first_equity = equity[0]
    last_equity = equity[-1][1]
    # 切片期初资金用切片首日期末权益，避免跨窗累计盈亏被重复计入
    capital = first_equity
    total_ret = (last_equity - capital) / capital if capital > 0 else Decimal(0)
    days = len(equity)
    years = Decimal(days) / Decimal(annual_trading_days)
    ann_return = (
        (Decimal(1) + total_ret) ** (Decimal(1) / years) - Decimal(1) if years > 0 and total_ret > -1 else total_ret
    )
    series = [float(v) for _, v in equity]
    returns = [(series[i] - series[i - 1]) / series[i - 1] for i in range(1, len(series)) if series[i - 1] > 0]
    if len(returns) > 1:
        mean_r = sum(returns) / len(returns)
        var = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        ann_vol = math.sqrt(var * annual_trading_days)
    else:
        ann_vol = 0.0
    sharpe = (float(ann_return) - float(rf_rate)) / ann_vol if ann_vol > 1e-12 else 0.0
    peak = series[0]
    max_dd = 0.0
    for value in series:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)
    return SliceMetrics(
        label=label,
        start=first_day if start is not None else None,
        end=equity[-1][0],
        initial_capital=capital,
        final_equity=last_equity,
        total_return=Decimal(str(total_ret)).quantize(Decimal("0.000001")),
        annualized_return=Decimal(str(ann_return)).quantize(Decimal("0.000001")),
        max_drawdown_percent=Decimal(str(max_dd)).quantize(Decimal("0.000001")),
        sharpe_ratio=Decimal(str(sharpe)).quantize(Decimal("0.0001")),
        trading_days=days,
    )


def run_product(
    dataset: ProductDataset,
    *,
    account_id: str,
    initial_capital: Decimal,
    fast_window: int,
    slow_window: int,
    order_size: int,
    bar_range: tuple[date, date] | None = None,
    calendar: TradingCalendar | None = None,
    execution_policy: ExecutionPolicy = ExecutionPolicy.NEXT_BAR_OPEN,
) -> ProductRun:
    """单品种运行：实际主力合约成交 + 真实两腿移仓.

    给定版本化日历时，委托必须落在日历允许的报单时段；日线策略在收盘后产生的意图
    会被持有到下一个可报单时段，而不是在当日 GFD 到期前从未获得成交机会就被过期 (A05/A21)。
    """
    bars = dataset.bars
    if calendar is not None:
        bars = tuple(bar for bar in bars if bar.meta.trading_day in calendar.trading_days)
    if bar_range is not None:
        start, end = bar_range
        bars = tuple(bar for bar in bars if start <= bar.meta.trading_day <= end)
    if not bars:
        raise ValueError(f"no bars for {dataset.spec.product} in requested window")

    session_gate = CalendarSessionGate(calendar) if calendar is not None else None
    gateway = SimulatedGateway(
        account_id=account_id,
        trading_day=bars[0].meta.trading_day,
        slippage_ticks=0,
        price_tick=dataset.spec.price_tick,
        participation_rate=Decimal("1.0"),
        session_gate=session_gate,
    )
    engine = BacktestEngine(
        account_id=account_id,
        gateway=gateway,
        start_time=bars[0].bar_start,
        initial_capital=initial_capital,
        execution_policy=execution_policy,
        session_gate=session_gate,
        trading_days=tuple(sorted(calendar.trading_days)) if calendar is not None else None,
    )
    seen: set[InstrumentId] = set()
    for bar in bars:
        if bar.instrument in seen:
            continue
        seen.add(bar.instrument)
        engine.register_instrument(
            bar.instrument,
            InstrumentEconomics(
                multiplier=dataset.spec.multiplier,
                price_tick=dataset.spec.price_tick,
                commission_per_lot=dataset.spec.commission_per_lot,
                margin_ratio=dataset.spec.margin_ratio,
                source=f"product_registry:{dataset.spec.source}",
            ),
        )
    strategy = RollAwareTrendStrategy(
        strategy_id=f"roll-trend-{dataset.spec.product}",
        context=engine,
        dataset=dataset,
        fast_window=fast_window,
        slow_window=slow_window,
        order_size=order_size,
        account_id=account_id,
    )
    engine.add_strategy(strategy)
    result = engine.run(bars)
    roll_records = tuple(
        replace(record, commission=_roll_commission(record, result, dataset.spec))
        for record in strategy.roll_records
    )
    metrics = calculate_performance(result, annual_trading_days=242)
    metrics = _with_roll_attribution(metrics, roll_records)
    return ProductRun(
        spec=dataset.spec, dataset=dataset, result=result, metrics=metrics, roll_records=roll_records
    )


def _roll_commission(record: RollRecord, result: BacktestResult, spec: ProductSpec) -> Decimal:
    """移仓两腿的实际手续费：从账本成交事实里取窗口内的两腿成交，不重算成交价."""
    if record.start_leg1 is None or record.completed_at is None:
        return Decimal(0)
    legs = {record.from_instrument, record.to_instrument}
    total = Decimal(0)
    for trade in result.trades:
        if trade.instrument not in legs:
            continue
        if record.start_leg1 <= trade.event_time <= record.completed_at:
            total += spec.commission_per_lot * Decimal(trade.quantity)
    return total


def _with_roll_attribution(metrics: PerformanceMetrics, records: Sequence[RollRecord]) -> PerformanceMetrics:
    """把移仓价差作为独立归因项附加到指标 (价差不进入账本现金盈亏，避免重复计收益)."""
    spread = sum((record.spread_cost for record in records), Decimal(0))
    return replace(metrics, rollover_spread_pnl=spread)


def run_portfolio(
    products: Sequence[str],
    *,
    storage: ParquetDataStorage,
    account_prefix: str = "s4",
    initial_capital_per_product: Decimal = Decimal("400000"),
    fast_window: int = 5,
    slow_window: int = 20,
    order_size: int = 1,
    train_ratio: Decimal = Decimal("0.6"),
    interval: str = "1d",
    adjustment: AdjustmentMethod = AdjustmentMethod.DIFF,
    session_template: Path | str = DEFAULT_SESSION_TEMPLATE,
    window: tuple[date, date] | None = None,
) -> MultiProductRun:
    """多品种组合运行：分品种回测 + 组合权益 + 样本内外评价.

    默认只用各品种共同覆盖的交易日，并投影版本化日历；日历缺失的品种显式失败，
    绝不静默退回到“无时段约束”的工程样例模式 (FR-RULE-05)。
    """
    datasets = {
        product: load_product_dataset(product, storage=storage, interval=interval, adjustment=adjustment)
        for product in products
    }
    if window is None:
        window = (
            max(dataset.first_day for dataset in datasets.values()),
            min(dataset.last_day for dataset in datasets.values()),
        )
    sample_start, sample_end = window
    span_days = (sample_end - sample_start).days
    train_end = sample_start + timedelta(days=int(span_days * float(train_ratio)))

    runs: dict[str, ProductRun] = {}
    for product, dataset in datasets.items():
        calendar = project_product_calendar(
            session_template, dataset.spec.product, dataset.contracts, window=window
        )
        runs[product] = run_product(
            dataset,
            account_id=f"{account_prefix}-{product}",
            initial_capital=initial_capital_per_product,
            fast_window=fast_window,
            slow_window=slow_window,
            order_size=order_size,
            bar_range=window,
            calendar=calendar,
        )

    # 组合权益：各品种按交易日取当日最后一条快照并前向填充；品种尚未开始的交易日计入其分配本金，
    # 否则组合曲线会因品种入场时点不同而出现虚假跳空。
    per_product: dict[str, list[tuple[date, Decimal]]] = {}
    for product, product_run in runs.items():
        last_by_day: dict[date, Decimal] = {}
        for snapshot in product_run.result.equity_snapshots:
            last_by_day[snapshot.trading_day] = snapshot.total_equity
        per_product[product] = sorted(last_by_day.items())
    all_days = sorted({day for seq in per_product.values() for day, _ in seq})
    portfolio_equity: list[tuple[date, Decimal]] = []
    for day in all_days:
        total = Decimal(0)
        for seq in per_product.values():
            value = initial_capital_per_product
            for seq_day, seq_value in seq:
                if seq_day <= day:
                    value = seq_value
                else:
                    break
            total += value
        portfolio_equity.append((day, total))

    return MultiProductRun(
        products=runs,
        portfolio_equity=tuple(portfolio_equity),
        sample_start=sample_start,
        sample_end=sample_end,
        train_end=train_end,
        initial_capital=initial_capital_per_product * len(runs),
    )


def split_metrics(run: MultiProductRun) -> tuple[dict[str, SliceMetrics], dict[str, SliceMetrics]]:
    """按 train_end 把组合权益切成拟合窗与保留窗 (保留窗不参与参数选择)."""
    in_sample = {
        "portfolio": slice_metrics(run, start=run.sample_start, end=run.train_end, label="in_sample")
    }
    out_of_sample = {"portfolio": slice_metrics(run, start=run.train_end, end=None, label="out_of_sample")}
    return in_sample, out_of_sample


def product_metrics(run: MultiProductRun) -> dict[str, PerformanceMetrics]:
    return {product: product_run.metrics for product, product_run in run.products.items()}


def roll_attribution(run: MultiProductRun) -> dict[str, dict[str, Decimal | int]]:
    """分品种移仓归因：次数、价差成本与两腿手续费；价差不计入收益."""
    summary: dict[str, dict[str, Decimal | int]] = {}
    for product, product_run in run.products.items():
        records = product_run.roll_records
        summary[product] = {
            "count": len(records),
            "spread_cost": sum((record.spread_cost for record in records), Decimal(0)),
            "commission": sum((record.commission for record in records), Decimal(0)),
        }
    return summary


def write_multi_product_report(run: MultiProductRun, out_path: Path) -> Path:
    """写出多品种样本外与移仓归因报告 (FR-VAL-03)."""
    in_sample, out_of_sample = split_metrics(run)
    attribution = roll_attribution(run)
    lines: list[str] = []
    lines.append("# S4 多品种样本外报告与移仓归因")
    lines.append("")
    lines.append(f"- 样本区间: {run.sample_start} ~ {run.sample_end}；拟合窗结束: {run.train_end}")
    lines.append(f"- 品种: {', '.join(sorted(run.products))}")
    lines.append("- 结算价来源: 当日收盘价假设 (BAR_CLOSE_ASSUMPTION)，非官方结算价")
    lines.append("- 经济参数: product_registry 研究假设，待规则核验 (FR-RULE-05)")
    lines.append("")
    lines.append("## 一、组合样本内外指标")
    lines.append("")
    lines.append("| 区间 | 期初资金 | 期末权益 | 总收益 | 年化 | 最大回撤% | 夏普 | 交易日 |")
    lines.append("| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for label, metrics in (("拟合窗", in_sample["portfolio"]), ("保留窗", out_of_sample["portfolio"])):
        lines.append(
            f"| {label} | {metrics.initial_capital:,.0f} | {metrics.final_equity:,.0f} | "
            f"{metrics.total_return * 100:.2f}% | {metrics.annualized_return * 100:.2f}% | "
            f"{metrics.max_drawdown_percent * 100:.2f} | {metrics.sharpe_ratio:.3f} | {metrics.trading_days} |"
        )
    lines.append("")
    lines.append("## 二、分品种成交贡献与移仓归因")
    lines.append("")
    lines.append(
        "| 品种 | 期末权益 | 收益% | 成交笔数 | 手续费 | 换手率 | 移仓次数 | 移仓价差成本 | 移仓手续费 | "
        "两腿最大间隔(自然日) |"
    )
    lines.append("| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for product in sorted(run.products):
        metrics = run.products[product].metrics
        roll = attribution[product]
        gap = max((record.exposure_calendar_days for record in run.products[product].roll_records), default=0)
        lines.append(
            f"| {product} | {metrics.final_equity:,.0f} | {metrics.total_return * 100:.2f}% | "
            f"{metrics.total_trades} | {metrics.total_commission:,.2f} | {metrics.turnover_ratio:.2f} | "
            f"{roll['count']} | {roll['spread_cost']:,.2f} | {roll['commission']:,.2f} | {gap} |"
        )
    lines.append("")
    lines.append("> 移仓价差成本是信息归因项：价差本身不计入账本现金盈亏，避免与两腿真实成交重复计收益。")
    lines.append("> 两腿最大间隔按自然日披露：日线粒度下先平后开无法在同一 Bar 内同时成交，")
    lines.append("> 残留暴露窗口已逐笔记录（`RollRecord.exposure_calendar_days`），不当作零成本处理。")
    lines.append("")
    lines.append("## 三、已登记的口径与缺口")
    lines.append("")
    lines.append("| 项 | 口径 | 状态 |")
    lines.append("| :--- | :--- | :--- |")
    lines.append("| 结算价 | 当日收盘价假设，非官方结算价 | 研究假设 (GAP-S0-03 未关闭) |")
    lines.append("| 成交额 | 免费源缺成交额字段，按不可用登记 | TURNOVER_UNAVAILABLE，不参与精确核算 |")
    lines.append("| 经济参数 | 乘数/最小变动/保证金/手续费/限仓 | product_registry 待规则核验 |")
    lines.append("| 交易时段 | 夜盘收盘时间、竞价方式、节假日规则 | config/sessions_s4_2024v1.json (schema v2) |")
    lines.append("| 样本外 | 保留窗不参与参数选择，仅作事后评价 | 已按时间切分 |")
    lines.append("")


    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path
