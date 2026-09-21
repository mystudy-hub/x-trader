"""[Data 适配器] 当时已知主力合约判定与映射 (S4-01, FR-CON-02, FR-CON-04).

核心逻辑：
1. 基于决策时点可见的历史日线持仓量 (open_interest) 与成交量 (volume) 识别主力合约；
2. 引入防假突破机制 (consecutive_days 连续确认天数，默认 2 天)；
3. 严格遵循时间因果律：T 日日盘结束生成的决策 (`decision_time`)，默认不早于 T+1 交易日首个可交易时段生效
   (`effective_from`)；绝不反向改写 T 日；
4. 产出带版本与有效时间区间的 VersionedValue[InstrumentId]。

`effective_from` 的来源 (写入映射记录 `effective_basis`)：
- ``session_gate``：调用方提供的版本化时段查询给出 T 日收盘后的下一个可撮合时段开始时刻；
- ``next_observed_bar``：无时段查询时，用同品种下一交易日首根可观测 Bar 的开始时刻；
- ``decision_time``：覆盖区间最后一日没有后续 Bar，只能记为决策时刻 (此时不再有后续切换)。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime

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

SessionOpenAfter = Callable[[datetime], datetime | None]


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
    effective_basis: str = "decision_time"


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

        active: DominantMappingEntry | None = None
        for entry in self._entries:
            if entry.effective_from <= t and (entry.effective_to is None or t < entry.effective_to):
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
    session_open_after: SessionOpenAfter | None = None,
) -> DominantContractResolver:
    """从各合约日线数据推导主力合约映射序列.

    规则：
    - 按交易日分组统计同品种各合约持仓量；
    - 当新合约持仓量连续 confirm_days 天超过当前主力合约时，确认切换主力；
    - 切换在确认日 T 收盘后的下一个可交易时段生效 (FR-CON-02)：优先由 ``session_open_after`` 给出，
      否则取同品种 T+1 交易日首根可观测 Bar 的开始时刻；
    - 严格保持无未来信息渗透。
    """
    require_int(confirm_days, "confirm_days", 1)
    require_text(version, "version")

    bars_by_day: dict[date, list[Bar]] = defaultdict(list)
    for b in daily_bars:
        bars_by_day[b.meta.trading_day].append(b)

    sorted_days = sorted(bars_by_day.keys())
    if not sorted_days:
        return DominantContractResolver(product, (), version=version)

    next_day_start: dict[date, datetime] = {}
    for current, following in zip(sorted_days, sorted_days[1:], strict=False):
        next_day_start[current] = min(b.bar_start for b in bars_by_day[following])

    def switch_effective(day: date, decision_bar: Bar) -> tuple[datetime, str]:
        if session_open_after is not None:
            target = session_open_after(decision_bar.bar_end)
            if target is not None:
                return utc_timestamp(target), "session_gate"
        following = next_day_start.get(day)
        if following is not None:
            return following, "next_observed_bar"
        return decision_bar.bar_end, "decision_time"

    entries: list[DominantMappingEntry] = []
    current_dominant: InstrumentId | None = None
    challenger: InstrumentId | None = None
    challenger_days = 0

    current_entry_start: datetime | None = None
    current_entry_basis = "first_observation"
    current_entry_oi = 0
    current_entry_vol = 0

    for day in sorted_days:
        day_bars = bars_by_day[day]
        top_bar = max(day_bars, key=lambda b: (b.open_interest, b.volume))
        day_leader = top_bar.instrument
        assert isinstance(day_leader, InstrumentId)

        if current_dominant is None:
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
                switch_time, basis = switch_effective(day, top_bar)
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
                        effective_basis=current_entry_basis,
                    )
                )
                current_dominant = challenger
                current_entry_start = switch_time
                current_entry_basis = basis
                current_entry_oi = top_bar.open_interest
                current_entry_vol = top_bar.volume
                challenger = None
                challenger_days = 0
        else:
            challenger = None
            challenger_days = 0
            current_entry_oi = max(current_entry_oi, top_bar.open_interest)
            current_entry_vol += top_bar.volume

    if current_dominant is not None and current_entry_start is not None:
        last_day = sorted_days[-1]
        last_bar = max(bars_by_day[last_day], key=lambda b: b.bar_end)
        entries.append(
            DominantMappingEntry(
                product=product,
                instrument=current_dominant,
                trading_day=last_day,
                decision_time=last_bar.meta.available_at,
                effective_from=current_entry_start,
                effective_to=None,
                open_interest=current_entry_oi,
                volume=current_entry_vol,
                version=version,
                effective_basis=current_entry_basis,
            )
        )

    return DominantContractResolver(product, entries, version=version)
