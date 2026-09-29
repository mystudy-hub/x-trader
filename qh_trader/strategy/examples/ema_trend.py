"""[Strategy 层] EMA 趋势过滤、两类入场与成交后风险保护 (S3-04/S5-05, FR-ORD-08).

入场只消费已完成 Bar；Bar 止损在观察后退出，不假定 OHLC 内的价格路径或止损价成交。
实时驱动可额外传入 Tick 立即检查已有止损，重启有仓而无策略状态时拒绝自动启动。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from qh_trader.core.constants import Offset, OrderStatus, Side
from qh_trader.core.objects import Bar, InstrumentId, OrderUpdate, Tick, Trade, require_int
from qh_trader.strategy.base import StrategyBase, StrategyContext


@dataclass(frozen=True)
class EmaTrendParameters:
    """首轮研究参数；未由规则明确的阈值均须随运行清单存档。"""

    mode: str = "A"
    entry_interval: str = "30m"
    fast_period: int = 20
    slow_period: int = 50
    trend_period: int = 200
    atr_period: int = 14
    breakout_lookback: int = 20
    structure_lookback: int = 5
    confirmation_bars: int = 2
    slope_lookback: int = 5
    flat_threshold_atr: Decimal = Decimal("0.05")
    near_ema_atr: Decimal = Decimal("0.5")
    crossing_window: int = 10
    crossing_count: int = 3
    pullback_tolerance_atr: Decimal = Decimal("0.25")
    stop_atr: Decimal = Decimal("2")
    breakeven_atr: Decimal = Decimal("2")
    trailing_atr: Decimal = Decimal("2")
    risk_fraction: Decimal = Decimal("0.015")
    max_lots: int = 1

    def __post_init__(self) -> None:
        if self.mode not in {"A", "B"} or not self.entry_interval:
            raise ValueError("mode must be A/B and entry_interval must be explicit")
        for name in (
            "fast_period",
            "slow_period",
            "trend_period",
            "atr_period",
            "breakout_lookback",
            "structure_lookback",
            "confirmation_bars",
            "slope_lookback",
            "crossing_window",
            "crossing_count",
            "max_lots",
        ):
            require_int(getattr(self, name), name, 1)
        if not self.fast_period < self.slow_period < self.trend_period:
            raise ValueError("EMA periods require fast < slow < trend")
        if self.max_lots != 1:
            raise ValueError("the first strategy experiment requires max_lots=1")
        for name in (
            "pullback_tolerance_atr",
            "flat_threshold_atr",
            "near_ema_atr",
            "stop_atr",
            "breakeven_atr",
            "trailing_atr",
            "risk_fraction",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a finite positive Decimal")
        if self.risk_fraction > Decimal("0.015"):
            raise ValueError("risk_fraction exceeds the authorized 1.5% ceiling")
        if not Decimal("1.5") <= self.stop_atr <= Decimal("2"):
            raise ValueError("stop_atr must be between 1.5 and 2")

    @property
    def warmup_bars(self) -> int:
        return max(
            self.trend_period + max(self.confirmation_bars, self.slope_lookback, self.crossing_window),
            self.breakout_lookback + 1,
            self.atr_period + 1,
        )


class _Ema:
    def __init__(self, period: int) -> None:
        self.period = period
        self.seed: list[Decimal] = []
        self.value: Decimal | None = None

    def update(self, close: Decimal) -> None:
        if self.value is None:
            self.seed.append(close)
            if len(self.seed) == self.period:
                self.value = sum(self.seed) / self.period
                self.seed.clear()
        else:
            self.value += Decimal(2) / (self.period + 1) * (close - self.value)


class EmaTrendStrategy(StrategyBase):
    """同周期 EMA200 过滤；持仓保护从实际成交建立，所有订单经上下文提交。"""

    def __init__(
        self,
        strategy_id: str,
        context: StrategyContext,
        instrument: InstrumentId,
        *,
        parameters: EmaTrendParameters,
        multiplier: Decimal,
        price_tick: Decimal,
        equity_provider: Callable[[], Decimal],
    ) -> None:
        super().__init__(strategy_id, context)
        for value in (multiplier, price_tick):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError("multiplier and price_tick must be finite positive Decimals")
        self.instrument = instrument
        self.parameters = parameters
        self.multiplier = multiplier
        self.price_tick = price_tick
        self.equity_provider = equity_provider
        self._fast = _Ema(parameters.fast_period)
        self._slow = _Ema(parameters.slow_period)
        self._trend = _Ema(parameters.trend_period)
        self._trend_history: deque[Decimal] = deque(maxlen=parameters.slope_lookback + 1)
        self._trend_signs: deque[int] = deque(maxlen=parameters.crossing_window + 1)
        self._bars: deque[Bar] = deque(maxlen=max(parameters.breakout_lookback, parameters.structure_lookback) + 1)
        self._tr_seed: list[Decimal] = []
        self.atr: Decimal | None = None
        self._above = 0
        self._below = 0
        self._long_pullback = False
        self._short_pullback = False
        self._last_bar_end = None
        self._last_tick_time = None
        self._pending_open: str | None = None
        self._pending_close: str | None = None
        self._planned_distance: Decimal | None = None
        self._planned_lots: int = 0
        self._entry_atr: Decimal | None = None
        self.entry_price: Decimal | None = None
        self.stop_price: Decimal | None = None
        self._position = 0
        self._position_opened_at = None
        self._extreme: Decimal | None = None
        self._breakeven = False
        self._seen_trades: set = set()
        self._stop_history: list[tuple] = []
        self.decisions: list[dict] = []

    def on_start(self) -> None:
        if self.context.get_position(self.instrument) != self._position:
            raise ValueError("existing position has no matching strategy protection state; reconcile before starting")
        super().on_start()

    @property
    def ready(self) -> bool:
        return self._trend.value is not None and self.atr is not None and len(self._bars) >= 2

    def warmup(self, bars: Sequence[Bar]) -> None:
        """仅接受启动前已可见历史，更新指标及形态，不发出交易意图。"""
        if self.is_active or self._pending_open or self._position:
            raise ValueError("warmup is only allowed before an empty strategy starts")
        for bar in bars:
            self._consume(bar, trading=False)

    def on_bar(self, bar: Bar) -> None:
        self._consume(bar, trading=self.is_active)

    def _consume(self, bar: Bar, *, trading: bool) -> None:
        if bar.instrument != self.instrument or bar.interval != self.parameters.entry_interval:
            return
        if bar.meta.available_at > self.context.now() or bar.bar_end > self.context.now():
            raise ValueError("strategy cannot consume a bar before it is available")
        if self._last_bar_end is not None and bar.bar_end <= self._last_bar_end:
            if bar.bar_end == self._last_bar_end:
                return
            raise ValueError("strategy bars must be chronological")
        if self._last_bar_end is not None and bar.bar_start < self._last_bar_end:
            raise ValueError("strategy bars must not overlap")
        prior = self._bars[-1] if self._bars else None
        previous_fast, previous_slow = self._fast.value, self._slow.value
        previous_above, previous_below = self._above, self._below
        old_stop = next(
            (stop for effective_at, stop in reversed(self._stop_history) if effective_at <= bar.bar_start), None
        )
        for ema in (self._fast, self._slow, self._trend):
            ema.update(bar.close)
        tr = bar.high - bar.low
        if prior is not None:
            tr = max(tr, abs(bar.high - prior.close), abs(bar.low - prior.close))
        if self.atr is None:
            self._tr_seed.append(tr)
            if len(self._tr_seed) == self.parameters.atr_period:
                self.atr = sum(self._tr_seed) / self.parameters.atr_period
                self._tr_seed.clear()
        else:
            self.atr = (self.atr * (self.parameters.atr_period - 1) + tr) / self.parameters.atr_period
        self._last_bar_end = bar.bar_end
        if self._trend.value is not None:
            self._trend_history.append(self._trend.value)
            self._trend_signs.append(1 if bar.close > self._trend.value else -1 if bar.close < self._trend.value else 0)
            self._above = self._above + 1 if bar.close > self._trend.value else 0
            self._below = self._below + 1 if bar.close < self._trend.value else 0
            if not self._above:
                self._long_pullback = False
            if not self._below:
                self._short_pullback = False
        if self.ready and prior is not None:
            assert self.atr is not None and self._fast.value is not None and self._slow.value is not None
            assert self._trend.value is not None
            tolerance = self.parameters.pullback_tolerance_atr * self.atr
            touched = any(
                bar.low <= value + tolerance and bar.high >= value - tolerance
                for value in (self._fast.value, self._slow.value, self._trend.value)
            )
            if previous_above >= self.parameters.confirmation_bars and self._above and touched:
                self._long_pullback = True
            if previous_below >= self.parameters.confirmation_bars and self._below and touched:
                self._short_pullback = True
            if trading:
                if self._position:
                    # 只能检查进入此 Bar 之前已有止损；本 Bar 的极值只改变下一时刻的止损。
                    hit = old_stop is not None and (bar.low <= old_stop if self._position > 0 else bar.high >= old_stop)
                    ema_exit = bar.close < self._slow.value if self._position > 0 else bar.close > self._slow.value
                    if hit or ema_exit:
                        self._exit("stop_observed_at_bar_close" if hit else "ema50_close", bar.bar_end)
                    else:
                        observed = bar.high if self._position > 0 else bar.low
                        if self._position_opened_at is not None and self._position_opened_at > bar.bar_start:
                            observed = bar.close
                        self._advance_stop(observed)
                        self._record_stop(bar.meta.available_at)
                elif not self._pending_open and not self._pending_close:
                    side = self._entry_side(bar, prior, previous_fast, previous_slow)
                    if side:
                        self._enter(side, bar)
        self._bars.append(bar)
        before_end = [entry for entry in self._stop_history if entry[0] <= bar.bar_end]
        self._stop_history = before_end[-1:] + [entry for entry in self._stop_history if entry[0] > bar.bar_end]

    def _entry_side(self, bar: Bar, prior: Bar, previous_fast, previous_slow) -> Side | None:
        assert self._fast.value is not None and self._slow.value is not None and self._trend.value is not None
        if len(self._trend_history) < self.parameters.slope_lookback + 1 or self.atr is None or self.atr <= 0:
            return None
        slope = (self._trend_history[-1] - self._trend_history[0]) / self.atr
        signs = [sign for sign in self._trend_signs if sign]
        crossings = sum(a != b for a, b in zip(signs, signs[1:], strict=False))
        tangled = (
            abs(slope) <= self.parameters.flat_threshold_atr
            and abs(bar.close - self._trend.value) <= self.parameters.near_ema_atr * self.atr
            and crossings >= self.parameters.crossing_count
        )
        if tangled:
            return None
        if self.parameters.mode == "A":
            if slope >= 0 and self._long_pullback and self._above and bar.close > bar.open and bar.close > prior.high:
                return Side.BUY
            if slope <= 0 and self._short_pullback and self._below and bar.close < bar.open and bar.close < prior.low:
                return Side.SELL
        elif previous_fast is not None and previous_slow is not None:
            previous = list(self._bars)[-self.parameters.breakout_lookback :]
            if len(previous) < self.parameters.breakout_lookback:
                return None
            if (
                slope >= 0
                and previous_fast <= previous_slow
                and self._fast.value > self._slow.value > self._trend.value
                and bar.close > max(b.high for b in previous)
            ):
                return Side.BUY
            if (
                slope <= 0
                and previous_fast >= previous_slow
                and self._fast.value < self._slow.value < self._trend.value
                and bar.close < min(b.low for b in previous)
            ):
                return Side.SELL
        return None

    def _enter(self, side: Side, bar: Bar) -> None:
        assert self.atr is not None
        previous = list(self._bars)[-self.parameters.structure_lookback :]
        if len(previous) < self.parameters.structure_lookback or self.atr <= 0:
            return
        structure_distance = (
            bar.close - min(b.low for b in previous) if side == Side.BUY else max(b.high for b in previous) - bar.close
        )
        if structure_distance <= 0:
            return
        distance = min(self.parameters.stop_atr * self.atr, structure_distance)
        # 向更紧的一侧取整：多头止损更高、空头更低；不足一跳则不交易。
        distance = (distance / self.price_tick).to_integral_value(rounding=ROUND_FLOOR) * self.price_tick
        if distance < self.price_tick:
            return
        equity = self.equity_provider()
        if not isinstance(equity, Decimal) or not equity.is_finite() or equity <= 0:
            raise ValueError("risk sizing requires current finite positive account equity")
        lots = int(
            (equity * self.parameters.risk_fraction / (distance * self.multiplier)).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        lots = min(lots, self.parameters.max_lots)
        if lots < 1:
            self.decisions.append({"at": bar.bar_end, "reason": "risk_budget_below_one_lot"})
            return
        self._planned_distance = distance
        self._planned_lots = lots
        self._entry_atr = self.atr
        sender = self.buy if side == Side.BUY else self.sell
        self._pending_open = sender(self.instrument, lots, Offset.OPEN)
        self._long_pullback = self._short_pullback = False
        self.decisions.append(
            {
                "at": bar.bar_end,
                "reason": f"entry_{self.parameters.mode}",
                "side": side.value,
                "quantity": lots,
                "risk_distance": distance,
            }
        )

    def _exit(self, reason: str, at) -> None:
        if not self._position or self._pending_close:
            return
        if self._pending_open:
            self.context.cancel_order(self._pending_open)
        sender = self.sell if self._position > 0 else self.buy
        self._pending_close = sender(self.instrument, abs(self._position), Offset.CLOSE)
        self.decisions.append({"at": at, "reason": reason, "quantity": abs(self._position)})

    def _advance_stop(self, observed: Decimal) -> None:
        if self.entry_price is None or self.stop_price is None or self._entry_atr is None or not self._position:
            return
        long = self._position > 0
        self._extreme = (max if long else min)(self._extreme or self.entry_price, observed)
        profit = self._extreme - self.entry_price if long else self.entry_price - self._extreme
        if profit >= self.parameters.breakeven_atr * self._entry_atr:
            self._breakeven = True
        if self._breakeven:
            atr = self.atr if self.atr is not None else self._entry_atr
            trailing = (
                self._extreme - self.parameters.trailing_atr * atr
                if long
                else (self._extreme + self.parameters.trailing_atr * atr)
            )
            candidate = (max if long else min)(self.stop_price, self.entry_price, trailing)
            rounding = ROUND_CEILING if long else ROUND_FLOOR
            self.stop_price = (candidate / self.price_tick).to_integral_value(rounding=rounding) * self.price_tick

    def on_tick(self, tick: Tick) -> None:
        """仅用已知止损检查最新成交价；触发价不保证等于最终平仓成交价。"""
        if tick.instrument != self.instrument:
            return
        if tick.meta.available_at > self.context.now():
            raise ValueError("strategy cannot consume a tick before it is available")
        if self._last_tick_time is not None and tick.meta.event_time < self._last_tick_time:
            return
        self._last_tick_time = tick.meta.event_time
        if not self._position or self.stop_price is None or tick.last_price is None:
            return
        if self._position_opened_at is not None and tick.meta.event_time < self._position_opened_at:
            return
        hit = tick.last_price <= self.stop_price if self._position > 0 else tick.last_price >= self.stop_price
        if hit:
            self._exit("tick_stop", tick.meta.event_time)
        else:
            self._advance_stop(tick.last_price)
            self._record_stop(tick.meta.available_at)

    def _record_stop(self, effective_at) -> None:
        """保留当前桶所需的保护历史，避免新桶首 Tick 的止损被应用到刚结束旧桶。"""
        if self.stop_price is None:
            return
        if self._stop_history:
            if self._stop_history[-1][1] == self.stop_price:
                return
            effective_at = max(effective_at, self._stop_history[-1][0])
        self._stop_history.append((effective_at, self.stop_price))

    def on_order(self, order: OrderUpdate) -> None:
        if order.instrument != self.instrument:
            return
        if order.status in {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED}:
            if order.offset == Offset.OPEN and order.identity.client_order_id == self._pending_open:
                # 委托终态不是成交事实；先到的 FILLED 不得释放尚未建立止损的开仓意图。
                if order.status != OrderStatus.FILLED and order.filled_quantity <= abs(self._position):
                    self._pending_open = None
            elif order.offset != Offset.OPEN and order.identity.client_order_id == self._pending_close:
                if order.status != OrderStatus.FILLED and (order.filled_quantity == 0 or self._position == 0):
                    self._pending_close = None

    def on_trade(self, trade: Trade) -> None:
        # 引擎负责策略归因；平仓子单可能拥有不同于返回父单的 client_order_id。
        if trade.instrument != self.instrument or trade.deduplication_key in self._seen_trades:
            return
        self._seen_trades.add(trade.deduplication_key)
        signed = trade.quantity if trade.side == Side.BUY else -trade.quantity
        if trade.offset == Offset.OPEN:
            if self._planned_distance is None or self._entry_atr is None:
                raise ValueError("opening fill has no strategy risk plan")
            old_size = abs(self._position)
            if not old_size:
                self._position_opened_at = trade.event_time
            total = old_size + trade.quantity
            self.entry_price = ((self.entry_price or trade.price) * old_size + trade.price * trade.quantity) / total
            self._position += signed
            if abs(self._position) >= self._planned_lots:
                self._pending_open = None
            long = self._position > 0
            candidate = self.entry_price - self._planned_distance if long else self.entry_price + self._planned_distance
            self.stop_price = (
                candidate if self.stop_price is None else (max if long else min)(self.stop_price, candidate)
            )
            self._extreme = self.entry_price
            self._record_stop(trade.available_at)
        else:
            if not self._position or (signed > 0) == (self._position > 0) or trade.quantity > abs(self._position):
                raise ValueError("closing fill is inconsistent with strategy position")
            self._position += signed
            if not self._position:
                self.entry_price = self.stop_price = self._extreme = None
                self._position_opened_at = None
                self._pending_close = None
                self._breakeven = False
                self._stop_history.clear()
