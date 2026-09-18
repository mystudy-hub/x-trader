"""[Domain 层] 交易所硬约束与版本化限额规则 (S2-05, FR-RISK-02, FR-RISK-03, A19).

核心约束:
1. 三类规则分别管理 (LimitSource): 交易所及监管 / 期货公司及柜台 / 内部风险偏好，每条规则带来源证据。
2. 不设置无来源的生产默认值 (FR-RISK-02): 某维度没有配置规则就不做该维度检查，
   调用方可通过 ``missing_limits`` 要求显式配置。
3. 规则按 (合约或品种, 交易日) 版本化生效；合约阶段递减的持仓限额用多条不同生效日的规则表达。
4. 交割月资格: 自然人客户进入交割月不得开仓 (由 ContractSpec 交割年月或显式 ``no_open_from`` 驱动)。
5. 涨跌停价格带: 限价必须落在 [lower_limit, upper_limit] 内，适用于所有委托 (含平仓)。
6. 回测与实盘共用同一套硬约束逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from qh_trader.core.constants import AmbiguousRuleError, Offset
from qh_trader.core.objects import ContractSpec, InstrumentId, OrderIntent


class LimitViolationError(ValueError):
    """违反交易所硬约束异常."""


class LimitSource(StrEnum):
    """三类规则来源 (FR-RISK-02)."""

    EXCHANGE = "EXCHANGE"
    BROKER = "BROKER"
    INTERNAL = "INTERNAL"


class LimitKind(StrEnum):
    MAX_OPEN_LOTS_PER_DAY = "MAX_OPEN_LOTS_PER_DAY"
    MAX_POSITION_LOTS = "MAX_POSITION_LOTS"
    MAX_CANCELS_PER_DAY = "MAX_CANCELS_PER_DAY"
    # 从某日起禁止开仓 (交割月资格)；value 为 date
    NO_OPEN_FROM = "NO_OPEN_FROM"


def product_code_of(instrument: InstrumentId) -> str:
    """提取英文品种代码 (如 SHFE.rb2410 -> rb)."""
    return "".join(c for c in instrument.symbol if c.isalpha()).lower()


def normalize_scope(scope: str) -> str:
    return scope.strip().lower()


@dataclass(frozen=True, slots=True)
class LimitRule:
    """版本化限额规则.

    scope: 合约 ("SHFE.rb2410") 或品种 ("rb")，大小写不敏感；合约级规则优先于品种级。
    value: 数量类规则为 int；NO_OPEN_FROM 为 date。
    """

    kind: LimitKind
    scope: str
    value: int | date
    source: LimitSource
    effective_from: date
    evidence_ref: str
    effective_to: date | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, LimitKind):
            raise TypeError("kind must be a LimitKind")
        if not isinstance(self.source, LimitSource):
            raise TypeError("source must be a LimitSource")
        if not self.scope or not self.scope.strip():
            raise ValueError("scope must be nonempty")
        if not self.evidence_ref or not self.evidence_ref.strip():
            raise ValueError("limit rule requires an evidence_ref (FR-RISK-02: no unsourced limits)")
        if not isinstance(self.effective_from, date):
            raise TypeError("effective_from must be a date")
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be after effective_from (left-closed, right-open)")
        if self.kind == LimitKind.NO_OPEN_FROM:
            if not isinstance(self.value, date):
                raise TypeError("NO_OPEN_FROM rule value must be a date")
        elif isinstance(self.value, bool) or not isinstance(self.value, int) or self.value < 0:
            raise TypeError(f"{self.kind} rule value must be a non-negative integer")

    def effective_on(self, trading_day: date) -> bool:
        return self.effective_from <= trading_day and (self.effective_to is None or trading_day < self.effective_to)


class ExchangeLimits:
    """交易所硬约束规则集 (无默认兜底值)."""

    def __init__(self, rules: list[LimitRule] | tuple[LimitRule, ...] | None = None) -> None:
        self._rules: list[LimitRule] = list(rules or ())

    def add_rule(self, rule: LimitRule) -> None:
        self._rules.append(rule)

    @property
    def rules(self) -> tuple[LimitRule, ...]:
        return tuple(self._rules)

    # ------------------------------------------------------------------ 查询
    def find_rule(self, kind: LimitKind, instrument: InstrumentId, trading_day: date) -> LimitRule | None:
        """选出在 trading_day 生效的规则：合约级优先于品种级；同级取最新生效；同日冲突视为歧义."""
        inst_key = normalize_scope(str(instrument))
        prod_key = product_code_of(instrument)
        for key in (inst_key, prod_key):
            candidates = [
                r
                for r in self._rules
                if r.kind == kind and normalize_scope(r.scope) == key and r.effective_on(trading_day)
            ]
            if not candidates:
                continue
            latest = max(r.effective_from for r in candidates)
            winners = [r for r in candidates if r.effective_from == latest]
            if len(winners) > 1:
                raise AmbiguousRuleError(
                    f"{len(winners)} {kind} rules for {key} share effective_from={latest}; "
                    "rules must be versioned unambiguously"
                )
            return winners[0]
        return None

    def _int_limit(self, kind: LimitKind, instrument: InstrumentId, trading_day: date) -> int | None:
        rule = self.find_rule(kind, instrument, trading_day)
        return None if rule is None else int(rule.value)

    def get_max_open_lots(self, instrument: InstrumentId, trading_day: date) -> int | None:
        return self._int_limit(LimitKind.MAX_OPEN_LOTS_PER_DAY, instrument, trading_day)

    def get_max_position_lots(self, instrument: InstrumentId, trading_day: date) -> int | None:
        return self._int_limit(LimitKind.MAX_POSITION_LOTS, instrument, trading_day)

    def get_max_cancels(self, instrument: InstrumentId, trading_day: date) -> int | None:
        return self._int_limit(LimitKind.MAX_CANCELS_PER_DAY, instrument, trading_day)

    def get_no_open_from(self, instrument: InstrumentId, trading_day: date) -> date | None:
        rule = self.find_rule(LimitKind.NO_OPEN_FROM, instrument, trading_day)
        return None if rule is None else rule.value  # type: ignore[return-value]

    def missing_limits(
        self,
        instrument: InstrumentId,
        trading_day: date,
        required: tuple[LimitKind, ...] = (
            LimitKind.MAX_OPEN_LOTS_PER_DAY,
            LimitKind.MAX_POSITION_LOTS,
            LimitKind.MAX_CANCELS_PER_DAY,
        ),
    ) -> tuple[LimitKind, ...]:
        """返回该合约在该交易日没有生效规则的维度，供调用方要求显式配置."""
        return tuple(kind for kind in required if self.find_rule(kind, instrument, trading_day) is None)

    # ------------------------------------------------------------------ 检查
    def check_price_band(self, order: OrderIntent, lower_limit_ticks: int, upper_limit_ticks: int) -> None:
        """涨跌停价格带：限价必须落在 [lower, upper]，对开仓与平仓一律适用；市价单无价格不检查."""
        if lower_limit_ticks > upper_limit_ticks:
            raise ValueError(f"invalid price band: lower={lower_limit_ticks} > upper={upper_limit_ticks}")
        price = order.limit_price_ticks
        if price is None:
            return
        if price < lower_limit_ticks or price > upper_limit_ticks:
            raise LimitViolationError(
                f"limit price out of price band for {order.instrument}: price={price}, "
                f"band=[{lower_limit_ticks}, {upper_limit_ticks}]"
            )

    def check_delivery_month(
        self,
        order: OrderIntent,
        trading_day: date,
        contract_spec: ContractSpec | None = None,
        natural_person: bool = False,
    ) -> None:
        """交割月资格：自然人账户进入交割月不得开仓；显式 NO_OPEN_FROM 规则对所有账户生效."""
        if order.offset != Offset.OPEN:
            return
        inst = order.instrument
        no_open_from = self.get_no_open_from(inst, trading_day)
        if no_open_from is not None and trading_day >= no_open_from:
            raise LimitViolationError(
                f"open forbidden for {inst} from {no_open_from} (delivery eligibility rule), trading_day={trading_day}"
            )
        if natural_person and contract_spec is not None:
            if contract_spec.instrument != inst:
                raise ValueError(f"contract spec {contract_spec.instrument} does not match order {inst}")
            delivery_start = date(contract_spec.delivery_year, contract_spec.delivery_month, 1)
            if trading_day >= delivery_start:
                raise LimitViolationError(
                    f"natural-person account cannot open {inst} in delivery month "
                    f"{contract_spec.delivery_year}-{contract_spec.delivery_month:02d}, trading_day={trading_day}"
                )

    def check_order(
        self,
        order: OrderIntent,
        trading_day: date,
        current_open_lots_today: int,
        current_holding_lots: int,
        price_band: tuple[int, int] | None = None,
        contract_spec: ContractSpec | None = None,
        natural_person: bool = False,
    ) -> None:
        """检查委托是否违反硬约束. 若违反则抛出 LimitViolationError.

        价格带对所有委托适用；开仓限额、持仓限额与交割月资格只对开仓适用。
        某维度没有生效规则时跳过该维度 (由调用方通过 missing_limits 决定是否强制配置)。
        """
        inst = order.instrument

        if price_band is not None:
            self.check_price_band(order, price_band[0], price_band[1])

        if order.offset != Offset.OPEN:
            return

        self.check_delivery_month(order, trading_day, contract_spec, natural_person)

        limit_open = self.get_max_open_lots(inst, trading_day)
        if limit_open is not None and current_open_lots_today + order.quantity > limit_open:
            raise LimitViolationError(
                f"daily open limit exceeded for {inst}: current={current_open_lots_today}, "
                f"order={order.quantity}, limit={limit_open}"
            )

        limit_pos = self.get_max_position_lots(inst, trading_day)
        if limit_pos is not None and current_holding_lots + order.quantity > limit_pos:
            raise LimitViolationError(
                f"position limit exceeded for {inst}: current={current_holding_lots}, "
                f"order={order.quantity}, limit={limit_pos}"
            )

    def check_cancel(self, instrument: InstrumentId, trading_day: date, current_cancels_today: int) -> None:
        """检查撤单是否超限 (无规则时不检查)."""
        limit_cancels = self.get_max_cancels(instrument, trading_day)
        if limit_cancels is not None and current_cancels_today >= limit_cancels:
            raise LimitViolationError(
                f"cancel limit reached for {instrument}: current={current_cancels_today}, limit={limit_cancels}"
            )
