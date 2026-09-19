"""[Data 适配器] 连续序列与收益率视图 (S4-02, FR-CON-03, A10).

核心设计：
1. 区分实际合约行情、连续指标价格与收益率视图；
2. 切换日消除虚假收益：绝不使用 (新合约今日收盘 / 旧合约昨日收盘 - 1) 计算收益；
3. 复权仅用于策略指标计算，交易撮合、账本估值与保证金严格使用未经复权的真实合约价格；
4. 支持比例复权与价差平移复权两种模式。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from qh_trader.core.clock import utc_timestamp
from qh_trader.core.objects import Bar, InstrumentId, ProductId
from qh_trader.data.dominant_contract import DominantContractResolver


class AdjustmentMethod(StrEnum):
    RAW = "RAW"                      # 原始拼接，无调整
    DIFF = "DIFF"                    # 差价平移调整 (后向累加价差)
    RATIO = "RATIO"                  # 比例复权调整 (后向累乘比率)


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
    single_day_return: Decimal      # 同合约无跳空真实收益率


class ContinuousSeriesBuilder:
    """连续序列与收益率视图构建器."""

    def __init__(
        self,
        resolver: DominantContractResolver,
        method: AdjustmentMethod = AdjustmentMethod.RAW,
    ) -> None:
        self.resolver = resolver
        self.method = method

    def build_series(
        self,
        bars_by_instrument: Mapping[InstrumentId, Sequence[Bar]],
    ) -> list[ContinuousBar]:
        """根据主力映射构建连续行情序列.

        严格按当时可见的主力合约提取 Bar，并在主力切换点精确处理价差跳空.
        """
        # 1. 提取所有主力 Bar 序列
        stitched_bars: list[tuple[Bar, InstrumentId]] = []

        # 收集所有已知的交易日并排序
        all_days: set[date] = set()
        for bars in bars_by_instrument.values():
            for b in bars:
                all_days.add(b.meta.trading_day)

        sorted_days = sorted(all_days)

        # 建立按 (instrument, day) 索引字典
        bar_lookup: dict[tuple[InstrumentId, date], Bar] = {}
        for inst, bar_seq in bars_by_instrument.items():
            for b in bar_seq:
                bar_lookup[(inst, b.meta.trading_day)] = b

        for day in sorted_days:
            # 找到当天对应的有效主力合约
            # 用当天结束时刻查询生效的主力
            day_bars = [b for (inst, d), b in bar_lookup.items() if d == day]
            if not day_bars:
                continue
            sample_time = max(b.bar_end for b in day_bars)
            try:
                dom_val = self.resolver.dominant(self.resolver.product, sample_time)
                dom_inst = dom_val.value
            except Exception:
                continue

            target_bar = bar_lookup.get((dom_inst, day))
            if target_bar is not None:
                stitched_bars.append((target_bar, dom_inst))

        if not stitched_bars:
            return []

        # 2. 计算复权因子与同合约真实收益率
        result: list[ContinuousBar] = []
        cumulative_diff = Decimal("0")
        cumulative_ratio = Decimal("1.0")

        prev_bar: Bar | None = None
        prev_inst: InstrumentId | None = None

        for i, (bar, inst) in enumerate(stitched_bars):
            day_ret = Decimal("0")

            if prev_bar is not None and prev_inst is not None:
                if inst == prev_inst:
                    # 同一主力合约：直接计算收益率
                    if prev_bar.close > 0:
                        day_ret = (bar.close - prev_bar.close) / prev_bar.close
                else:
                    # 主力切换点 (A10 核心逻辑):
                    # 严禁将新合约 close / 旧合约 close - 1 作为收益率！
                    # 查找新合约在昨日的价格，若存在则用新合约自身的昨日价格计算当日真实波动；
                    # 若不存在，用旧合约在今日的价格计算；若都不可得，采用同日开平价差 (bar.close - bar.open) / bar.open
                    yesterday_new_contract_bar = bar_lookup.get((inst, prev_bar.meta.trading_day))
                    if yesterday_new_contract_bar is not None and yesterday_new_contract_bar.close > 0:
                        day_ret = (bar.close - yesterday_new_contract_bar.close) / yesterday_new_contract_bar.close
                        gap = bar.open - yesterday_new_contract_bar.close
                    else:
                        day_ret = (bar.close - bar.open) / bar.open if bar.open > 0 else Decimal(0)
                        gap = bar.open - prev_bar.close

                    # 记录累积调整量
                    if self.method == AdjustmentMethod.DIFF:
                        cumulative_diff += gap
                    elif self.method == AdjustmentMethod.RATIO:
                        if prev_bar.close > 0:
                            ratio = bar.open / prev_bar.close
                            cumulative_ratio *= ratio

            adj_open = bar.open
            adj_high = bar.high
            adj_low = bar.low
            adj_close = bar.close
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
                )
            )

            prev_bar = bar
            prev_inst = inst

        return result
