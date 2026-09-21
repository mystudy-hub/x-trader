"""[Research 层] 多品种组合回测、样本外评价与移仓归因 (S4-06, FR-VAL-01/02/03, FR-CON-06/07).

设计要点：
1. **信号在连续序列上，成交在实际主力合约上**：每个品种用当时可见的主力映射拼接连续序列
   计算双均线信号，但委托只发给当日主力实际合约，绝不把派生序列当作成交标的 (FR-CON-01/03)。
   连续序列的切换日调整只用同一交易日两个实际合约的价差；缺参考价的 Bar 带 ``degraded`` 标记，
   策略在该日禁用信号 (FR-CON-03 降级方案)。
2. **移仓真实执行**：主力切换 (T 日收盘确认、T+1 首个可交易时段生效) 且有持仓时，`RollManager`
   生成两腿委托 (先平后开)，由策略提交给引擎并按实际成交推进；某一腿被拒绝 / 撤销 / 过期时任务
   进入 PAUSED，按重试上限重试，超限进入 FAILED；无论成败都以实际持仓为准继续，并把未完成移仓
   连同剩余暴露写入报告 (FR-CON-06)。
3. **样本外**：按时间切分拟合窗与保留窗，拟合窗只用于信号参数选择，保留窗不参与调参 (FR-VAL-01)。
4. **归因**：分品种成交贡献、逐次移仓明细 (两腿合约、价格、时刻、暴露交易日)、移仓价差 (带符号的
   展期价差，仅信息项) 与两腿实际手续费 (取自账本成交事实) 写入报告；价差不重复计入收益。
5. **长假钩子**：按版本化日历识别的法定假日首日，节前若干交易日禁止新开仓 (FR-RISK-06)，
   被拒意图写入报告。

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
from qh_trader.core.objects import Bar, ControlEpoch, InstrumentId, OrderUpdate, Trade
from qh_trader.data.calendar import TradingCalendar, project_product_calendar
from qh_trader.data.continuous import AdjustmentMethod, ContinuousBar, ContinuousSeriesBuilder
from qh_trader.data.contracts import ContractResolver
from qh_trader.data.dominant_contract import DominantContractResolver, build_dominant_mappings
from qh_trader.data.product_registry import ProductSpec, get_product_spec
from qh_trader.data.session_gate import CalendarSessionGate
from qh_trader.data.storage import ParquetDataStorage, normalize_interval
from qh_trader.domain.risk import HolidayRiskHook, RiskManager
from qh_trader.domain.rollover import LegOrderPolicy, RollManager, RollState, RollTask
from qh_trader.engine.backtest_engine import BacktestEngine, BacktestResult
from qh_trader.engine.base_engine import InstrumentEconomics
from qh_trader.gateway.simulated_gateway import SimulatedGateway
from qh_trader.strategy.base import StrategyBase

# 结算价来源标记：研究模式用当日收盘价，不是官方结算价 (FR-LED-09/FR-VAL-03)。
BAR_CLOSE_SETTLEMENT = "BAR_CLOSE_ASSUMPTION"

# S4-05 生成的紧凑时段模板；运行时按品种投影到本次交易的合约集合。
DEFAULT_SESSION_TEMPLATE = Path("config/sessions_s4_2024v2.json")
# S4-05 由可观测序列推导的实际合约目录；主连序列不在其中，合约必须经它解析 (FR-CON-01, A29)。
DEFAULT_CONTRACT_CATALOG = Path("config/contract_catalog_s4_2024v1.json")
# 研究数据集与工程样本分开存放 (07 §4.4)。
DEFAULT_RESEARCH_STORAGE = Path("data_storage/s4_research")


@dataclass(frozen=True, slots=True)
class ProductDataset:
    """一个品种的实际合约行情、主力映射与连续序列."""

    spec: ProductSpec
    contracts: tuple[InstrumentId, ...]
    bars: tuple[Bar, ...]
    resolver: DominantContractResolver
    continuous: tuple[ContinuousBar, ...]
    continuous_close_by_day: Mapping[date, Decimal]
    degraded_days: frozenset[date]
    raw_close_by_key: Mapping[tuple[InstrumentId, date], Decimal]
    first_day: date
    last_day: date
    calendar: TradingCalendar | None = None
    catalog_version: str | None = None
    adjustment_degradations: tuple = ()

    def dominant_on(self, day: date, *, at: datetime | None = None) -> InstrumentId:
        reference = at if at is not None else _day_decision_time(day)
        return self.resolver.dominant(self.spec.product_id, reference).value


def _day_decision_time(day: date) -> datetime:
    """日盘结束后的决策时刻 (15:00 Asia/Shanghai)，用于查询当时可见的主力."""
    from zoneinfo import ZoneInfo

    return datetime(day.year, day.month, day.day, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _discover_contract_symbols(storage: ParquetDataStorage, spec: ProductSpec, interval: str) -> list[str]:
    snapshot = storage.capture_snapshot()
    metric = "".join(ch for ch in spec.product if ch.isalpha()).casefold()
    symbols: list[str] = []
    for entry in snapshot.datasets.values():
        if entry.get("kind") != "bar" or entry.get("interval") != interval:
            continue
        instrument = str(entry.get("instrument", ""))
        if not instrument.startswith(f"{spec.exchange.value}."):
            continue
        symbol = instrument.split(".", 1)[1]
        # 只接受 4 位交割月的实际合约；主连/连续序列 (如 rb0) 绝不能作为成交标的 (FR-CON-01)
        if not re.fullmatch(r"[A-Za-z]{1,3}\d{4}", symbol):
            continue
        if "".join(ch for ch in symbol if ch.isalpha()).casefold() != metric:
            continue
        symbols.append(symbol)
    return sorted(set(symbols))


def load_product_dataset(
    product: str,
    *,
    storage: ParquetDataStorage,
    interval: str = "1d",
    adjustment: AdjustmentMethod = AdjustmentMethod.DIFF,
    session_template: Path | str | None = DEFAULT_SESSION_TEMPLATE,
    contract_catalog: Path | str | None = DEFAULT_CONTRACT_CATALOG,
    window: tuple[date, date] | None = None,
) -> ProductDataset:
    """读取一个品种的全部实际合约 Bar，构建主力映射 (T+1 生效) 与无跳空连续序列.

    合约代码经版本化目录 (`ContractResolver`) 解析，缺目录条目的代码明确失败 (A29)；
    给定时段模板时，主力切换的生效时刻取日历中确认日收盘后的下一个可撮合时段 (FR-CON-02)。
    """
    spec = get_product_spec(product)
    snapshot = storage.capture_snapshot()
    interval = normalize_interval(interval)
    symbols = _discover_contract_symbols(storage, spec, interval)

    catalog: ContractResolver | None = None
    catalog_version: str | None = None
    if contract_catalog is not None:
        catalog_path = Path(contract_catalog)
        if not catalog_path.is_absolute():
            catalog_path = Path(__file__).resolve().parents[2] / catalog_path
        catalog = ContractResolver.from_file(catalog_path)
        catalog_version = catalog.catalog_version

    contracts: list[InstrumentId] = []
    bars_by_instrument: dict[InstrumentId, tuple[Bar, ...]] = {}
    for symbol in symbols:
        instrument = InstrumentId(spec.exchange, symbol)
        bars = tuple(storage.read_bars(instrument, interval, snapshot=snapshot))
        if not bars:
            continue
        if catalog is not None:
            resolved, _, _ = catalog.resolve(f"{spec.exchange.value}.{symbol}", as_of=bars[0].meta.trading_day)
            if resolved != instrument:
                raise ValueError(f"catalog resolves {symbol} to {resolved}, dataset is keyed by {instrument}")
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
    days = sorted({bar.meta.trading_day for bar in all_bars})

    calendar: TradingCalendar | None = None
    session_open_after = None
    if session_template is not None:
        template_path = Path(session_template)
        if not template_path.is_absolute():
            template_path = Path(__file__).resolve().parents[2] / template_path
        calendar = project_product_calendar(
            template_path, spec.product, tuple(contracts), window=window or (days[0], days[-1])
        )
        gate = CalendarSessionGate(calendar)
        representative = contracts[0]

        def session_open_after(after: datetime) -> datetime | None:
            return gate.next_session_open(representative, after)

    resolver = build_dominant_mappings(spec.product_id, all_bars, confirm_days=2, session_open_after=session_open_after)
    builder = ContinuousSeriesBuilder(resolver, method=adjustment)
    continuous = tuple(builder.build_series(bars_by_instrument))

    raw_close: dict[tuple[InstrumentId, date], Decimal] = {}
    for instrument, seq in bars_by_instrument.items():
        for bar in seq:
            raw_close[(instrument, bar.meta.trading_day)] = bar.close

    continuous_close = {item.raw_bar.meta.trading_day: item.adjusted_close for item in continuous}
    degraded_days = frozenset(item.raw_bar.meta.trading_day for item in continuous if item.degraded)
    return ProductDataset(
        spec=spec,
        contracts=tuple(sorted(contracts, key=str)),
        bars=all_bars,
        resolver=resolver,
        continuous=continuous,
        continuous_close_by_day=continuous_close,
        degraded_days=degraded_days,
        raw_close_by_key=raw_close,
        first_day=days[0],
        last_day=days[-1],
        calendar=calendar,
        catalog_version=catalog_version,
        adjustment_degradations=tuple(builder.degradations),
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
    leg1_order_ids: tuple[str, ...] = ()
    leg2_order_ids: tuple[str, ...] = ()
    leg1_filled_at: datetime | None = None
    exposure_trading_days: int = 0

    @property
    def spread_cost(self) -> Decimal:
        """展期价差 (带符号)：多头视角新合约贵于旧合约为正 (成本)，空头反向；只作归因信息."""
        if self.side == PositionSide.LONG:
            return (self.to_price - self.from_price) * Decimal(self.quantity) * self.multiplier
        return (self.from_price - self.to_price) * Decimal(self.quantity) * self.multiplier

    @property
    def exposure_calendar_days(self) -> int:
        """两腿之间的自然日数 (先平后开的空仓窗口)；交易日口径见 ``exposure_trading_days``."""
        if self.start_leg1 is None or self.completed_at is None:
            return 0
        return max(0, (self.completed_at.date() - self.start_leg1.date()).days)


@dataclass(frozen=True, slots=True)
class IncompleteRollRecord:
    """未完成的移仓：实际剩余持仓与风险暴露必须报告 (FR-CON-06, FR-VAL-03)."""

    product: str
    roll_id: str
    from_instrument: InstrumentId
    to_instrument: InstrumentId
    side: PositionSide
    total_quantity: int
    leg1_filled_qty: int
    leg2_filled_qty: int
    remaining_exposure_qty: int
    state: str
    reason: str | None
    retries: int
    started_at: datetime | None
    recorded_at: datetime
    remaining_position_from: int
    remaining_position_to: int


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
        max_leg_retries: int = 3,
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
        self.max_leg_retries = max_leg_retries
        self._closes: list[Decimal] = []
        self._last_day: date | None = None
        self._target = 0
        self._held_contract: InstrumentId | None = None
        self._roll_task: RollTask | None = None
        self._roll_started_at: datetime | None = None
        self._leg1_filled_at: datetime | None = None
        self.roll_records: list[RollRecord] = []
        self.incomplete_rolls: list[IncompleteRollRecord] = []
        self.signal_suppressed_days: list[date] = []
        # 按委托号跟踪在途意图；引擎回答父单及其子单是否仍可能成交 (本地拒绝也会使其失活)
        self._pending: set[str] = set()

    # ------------------------------------------------------------------ 策略回调
    def on_bar(self, bar: Bar) -> None:
        day = bar.meta.trading_day
        # 连续序列信号：同一交易日只记录一次，且只用当日可见的连续收盘价 (无未来信息)
        if day != self._last_day and day in self.dataset.continuous_close_by_day:
            self._closes.append(self.dataset.continuous_close_by_day[day])
            self._last_day = day

        self._prune_pending()
        dominant = self.dataset.dominant_on(day, at=bar.bar_end)

        task = self._roll_task
        if task is not None:
            if task.state == RollState.PAUSED:
                self._retry_or_abandon_roll(bar.bar_end)
            return  # 两腿未完成前不做任何新信号交易

        held = self._held_contract
        position = self._position_on(held) if held is not None else 0
        if held is not None and held != dominant:
            if position != 0:
                # 主力切换且仍有持仓：真实移仓 (先平旧合约，再开新合约)
                self._start_roll(held, dominant, position, bar.bar_end)
                return
            self._held_contract = dominant

        if day in self.dataset.degraded_days:
            # 切换日缺少调整参考价：按 FR-CON-03 降级方案禁用当日信号并记录
            self.signal_suppressed_days.append(day)
            return
        signal = self._signal()
        if signal is None:
            return
        self._target = signal * self.order_size
        self._held_contract = dominant
        self._apply_target(dominant)

    def on_order(self, order: OrderUpdate) -> None:
        if order.status not in (OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.EXPIRED):
            return
        task = self._roll_task
        if task is None or task.is_done or task.state == RollState.PAUSED:
            return
        # 引擎会把 CLOSE 改写成 CLOSE_YESTERDAY 或拆成子单，回报里是子单号；按合约归属确定是哪一腿
        if order.instrument == task.from_instrument:
            leg = 1
        elif order.instrument == task.to_instrument:
            leg = 2
        else:
            return
        if order.filled_quantity >= order.quantity:
            return  # 终态前已全部成交 (迟到的撤单回报)，不算失败
        self.roll_manager.on_leg_failed(task, leg, f"{order.status.value} leg{leg} {order.instrument}")

    def on_trade(self, trade: Trade) -> None:
        task = self._roll_task
        if task is None or task.is_done:
            return
        if trade.instrument == task.from_instrument:
            self.roll_manager.on_fill(task, trade, leg=1)
            if self._leg1_filled_at is None:
                self._leg1_filled_at = trade.event_time
        elif trade.instrument == task.to_instrument:
            self.roll_manager.on_fill(task, trade, leg=2)
        else:
            return
        if task.state == RollState.COMPLETED:
            self._finish_roll(task, trade)
        elif task.state != RollState.PAUSED:
            # 第一腿成交后立即送出第二腿，尽量缩短两腿之间的暴露窗口
            self._advance_roll(trade.event_time)

    # ------------------------------------------------------------------ 内部
    def _position_on(self, instrument: InstrumentId) -> int:
        return int(self.context.get_position(instrument))

    def _prune_pending(self) -> None:
        self._pending = {cid for cid in self._pending if self.context.is_order_active(cid)}

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
        if self._pending:
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
        client_order_id = action(instrument, quantity, offset)
        if self.context.is_order_active(client_order_id):
            self._pending.add(client_order_id)

    def _start_roll(
        self, from_instrument: InstrumentId, to_instrument: InstrumentId, position: int, now: datetime
    ) -> None:
        side = PositionSide.LONG if position > 0 else PositionSide.SHORT
        self._roll_started_at = now
        self._leg1_filled_at = None
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
        if not self.context.is_order_active(client_order_id):
            # 本地拒绝在 send_order 内已同步回调 on_order；绑定之后再次确认任务停在 PAUSED 且腿单号已清空
            self.roll_manager.on_leg_failed(task, leg, task.failure_reason or f"rejected leg{leg} {intent.instrument}")
            return
        self._pending.add(client_order_id)

    def _retry_or_abandon_roll(self, now: datetime) -> None:
        task = self._roll_task
        if task is None:
            return
        if self.roll_manager.resume(task, max_retries=self.max_leg_retries):
            self._advance_roll(now)
            return
        # 重试用尽：记录未完成移仓与实际剩余持仓，按真实持仓继续，不假装移仓已完成 (FR-CON-06)
        self._record_incomplete(task, now)
        self._roll_task = None
        self._roll_started_at = None
        self._leg1_filled_at = None
        pos_from = self._position_on(task.from_instrument)
        pos_to = self._position_on(task.to_instrument)
        if pos_to != 0:
            self._held_contract = task.to_instrument
            self._target = pos_to
        elif pos_from != 0:
            self._held_contract = task.from_instrument
            self._target = pos_from
        else:
            self._held_contract = task.to_instrument
            self._target = 0

    def _record_incomplete(self, task: RollTask, now: datetime) -> None:
        self.incomplete_rolls.append(
            IncompleteRollRecord(
                product=self.dataset.spec.product,
                roll_id=task.roll_id,
                from_instrument=task.from_instrument,
                to_instrument=task.to_instrument,
                side=task.position_side,
                total_quantity=task.total_quantity,
                leg1_filled_qty=task.leg1_filled_qty,
                leg2_filled_qty=task.leg2_filled_qty,
                remaining_exposure_qty=task.remaining_exposure_qty,
                state=task.state.value,
                reason=task.failure_reason,
                retries=task.retry_count,
                started_at=self._roll_started_at,
                recorded_at=now,
                remaining_position_from=self._position_on(task.from_instrument),
                remaining_position_to=self._position_on(task.to_instrument),
            )
        )

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
                commission=Decimal(0),  # 由 run_product 按账本成交事实回填
                start_leg1=self._roll_started_at,
                completed_at=trade.event_time,
                leg1_order_ids=tuple(task.leg1_order_ids),
                leg2_order_ids=tuple(task.leg2_order_ids),
                leg1_filled_at=self._leg1_filled_at,
            )
        )
        self._held_contract = task.to_instrument
        self._target = task.total_quantity if task.position_side == PositionSide.LONG else -task.total_quantity
        self._roll_task = None
        self._roll_started_at = None
        self._leg1_filled_at = None

    def unfinished_roll(self, now: datetime) -> None:
        """回测结束时仍在途的移仓：记录为未完成 (不清空任务，供报告)."""
        task = self._roll_task
        if task is not None and not task.is_done:
            self._record_incomplete(task, now)


@dataclass(frozen=True, slots=True)
class ProductRun:
    spec: ProductSpec
    dataset: ProductDataset
    result: BacktestResult
    metrics: PerformanceMetrics
    roll_records: tuple[RollRecord, ...]
    incomplete_rolls: tuple[IncompleteRollRecord, ...] = ()
    signal_suppressed_days: tuple[date, ...] = ()
    holiday_rejections: int = 0
    calendar_version: str | None = None


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
    dataset_snapshot_id: str | None = None
    session_template_version: str | None = None
    holiday_days_before: int | None = None


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
    holiday_days_before: int | None = 1,
    max_leg_retries: int = 3,
) -> ProductRun:
    """单品种运行：实际主力合约成交 + 真实两腿移仓 + 长假钩子.

    给定版本化日历时，委托必须落在日历允许的报单时段；日线策略在收盘后产生的意图
    会被持有到下一个可报单时段 (A05/A21)。日历缺失而 Bar 为日线时引擎明确失败。
    """
    bars = dataset.bars
    calendar = calendar if calendar is not None else dataset.calendar
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
    epoch = ControlEpoch("backtest-controller", 1)
    risk_manager = None
    if calendar is not None and holiday_days_before is not None:
        risk_manager = RiskManager(
            account_id=account_id,
            control=epoch,
            holiday_hook=HolidayRiskHook(
                days_before_holiday=holiday_days_before,
                prevent_new_open=True,
                trading_days=tuple(sorted(calendar.trading_days)),
            ),
            holiday_dates=calendar.holiday_starts(),
        )
    engine = BacktestEngine(
        account_id=account_id,
        gateway=gateway,
        start_time=bars[0].bar_start,
        initial_capital=initial_capital,
        execution_policy=execution_policy,
        session_gate=session_gate,
        risk_manager=risk_manager,
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
        max_leg_retries=max_leg_retries,
    )
    engine.add_strategy(strategy)
    result = engine.run(bars)
    strategy.unfinished_roll(bars[-1].bar_end)
    trading_days = tuple(sorted(calendar.trading_days)) if calendar is not None else ()
    roll_records = tuple(
        replace(
            record,
            commission=_roll_commission(record, result),
            exposure_trading_days=_exposure_trading_days(record, trading_days),
        )
        for record in strategy.roll_records
    )
    metrics = calculate_performance(result, annual_trading_days=242)
    metrics = _with_roll_attribution(metrics, roll_records)
    holiday_rejections = sum(1 for item in result.rejected_intents if "HolidayRiskHook" in item.reason)
    return ProductRun(
        spec=dataset.spec,
        dataset=dataset,
        result=result,
        metrics=metrics,
        roll_records=roll_records,
        incomplete_rolls=tuple(strategy.incomplete_rolls),
        signal_suppressed_days=tuple(strategy.signal_suppressed_days),
        holiday_rejections=holiday_rejections,
        calendar_version=calendar.version if calendar is not None else None,
    )


def _leg_trade_ids(record: RollRecord, result: BacktestResult) -> set[str]:
    """两腿父单及其路由子单名下的成交 (不按时间窗口筛，避免混入同窗信号交易)."""
    order_ids = set(record.leg1_order_ids) | set(record.leg2_order_ids)
    for order in result.orders:
        if order.client_order_id in tuple(record.leg1_order_ids) + tuple(record.leg2_order_ids):
            order_ids.update(order.child_order_ids)
    return {
        trade.trade_id
        for trade in result.trades
        if trade.order_identity is not None and trade.order_identity.client_order_id in order_ids
    }


def _roll_commission(record: RollRecord, result: BacktestResult) -> Decimal:
    """移仓两腿的实际手续费：从账本 COMMISSION 条目按成交事实汇总，不重算 (FR-CON-07)."""
    trade_ids = _leg_trade_ids(record, result)
    references = {f"trade:{trade_id}" for trade_id in trade_ids}
    total = Decimal(0)
    for entry in result.ledger_entries:
        if str(getattr(entry, "kind", "")) == "COMMISSION" and getattr(entry, "reference", None) in references:
            total += -entry.amount
    return total


def _exposure_trading_days(record: RollRecord, trading_days: Sequence[date]) -> int:
    """先平后开：第一腿成交到第二腿成交之间的交易日数 (空仓窗口)."""
    if record.leg1_filled_at is None or record.completed_at is None or not trading_days:
        return 0
    start, end = record.leg1_filled_at.date(), record.completed_at.date()
    return sum(1 for day in trading_days if start < day <= end)


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
    contract_catalog: Path | str | None = DEFAULT_CONTRACT_CATALOG,
    window: tuple[date, date] | None = None,
    holiday_days_before: int | None = 1,
    max_leg_retries: int = 3,
) -> MultiProductRun:
    """多品种组合运行：分品种回测 + 组合权益 + 样本内外评价.

    默认只用各品种共同覆盖的交易日，并投影版本化日历；日历缺失的品种显式失败，
    绝不静默退回到"无时段约束"的工程样例模式 (FR-RULE-05)。
    """
    datasets = {
        product: load_product_dataset(
            product,
            storage=storage,
            interval=interval,
            adjustment=adjustment,
            session_template=session_template,
            contract_catalog=contract_catalog,
            window=window,
        )
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
    template_version: str | None = None
    for product, dataset in datasets.items():
        calendar = project_product_calendar(session_template, dataset.spec.product, dataset.contracts, window=window)
        template_version = calendar.version
        runs[product] = run_product(
            dataset,
            account_id=f"{account_prefix}-{product}",
            initial_capital=initial_capital_per_product,
            fast_window=fast_window,
            slow_window=slow_window,
            order_size=order_size,
            bar_range=window,
            calendar=calendar,
            holiday_days_before=holiday_days_before,
            max_leg_retries=max_leg_retries,
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
        dataset_snapshot_id=storage.capture_snapshot().snapshot_id,
        session_template_version=template_version,
        holiday_days_before=holiday_days_before,
    )


def split_metrics(run: MultiProductRun) -> tuple[dict[str, SliceMetrics], dict[str, SliceMetrics]]:
    """按 train_end 把组合权益切成拟合窗与保留窗 (保留窗不参与参数选择)."""
    in_sample = {"portfolio": slice_metrics(run, start=run.sample_start, end=run.train_end, label="in_sample")}
    out_of_sample = {"portfolio": slice_metrics(run, start=run.train_end, end=None, label="out_of_sample")}
    return in_sample, out_of_sample


def product_metrics(run: MultiProductRun) -> dict[str, PerformanceMetrics]:
    return {product: product_run.metrics for product, product_run in run.products.items()}


def roll_attribution(run: MultiProductRun) -> dict[str, dict[str, Decimal | int]]:
    """分品种移仓归因：次数、展期价差 (带符号) 与两腿手续费；价差不计入收益."""
    summary: dict[str, dict[str, Decimal | int]] = {}
    for product, product_run in run.products.items():
        records = product_run.roll_records
        summary[product] = {
            "count": len(records),
            "incomplete": len(getattr(product_run, "incomplete_rolls", ())),
            "spread_cost": sum((record.spread_cost for record in records), Decimal(0)),
            "commission": sum((record.commission for record in records), Decimal(0)),
        }
    return summary


def session_split(product_run: ProductRun) -> dict[str, int]:
    """成交按所在时段拆分 (夜盘开盘 / 日盘开盘 / 盘中估算)，供报告日 / 夜盘拆分列."""
    counts = {"night": 0, "day": 0, "intrabar": 0}
    gate = CalendarSessionGate(product_run.dataset.calendar) if product_run.dataset.calendar is not None else None
    opens = {bar.open_time for bar in product_run.dataset.bars}
    for trade in product_run.result.trades:
        if trade.event_time not in opens:
            counts["intrabar"] += 1
            continue
        session = gate.session_at(trade.instrument, trade.event_time) if gate is not None else None
        if session is not None and session.session_id.startswith("night"):
            counts["night"] += 1
        else:
            counts["day"] += 1
    return counts


def write_multi_product_report(run: MultiProductRun, out_path: Path) -> Path:
    """写出多品种样本外与移仓归因报告 (FR-VAL-03 / FR-CON-06 / FR-CON-07)."""
    in_sample, out_of_sample = split_metrics(run)
    attribution = roll_attribution(run)
    lines: list[str] = []
    lines.append("# S4 多品种样本外报告与移仓归因")
    lines.append("")
    lines.append(f"- 样本区间: {run.sample_start} ~ {run.sample_end}；拟合窗结束: {run.train_end}")
    lines.append(f"- 品种: {', '.join(sorted(run.products))}")
    lines.append(f"- 数据快照: `{run.dataset_snapshot_id}`；时段模板版本: {run.session_template_version}")
    lines.append("- 结算价来源: 当日收盘价假设 (BAR_CLOSE_ASSUMPTION)，非官方结算价")
    lines.append("- 经济参数: product_registry 研究假设，待规则核验 (FR-RULE-05)")
    lines.append(
        f"- 长假钩子: 节前 {run.holiday_days_before} 个交易日禁止新开仓 (按意图送出所属交易日判断；"
        "移仓第二腿的开仓同样受限，节前最后交易日的先平后开移仓会以空仓过节并在节后补开)"
        if run.holiday_days_before is not None
        else "- 长假钩子: 未启用"
    )
    lines.append("- 滑点: 假设 0 跳；模拟成交价即参考价，实际滑点不能由回测得出 (A24 另行核验)")
    lines.append(
        "- 日线 Open: 有夜盘品种视为前一自然日 21:00 夜盘首笔、否则 09:00 日盘首笔，且视为含集合竞价 "
        "(includes_auction=True，网关按 ASSUME_PARTICIPATION 允许开盘候选参与竞价成交)；来源语义待核验 (A21)"
    )
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
        "| 品种 | 期末权益 | 收益% | 成交笔数 | 夜盘/日盘/盘中 | 手续费 | 换手率 | 移仓完成/未完成 | "
        "展期价差(多头视角成本为正) | 移仓手续费 | 两腿最大间隔(交易日) | 节前拒单 | 信号禁用日 |"
    )
    lines.append("| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for product in sorted(run.products):
        product_run = run.products[product]
        metrics = product_run.metrics
        roll = attribution[product]
        split = session_split(product_run)
        gap = max((record.exposure_trading_days for record in product_run.roll_records), default=0)
        lines.append(
            f"| {product} | {metrics.final_equity:,.0f} | {metrics.total_return * 100:.2f}% | "
            f"{metrics.total_trades} | {split['night']}/{split['day']}/{split['intrabar']} | "
            f"{metrics.total_commission:,.2f} | {metrics.turnover_ratio:.2f} | "
            f"{roll['count']}/{roll['incomplete']} | {roll['spread_cost']:,.2f} | {roll['commission']:,.2f} | "
            f"{gap} | {product_run.holiday_rejections} | {len(product_run.signal_suppressed_days)} |"
        )
    lines.append("")
    lines.append("> 展期价差是信息归因项：价差本身不计入账本现金盈亏，避免与两腿真实成交重复计收益；")
    lines.append("> 移仓手续费取自账本 COMMISSION 事实 (两腿父单及其子单名下成交)，不重算。")
    lines.append("> 两腿最大间隔按交易日披露 (第一腿成交到第二腿成交)；先平后开的空仓窗口不当作零成本处理。")
    lines.append("")
    lines.append("## 三、逐次移仓明细")
    lines.append("")
    lines.append(
        "| 品种 | 旧合约 | 新合约 | 方向 | 手数 | 旧腿均价 | 新腿均价 | 第一腿成交 | 第二腿成交 | "
        "间隔(交易日) | 展期价差 | 手续费 |"
    )
    lines.append("| :--- | :--- | :--- | :--- | ---: | ---: | ---: | :--- | :--- | ---: | ---: | ---: |")
    for product in sorted(run.products):
        for record in run.products[product].roll_records:
            lines.append(
                f"| {product} | {record.from_instrument.symbol} | {record.to_instrument.symbol} | "
                f"{record.side.value} | {record.quantity} | {record.from_price} | {record.to_price} | "
                f"{record.leg1_filled_at.isoformat() if record.leg1_filled_at else '-'} | "
                f"{record.completed_at.isoformat() if record.completed_at else '-'} | "
                f"{record.exposure_trading_days} | {record.spread_cost:,.2f} | {record.commission:,.2f} |"
            )
    lines.append("")
    lines.append("## 四、未完成移仓与实际剩余持仓 (FR-CON-06)")
    lines.append("")
    incomplete = [
        (product, item) for product in sorted(run.products) for item in run.products[product].incomplete_rolls
    ]
    if not incomplete:
        lines.append("无。")
    else:
        lines.append(
            "| 品种 | 任务 | 旧合约 | 新合约 | 方向 | 计划 | 第一腿成交 | 第二腿成交 | 剩余暴露 | 状态 | 重试 | "
            "原因 | 剩余持仓(旧/新) |"
        )
        lines.append("| :--- | :--- | :--- | :--- | :--- | ---: | ---: | ---: | ---: | :--- | ---: | :--- | :--- |")
        for product, item in incomplete:
            lines.append(
                f"| {product} | {item.roll_id} | {item.from_instrument.symbol} | {item.to_instrument.symbol} | "
                f"{item.side.value} | {item.total_quantity} | {item.leg1_filled_qty} | {item.leg2_filled_qty} | "
                f"{item.remaining_exposure_qty} | {item.state} | {item.retries} | {item.reason or '-'} | "
                f"{item.remaining_position_from}/{item.remaining_position_to} |"
            )
    lines.append("")
    lines.append("## 五、已登记的口径与缺口")
    lines.append("")
    lines.append("| 项 | 口径 | 状态 |")
    lines.append("| :--- | :--- | :--- |")
    lines.append("| 结算价 | 当日收盘价假设，非官方结算价 | 研究假设 (GAP-S0-03 未关闭) |")
    lines.append("| 成交额 | 免费源缺成交额字段，按不可用登记 | TURNOVER_UNAVAILABLE，不参与精确核算 |")
    lines.append("| 经济参数 | 乘数/最小变动/保证金/手续费/限仓 | product_registry 待规则核验 |")
    lines.append(
        "| 交易时段 | 夜盘收盘时间、竞价方式；法定假日前夜无夜盘按交易日序列推断 | 模板版本见上；公告归档待补 |"
    )
    lines.append(
        "| 日线 Open 时段归属 | 有夜盘品种日线 Open 按交易所日线惯例视为夜盘首笔，标记 SYNTHETIC | "
        "来源语义待核验 (A21) |"
    )
    lines.append("| 主力切换生效 | T 日收盘确认，T+1 首个可撮合时段生效 | 映射记录带 effective_basis |")
    lines.append("| 连续序列调整 | 同日双合约价差；缺参考价的切换日禁用信号 | 降级日数见上表 |")
    lines.append("| 样本外 | 保留窗不参与参数选择，仅作事后评价 | 已按时间切分 |")
    lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path
