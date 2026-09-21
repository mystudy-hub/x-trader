"""[Data 适配器] 连续序列与收益率视图 (S4-02, FR-CON-03, A10).

核心设计：
1. 区分实际合约行情、连续指标价格与收益率视图；
2. 切换日消除虚假收益：绝不使用 (新合约今日收盘 / 旧合约昨日收盘 - 1) 计算收益，
   也绝不把新合约自身的隔夜跳空当作合约间价差写进复权因子；
3. 复权只用于策略指标计算，交易撮合、账本估值与保证金严格使用未经复权的真实合约价格；
4. 支持差价平移 (DIFF) 与比例复权 (RATIO) 两种模式；
5. 切换日的调整参考价按声明的优先级选取，缺参考价时显式降级并记录 (FR-CON-03 硬约束)。

调整参考价 (`ContinuousBar.adjustment_reference`)：
- ``PREV_DAY_BOTH``：新旧合约在切换前一交易日都有收盘价，价差 = 新昨收 − 旧昨收。
  调整后序列在切换日的变动 = 新合约自身当日变动。
- ``SAME_DAY_BOTH``：新合约没有昨日 Bar，但旧合约在切换日有收盘价，价差 = 新今收 − 旧今收。
  调整后序列在切换日的变动 = 旧合约自身当日变动。
- ``INTRADAY_NEW_ONLY``（降级）：两者都没有，只能用旧昨收 → 新今开的原始跳空；
  调整后序列在切换日的变动 = 新合约日内 (开→收) 变动；该 Bar 标记 ``degraded=True`` 并记入
  ``ContinuousSeriesBuilder.degradations``，策略可据此禁用受影响信号。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from qh_trader.core.constants import MissingRuleError
from qh_trader.core.objects import Bar, InstrumentId
from qh_trader.data.dominant_contract import DominantContractResolver


class AdjustmentMethod(StrEnum):
    RAW = "RAW"  # 原始拼接，无调整
    DIFF = "DIFF"  # 差价平移调整 (后向累加价差)
    RATIO = "RATIO"  # 比例复权调整 (后向累乘比率)


class AdjustmentReference(StrEnum):
    SAME_CONTRACT = "SAME_CONTRACT"
    PREV_DAY_BOTH = "PREV_DAY_BOTH"
    SAME_DAY_BOTH = "SAME_DAY_BOTH"
    INTRADAY_NEW_ONLY = "INTRADAY_NEW_ONLY"


@dataclass(frozen=True, slots=True)
class ContinuousBar:
    """连续行情 Bar (携带真实底层合约引用与复权调整后价格)."""

    raw_bar: Bar
    underlying_instrument: InstrumentId
    adjusted_open: Decimal
    adjusted_high: Decimal
    adjusted_low: Decimal
    adjusted_close: Decimal
    adjustment_factor: Decimal
    single_day_return: Decimal  # 同合约无跳空真实收益率
    adjustment_reference: AdjustmentReference = AdjustmentReference.SAME_CONTRACT
    degraded: bool = False


@dataclass(frozen=True, slots=True)
class AdjustmentDegradation:
    """切换日缺少调整参考价或缺少映射时的显式记录 (FR-CON-03：明确标记，不静默)."""

    trading_day: date
    from_instrument: InstrumentId | None
    to_instrument: InstrumentId | None
    reference: str
    reason: str


class ContinuousSeriesBuilder:
    """连续序列与收益率视图构建器."""

    def __init__(
        self,
        resolver: DominantContractResolver,
        method: AdjustmentMethod = AdjustmentMethod.RAW,
    ) -> None:
        self.resolver = resolver
        self.method = method
        self.degradations: list[AdjustmentDegradation] = []

    def build_series(
        self,
        bars_by_instrument: Mapping[InstrumentId, Sequence[Bar]],
    ) -> list[ContinuousBar]:
        """根据主力映射构建连续行情序列.

        严格按当时可见的主力合约提取 Bar，并在主力切换点用同一交易日的两个合约价差处理跳空.
        """
        self.degradations = []
        bar_lookup: dict[tuple[InstrumentId, date], Bar] = {}
        bars_by_day: dict[date, list[Bar]] = {}
        for inst, bar_seq in bars_by_instrument.items():
            for bar in bar_seq:
                bar_lookup[(inst, bar.meta.trading_day)] = bar
                bars_by_day.setdefault(bar.meta.trading_day, []).append(bar)

        # 1. 提取所有主力 Bar 序列 (按当天结束时刻查询生效的主力)
        stitched_bars: list[tuple[Bar, InstrumentId]] = []
        for day in sorted(bars_by_day):
            day_bars = bars_by_day[day]
            sample_time = max(b.bar_end for b in day_bars)
            try:
                dominant = self.resolver.dominant(self.resolver.product, sample_time).value
            except MissingRuleError as exc:
                self.degradations.append(
                    AdjustmentDegradation(day, None, None, "NO_MAPPING", f"no dominant mapping visible: {exc}")
                )
                continue
            target_bar = bar_lookup.get((dominant, day))
            if target_bar is not None:
                stitched_bars.append((target_bar, dominant))

        if not stitched_bars:
            return []

        # 2. 计算复权因子与同合约真实收益率
        result: list[ContinuousBar] = []
        cumulative_diff = Decimal("0")
        cumulative_ratio = Decimal("1.0")
        prev_bar: Bar | None = None
        prev_inst: InstrumentId | None = None

        for bar, inst in stitched_bars:
            day_ret = Decimal("0")
            reference = AdjustmentReference.SAME_CONTRACT
            degraded = False

            if prev_bar is not None and prev_inst is not None:
                if inst == prev_inst:
                    if prev_bar.close > 0:
                        day_ret = (bar.close - prev_bar.close) / prev_bar.close
                else:
                    day_ret, spread, ratio, reference, degraded = self._switch_adjustment(
                        bar, inst, prev_bar, prev_inst, bar_lookup
                    )
                    if self.method == AdjustmentMethod.DIFF:
                        cumulative_diff += spread
                    elif self.method == AdjustmentMethod.RATIO:
                        cumulative_ratio *= ratio

            adj_open, adj_high, adj_low, adj_close = bar.open, bar.high, bar.low, bar.close
            adj_factor = Decimal("1.0")
            if self.method == AdjustmentMethod.DIFF:
                adj_open -= cumulative_diff
                adj_high -= cumulative_diff
                adj_low -= cumulative_diff
                adj_close -= cumulative_diff
                adj_factor = cumulative_diff
            elif self.method == AdjustmentMethod.RATIO:
                adj_open /= cumulative_ratio
                adj_high /= cumulative_ratio
                adj_low /= cumulative_ratio
                adj_close /= cumulative_ratio
                adj_factor = cumulative_ratio

            result.append(
                ContinuousBar(
                    raw_bar=bar,
                    underlying_instrument=inst,
                    adjusted_open=adj_open,
                    adjusted_high=adj_high,
                    adjusted_low=adj_low,
                    adjusted_close=adj_close,
                    adjustment_factor=adj_factor,
                    single_day_return=day_ret,
                    adjustment_reference=reference,
                    degraded=degraded,
                )
            )
            prev_bar = bar
            prev_inst = inst

        return result

    def _switch_adjustment(
        self,
        bar: Bar,
        inst: InstrumentId,
        prev_bar: Bar,
        prev_inst: InstrumentId,
        bar_lookup: Mapping[tuple[InstrumentId, date], Bar],
    ) -> tuple[Decimal, Decimal, Decimal, AdjustmentReference, bool]:
        """主力切换日：返回 (同合约收益率, 差价平移量, 比例复权比率, 参考价类型, 是否降级).

        差价 / 比率一律取"同一交易日两个实际合约"的价差，绝不取新合约自身的隔夜跳空 (A10)。
        """
        prev_day = prev_bar.meta.trading_day
        new_prev = bar_lookup.get((inst, prev_day))
        old_today = bar_lookup.get((prev_inst, bar.meta.trading_day))

        if new_prev is not None and new_prev.close > 0 and prev_bar.close > 0:
            day_ret = (bar.close - new_prev.close) / new_prev.close
            spread = new_prev.close - prev_bar.close
            ratio = new_prev.close / prev_bar.close
            return day_ret, spread, ratio, AdjustmentReference.PREV_DAY_BOTH, False

        if old_today is not None and old_today.close > 0 and prev_bar.close > 0:
            day_ret = (old_today.close - prev_bar.close) / prev_bar.close
            spread = bar.close - old_today.close
            ratio = bar.close / old_today.close
            return day_ret, spread, ratio, AdjustmentReference.SAME_DAY_BOTH, False

        # 降级：没有任何同日双合约参考价，只能用旧昨收 → 新今开的原始跳空；显式记录
        day_ret = (bar.close - bar.open) / bar.open if bar.open > 0 else Decimal(0)
        spread = bar.open - prev_bar.close
        ratio = bar.open / prev_bar.close if prev_bar.close > 0 else Decimal("1.0")
        self.degradations.append(
            AdjustmentDegradation(
                trading_day=bar.meta.trading_day,
                from_instrument=prev_inst,
                to_instrument=inst,
                reference=AdjustmentReference.INTRADAY_NEW_ONLY.value,
                reason="neither contract has a same-day counterpart price around the switch; "
                "adjustment uses the raw old-close to new-open jump",
            )
        )
        return day_ret, spread, ratio, AdjustmentReference.INTRADAY_NEW_ONLY, True
