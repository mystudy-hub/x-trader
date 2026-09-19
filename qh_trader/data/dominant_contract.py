"""[Data 适配器] 当时已知主力合约判定与映射 (S4-01, FR-CON-02, FR-CON-04).

核心逻辑：
1. 基于决策时点可见的历史日线持仓量 (open_interest) 与成交量 (volume) 识别主力合约；
2. 引入防假突破机制 (consecutive_days 连续确认天数，默认 2 天)；
3. 严格遵循时间因果律：T 日日盘结束生成的决策，默认在 T+1 日开盘生效 (effective_from)，绝不反向改写 T 日；
4. 产出带版本与有效时间区间的 VersionedValue[InstrumentId]。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from qh_trader.core.clock import utc_timestamp
from qh_trader.core.constants import MissingRuleError
from qh_trader.core.objects import (
    Bar,
    InstrumentId,
    ProductId,
    VersionedValue,
    require_int,
    require_text,
)


@dataclass(frozen=True, slots=True)
class DominantMappingEntry:
    """单条主力合约映射记录."""
    product: ProductId
    instrument: InstrumentId
    trading_day: date
    decision_time: datetime
    effective_from: datetime
    effective_to: datetime | None
    open_interest: int
    volume: int
    version: str


class DominantContractResolver:
    """主力合约映射解析器，维护历史各时段已生效的主力合约版本."""

    def __init__(
        self,
        product: ProductId,
        entries: Sequence[DominantMappingEntry],
        version: str = "v1.0",
    ) -> None:
        self.product = product
        self.version = version
        self._entries = sorted(entries, key=lambda e: e.effective_from)

    @property
    def entries(self) -> tuple[DominantMappingEntry, ...]:
        return tuple(self._entries)

    def dominant(self, product: ProductId, at: datetime) -> VersionedValue[InstrumentId]:
        """按业务时刻查询当前生效的主力合约 (满足 MappingStorePort 契约)."""
        if product != self.product:
            raise MissingRuleError(f"resolver is for {self.product}, requested {product}")
        t = utc_timestamp(at)

        # 查找 effective_from <= t < effective_to 的记录
        active: DominantMappingEntry | None = None
        for entry in self._entries:
            if entry.effective_from <= t:
                if entry.effective_to is None or t < entry.effective_to:
                    active = entry
                    break

        if active is None:
            raise MissingRuleError(f"no dominant contract mapping for {product} at {at} (version {self.version})")

        return VersionedValue(
            value=active.instrument,
            source_id="dominant-contract-resolver",
            version=self.version,
            effective_from=active.effective_from,
            effective_to=active.effective_to,
            available_at=active.decision_time,
        )


def build_dominant_mappings(
    product: ProductId,
    daily_bars: Sequence[Bar],
    *,
    confirm_days: int = 2,
    version: str = "v1.0-auto",
) -> DominantContractResolver:
    """从各合约日线数据推导主力合约映射序列.

    规则：
    - 按交易日分组统计同品种各合约持仓量；
    - 当新合约持仓量连续 confirm_days 天超过当前主力合约时，确认切换主力；
    - 切换在最后一次确认日的下一个自然日/交易日 00:00 (UTC) 生效；
    - 严格保持无未来信息渗透。
    """
    require_int(confirm_days, "confirm_days", 1)
    require_text(version, "version")

    # 按 (trading_day, instrument) 汇总量仓
    # day -> list of (instrument, open_interest, volume)
    bars_by_day: dict[date, list[Bar]] = defaultdict(list)
    for b in daily_bars:
        bars_by_day[b.meta.trading_day].append(b)

    sorted_days = sorted(bars_by_day.keys())
    if not sorted_days:
        return DominantContractResolver(product, (), version=version)

    entries: list[DominantMappingEntry] = []
    current_dominant: InstrumentId | None = None
    challenger: InstrumentId | None = None
    challenger_days = 0

    current_entry_start: datetime | None = None
    current_entry_oi = 0
    current_entry_vol = 0

    for day in sorted_days:
        day_bars = bars_by_day[day]
        # 按 open_interest 降序排序
        top_bar = max(day_bars, key=lambda b: (b.open_interest, b.volume))
        day_leader = top_bar.instrument
        assert isinstance(day_leader, InstrumentId)

        if current_dominant is None:
            # 初始主力
            current_dominant = day_leader
            current_entry_start = top_bar.bar_start
            current_entry_oi = top_bar.open_interest
            current_entry_vol = top_bar.volume
            challenger = None
            challenger_days = 0
            continue

        if day_leader != current_dominant:
            if day_leader == challenger:
                challenger_days += 1
            else:
                challenger = day_leader
                challenger_days = 1

            if challenger_days >= confirm_days:
                # 确认切换！旧主力在次日生效前截止
                # 切换生效时间：当前交易日 end 之后（即下一日开盘）
                switch_time = top_bar.bar_end
                entries.append(
                    DominantMappingEntry(
                        product=product,
                        instrument=current_dominant,
                        trading_day=day,
                        decision_time=top_bar.meta.available_at,
                        effective_from=current_entry_start or top_bar.bar_start,
                        effective_to=switch_time,
                        open_interest=current_entry_oi,
                        volume=current_entry_vol,
                        version=version,
                    )
                )
                current_dominant = challenger
                current_entry_start = switch_time
                current_entry_oi = top_bar.open_interest
                current_entry_vol = top_bar.volume
                challenger = None
                challenger_days = 0
        else:
            # 保持原主力，挑战者计数清零
            challenger = None
            challenger_days = 0
            current_entry_oi = max(current_entry_oi, top_bar.open_interest)
            current_entry_vol += top_bar.volume

    # 封存最后一条主力映射
    if current_dominant is not None and current_entry_start is not None:
        last_day = sorted_days[-1]
        last_bars = bars_by_day[last_day]
        last_bar = max(last_bars, key=lambda b: b.bar_end)
        entries.append(
            DominantMappingEntry(
                product=product,
                instrument=current_dominant,
                trading_day=last_day,
                decision_time=last_bar.meta.available_at,
                effective_from=current_entry_start,
                effective_to=None,  # 持续有效
                open_interest=current_entry_oi,
                volume=current_entry_vol,
                version=version,
            )
        )

    return DominantContractResolver(product, entries, version=version)
