"""[Engine 层] 实盘账户模型：S2 领域内核的暂存 / 发布投影 (S5-04, ADR-X1/X2, FR-LED-01, FR-RISK-01).

账本由事件推导 (FR-LED-01)。本模型把账户事实按顺序保存在 Journal 状态里，每条事实一个键
(``account_fact/<序号>``)，提交只写新增事实，不重写历史；每次 ``publish`` 只对已提交的事实增量应用到
S2 内核 (AccountLedger / PositionManager / OrderManager / RiskManager)；``stage_*`` 一律在私有副本上运行
同一套领域规则，不改已发布内核、不发送、不做 I/O。提交失败时暂存副本被丢弃；重启时从 Journal 完整重建；
已发布事实前缀若与内核不一致即视为损坏，明确失败而不是猜测。

日终检查点 (06 R11)：结算完成、交易日前进的那次暂存把内核状态写成 ``account_checkpoint`` 并在同一事务里
删除已被它覆盖的事实键，此后只重放检查点之后的事实。检查点先经"导出 → 恢复 → 再导出"逐项一致校验，不一致
即放弃压缩 (保留全部事实并记录错误)，不以可疑检查点替代事实。退役规则见 ``engine/account_checkpoint.py``；
最早可比的账本交易日由 ``history_start`` 给出，早于它的日口径明确不可得。旧版整表键 ``account_facts``
须由装配入口显式迁移 (``legacy_fact_migration``)，模型不静默兼容。

事实种类 (每条事实的 ``kind``)：
- ``opened``：账户开立参数 (初始资金、交易日)，只出现一次且在最前；
- ``intent``：已通过风控并预占的最终子单意图 (SUBMIT 命令)；
- ``cancel``：已通过代次与撤单额度检查的撤单请求 (CANCEL 命令)；
- ``send_result`` / ``cancel_result``：本地发送结果；只有明确 ``NOT_SENT`` 才释放预占 (FR-REC-03)；
- ``order_report`` / ``trade``：柜台事实；真实成交不因代次、熔断或资金被丢弃；
- ``settlement_price``：官方结算价 (``EventKind.SETTLEMENT``)；
- ``advance_trading_day``：生命周期推进；缺结算价即 ``SETTLEMENT_PENDING``，禁止新增风险，价格补齐后自动完成；
- ``control``：暂停 / 只减仓 / 恢复 (风控状态机只前进，恢复须显式声明原因消除且账户一致)。

行情 (``EventKind.MARKET_DATA``) 只更新 ``mark_prices`` 键，用于资金估值；不进入事实序列。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from qh_trader.core.constants import (
    EventKind,
    Exchange,
    JournalConflictError,
    MissingRuleError,
    Offset,
    OrderStatus,
    OrderType,
    SendState,
)
from qh_trader.core.event import CanonicalEvent, JournalSnapshot
from qh_trader.core.execution import CommandKind, CommandPlan, ExecutionCommand
from qh_trader.core.objects import (
    Bar,
    ControlEpoch,
    InstrumentId,
    LocalSendResult,
    OrderIdentity,
    OrderIntent,
    OrderUpdate,
    Settlement,
    Tick,
    Trade,
    freeze_payload,
    require_decimal,
    require_text,
)
from qh_trader.domain.ledger import AccountFundsState, AccountLedger, SettlementPendingError
from qh_trader.domain.limits import ExchangeLimits, LimitViolationError
from qh_trader.domain.orders import OrderManager
from qh_trader.domain.positions import PositionManager
from qh_trader.domain.risk import (
    EpochViolationError,
    HolidayRiskHook,
    RiskManager,
    RiskState,
    RiskStateTransitionError,
    RiskViolationError,
)
from qh_trader.engine import account_checkpoint as checkpoints
from qh_trader.engine.base_engine import CommissionSchedule, InstrumentEconomics

LOGGER = logging.getLogger(__name__)

FACT_KEY_PREFIX = "account_fact/"
CHECKPOINT_KEY = "account_checkpoint"
LEGACY_FACTS_KEY = "account_facts"
MARK_PRICES_KEY = "mark_prices"
VIEW_KEY = "account_view"

ADVANCE_TRADING_DAY = "advance_trading_day"
CHECKPOINT_VERSION = 1


class AccountModelCorruptionError(RuntimeError):
    """已发布内核与 Journal 中的事实前缀不一致；不能继续在该内核上交易."""


def fact_key(sequence: int) -> str:
    """事实序号从 1 起全局递增、永不复用；零填充使键的字典序即事实顺序."""
    return f"{FACT_KEY_PREFIX}{sequence:012d}"


def stored_account(
    state: Mapping[str, object],
) -> tuple[Mapping[str, Any] | None, int, tuple[Mapping[str, Any], ...]]:
    """从 Journal 状态读出 (检查点, 检查点覆盖到的事实序号, 其后按序排列的事实)；缺号即损坏."""
    if LEGACY_FACTS_KEY in state:
        raise AccountModelCorruptionError(
            "journal still uses the legacy account_facts layout; run the explicit migration first"
        )
    checkpoint: Mapping[str, Any] | None = state.get(CHECKPOINT_KEY)  # type: ignore[assignment]
    base = 0
    if checkpoint is not None:
        if checkpoint.get("version") != CHECKPOINT_VERSION:
            raise AccountModelCorruptionError(f"unsupported account checkpoint version {checkpoint.get('version')!r}")
        base = checkpoint["through"]
    keys = sorted(key for key in state if key.startswith(FACT_KEY_PREFIX))
    for index, key in enumerate(keys):
        if key != fact_key(base + 1 + index):
            raise AccountModelCorruptionError("account facts are not contiguous after the checkpoint")
    return checkpoint, base, tuple(state[key] for key in keys)  # type: ignore[misc]


def account_facts(state: Mapping[str, object]) -> tuple[Mapping[str, Any], ...]:
    """检查点之后仍保存的事实 (供脚本与测试读取)."""
    return stored_account(state)[2]


def has_account_state(state: Mapping[str, object]) -> bool:
    return LEGACY_FACTS_KEY in state or CHECKPOINT_KEY in state or any(key.startswith(FACT_KEY_PREFIX) for key in state)


def legacy_fact_migration(state: Mapping[str, object]) -> Mapping[str, object] | None:
    """旧版整表 ``account_facts`` → 逐条事实键的一次性状态更新；无需迁移时返回 None."""
    legacy = state.get(LEGACY_FACTS_KEY)
    if legacy is None:
        return None
    if CHECKPOINT_KEY in state or any(key.startswith(FACT_KEY_PREFIX) for key in state):
        raise AccountModelCorruptionError("journal mixes the legacy account_facts layout with per-fact keys")
    updates: dict[str, object] = {LEGACY_FACTS_KEY: None}
    for index, fact in enumerate(legacy):  # type: ignore[arg-type]
        updates[fact_key(index + 1)] = fact
    return updates


def stored_opening(state: Mapping[str, object]) -> AccountOpening | None:
    """持久化的账户开立参数 (检查点或首条 ``opened`` 事实)；尚未开立时返回 None."""
    checkpoint, _, facts = stored_account(state)
    if checkpoint is not None:
        opening = checkpoint["kernel"]["opening"]
        return AccountOpening(opening["initial_capital"], opening["trading_day"])
    for fact in facts:
        if fact.get("kind") == "opened":
            return AccountOpening(fact["initial_capital"], fact["trading_day"])
    return None


def referenced_instruments(state: Mapping[str, object]) -> set[str]:
    """账户状态涉及的全部合约 (用于脚本侧按合约加载经济参数)."""
    checkpoint, _, facts = stored_account(state)
    instruments: set[str] = set()
    for fact in facts:
        if fact.get("kind") == "opened":
            instruments.update(fact.get("economics", {}))
        for key in ("intent", "trade", "update"):
            instrument = getattr(fact.get(key), "instrument", None)
            if instrument is not None:
                instruments.add(str(instrument))
        if fact.get("kind") == "settlement_price":
            instruments.add(str(fact["instrument"]))
    if checkpoint is not None:
        kernel = checkpoint["kernel"]
        instruments.update(kernel.get("economics", {}))
        instruments.update(str(item["instrument"]) for item in kernel["ledger"]["instruments"])
        instruments.update(str(item["instrument"]) for item in kernel["positions"]["positions"])
        instruments.update(str(item["intent"].instrument) for item in kernel["orders"]["orders"])
    return instruments


def stored_economics(state: Mapping[str, object]) -> dict[InstrumentId, InstrumentEconomics] | None:
    """恢复开户事实或日终检查点中的经济参数；仅旧版未声明时返回 None。"""
    checkpoint, _, facts = stored_account(state)
    declaration = (
        checkpoint["kernel"]
        if checkpoint is not None
        else next((fact for fact in facts if fact.get("kind") == "opened"), {})
    )
    if "economics" not in declaration:
        return None
    try:
        values = declaration["economics"]
        if not isinstance(values, Mapping):
            raise TypeError("economics must be a mapping")
        result = {}
        for symbol, raw in values.items():
            exchange, code = symbol.split(".", 1)
            instrument = InstrumentId(Exchange(exchange), code)
            fields = dict(raw)
            schedule = fields.get("commission_schedule")
            if schedule is not None:
                fields["commission_schedule"] = CommissionSchedule(**dict(schedule))
            result[instrument] = InstrumentEconomics(**fields)
        return result
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise AccountModelCorruptionError("persisted account economics cannot be restored") from exc


def order_facts(state: Mapping[str, object]) -> tuple[Mapping[str, Any], ...]:
    """委托意图与本地发送结果的事实形态：检查点中未退役的委托在前，其后是检查点之后的事实.

    柜台委托号簿按"本地已分配并落盘"的原会话三元组重建归属；检查点替代事实前缀后，仍可能收到回报的
    委托 (未退役者) 由检查点里的意图与发送记录补齐，已退役的委托不再参与归属。
    """
    checkpoint, _, facts = stored_account(state)
    restored: list[Mapping[str, Any]] = []
    if checkpoint is not None:
        for item in checkpoint["kernel"]["orders"]["orders"]:
            intent: OrderIntent = item["intent"]
            restored.append({"kind": "intent", "intent": intent})
            restored.extend(
                {
                    "kind": "send_result",
                    "client_order_id": intent.client_order_id,
                    "result": LocalSendResult(attempt["state"], attempt["local_code"], attempt["evidence"]),
                }
                for attempt in item["send_attempts"]
            )
    return tuple(restored) + facts


@dataclass(frozen=True, slots=True)
class AccountOpening:
    """账户开立参数；只在 Journal 尚无 ``opened`` 事实时使用，之后以持久化事实为准."""

    initial_capital: Decimal
    trading_day: date

    def __post_init__(self) -> None:
        require_decimal(self.initial_capital, "initial_capital", Decimal("0"))
        if not isinstance(self.trading_day, date):
            raise TypeError("account opening requires a trading day")


class _Kernel:
    """一组 S2 领域对象；用于已发布内核和暂存副本."""

    def __init__(
        self,
        account_id: str,
        opening: AccountOpening,
        *,
        limits: ExchangeLimits | None,
        holiday_hook: HolidayRiskHook | None,
        holiday_dates: Sequence[date],
        control: ControlEpoch | None,
    ) -> None:
        self.account_id = account_id
        self.opening = opening
        self.ledger = AccountLedger(
            account_id=account_id, initial_capital=opening.initial_capital, trading_day=opening.trading_day
        )
        self.positions: PositionManager = self.ledger.position_manager
        self.orders = OrderManager()
        self.risk = RiskManager(
            account_id,
            control=control,
            limits=limits,
            trading_day=opening.trading_day,
            holiday_hook=holiday_hook,
            holiday_dates=holiday_dates,
        )
        self.settlement_prices: dict[date, dict[InstrumentId, Decimal]] = {}
        self.pending_advance: tuple[date, date, str] | None = None
        # 本方最近成交价：无行情、无结算价时的最后估值回退 (记为估值降级，绝不按 0 估值)
        self.last_trade_prices: dict[InstrumentId, Decimal] = {}
        # 检查点：最早仍完整保存账本条目的交易日 (None 表示自开立起完整)、下一检查点的退役候选、已退役单号
        self.history_start: date | None = None
        self.retire_candidates: frozenset[str] = frozenset()
        self.retired_order_ids: frozenset[str] = frozenset()
        self.restored = False
        self.applied = 0

    @property
    def trading_day(self) -> date:
        return self.ledger.current_trading_day or self.opening.trading_day

    @property
    def settlement_pending(self) -> bool:
        return self.pending_advance is not None


class LiveAccountModel:
    """``ExecutionModelPort`` 的实盘实现；所有方法都必须在执行服务的账户线程上调用."""

    def __init__(
        self,
        account_id: str,
        opening: AccountOpening,
        economics: Mapping[InstrumentId, InstrumentEconomics],
        *,
        limits: ExchangeLimits | None = None,
        holiday_hook: HolidayRiskHook | None = None,
        holiday_dates: Sequence[date] = (),
        now: Callable[[], datetime] | None = None,
    ) -> None:
        require_text(account_id, "account_id")
        if not isinstance(opening, AccountOpening):
            raise TypeError("live account model requires an AccountOpening")
        self.account_id = account_id
        self._opening = opening
        self._economics = dict(economics)
        if any(
            not isinstance(inst, InstrumentId) or not isinstance(eco, InstrumentEconomics)
            for inst, eco in self._economics.items()
        ):
            raise TypeError("live economics requires actual instruments and InstrumentEconomics")
        self._limits = limits
        self._holiday_hook = holiday_hook
        self._holiday_dates = tuple(holiday_dates)
        self._now = now
        self._kernel: _Kernel | None = None
        self._checkpoint: Mapping[str, Any] | None = None
        self._base = 0
        self._facts: tuple[Mapping[str, Any], ...] = ()
        self._mark_prices: dict[InstrumentId, Decimal] = {}
        self._control: ControlEpoch | None = None
        self._poisoned: str | None = None
        self.rebuilds = 0
        self.checkpoints_written = 0
        self.checkpoint_refusals: list[str] = []

    # ------------------------------------------------------------------ 只读视图 (供装配、对账与脚本)
    @property
    def kernel_ready(self) -> bool:
        return self._kernel is not None and self._poisoned is None

    @property
    def ledger(self) -> AccountLedger:
        return self._require_kernel().ledger

    @property
    def positions(self) -> PositionManager:
        return self._require_kernel().positions

    @property
    def orders(self) -> OrderManager:
        return self._require_kernel().orders

    @property
    def risk(self) -> RiskManager:
        return self._require_kernel().risk

    @property
    def trading_day(self) -> date:
        return self._require_kernel().trading_day

    @property
    def settlement_pending(self) -> bool:
        return self._require_kernel().settlement_pending

    @property
    def fact_count(self) -> int:
        """检查点之后仍需重放的事实条数 (日终检查点后归零)."""
        return len(self._facts)

    @property
    def checkpoint_through(self) -> int:
        """检查点覆盖到的事实序号；0 表示尚无检查点."""
        return self._base

    @property
    def history_start(self) -> date | None:
        """最早仍完整保存账本条目的交易日；None 表示自开立起完整."""
        return self._require_kernel().history_start

    @property
    def mark_prices(self) -> Mapping[InstrumentId, Decimal]:
        return dict(self._mark_prices)

    def funds_state(self) -> AccountFundsState:
        return self._funds_state(self._require_kernel())

    def replica(self) -> _Kernel:
        """按已发布检查点与事实重建的一次性副本；恢复对账在副本上合并查询，不触碰已发布内核."""
        kernel = self._new_kernel(self._checkpoint, self._facts)
        self._apply_facts(kernel, self._facts)
        return kernel

    def _require_kernel(self) -> _Kernel:
        if self._poisoned is not None:
            raise AccountModelCorruptionError(self._poisoned)
        if self._kernel is None:
            raise AccountModelCorruptionError("account model has not published a checkpoint yet")
        return self._kernel

    # ------------------------------------------------------------------ ExecutionModelPort
    def publish(self, checkpoint: JournalSnapshot) -> None:
        if checkpoint.account_id != self.account_id:
            raise JournalConflictError("checkpoint belongs to another account")
        stored, base, facts = stored_account(checkpoint.state)
        self._control = None if checkpoint.control_record is None else checkpoint.control_record.epoch
        raw_prices: Mapping[str, Mapping[str, Any]] = checkpoint.state.get(MARK_PRICES_KEY, {})  # type: ignore[assignment]
        self._mark_prices = {entry["instrument"]: entry["price"] for entry in raw_prices.values()}
        rebuild = self._kernel is None or self._poisoned is not None or base != self._base
        if not rebuild and stored != self._checkpoint:
            self._poisoned = "published account checkpoint diverged from the durable journal state"
            raise AccountModelCorruptionError(self._poisoned)
        if rebuild:
            try:
                kernel = self._new_kernel(stored, facts)
            except (checkpoints.CheckpointFormatError, KeyError, TypeError, ValueError) as exc:
                self._poisoned = f"account checkpoint cannot be restored: {type(exc).__name__}"
                raise AccountModelCorruptionError(self._poisoned) from exc
            self._apply_facts(kernel, facts)
            self._kernel = kernel
            self._checkpoint = stored
            self._base = base
            self._poisoned = None
            self.rebuilds += 1
        else:
            applied = self._kernel.applied
            if facts[:applied] != self._facts[:applied]:
                self._poisoned = "published account facts diverged from the durable journal state"
                raise AccountModelCorruptionError(self._poisoned)
            try:
                self._apply_facts(self._kernel, facts[applied:])
            except Exception as exc:
                self._poisoned = f"kernel projection failed: {type(exc).__name__}"
                raise
        self._facts = facts
        if self._control is not None and self._kernel.risk.control != self._control:
            self._kernel.risk.control = self._control

    def stage_command(self, command: ExecutionCommand) -> CommandPlan:
        kernel = self.replica()
        if self._control is not None:
            kernel.risk.control = self._control
        try:
            if command.kind == CommandKind.SUBMIT:
                fact = self._stage_submit(kernel, command)
            elif command.kind == CommandKind.CANCEL:
                fact = self._stage_cancel(kernel, command)
            elif command.kind in (CommandKind.PAUSE, CommandKind.REDUCE_ONLY, CommandKind.RESUME):
                fact = self._stage_control(kernel, command)
            else:
                return CommandPlan(approved=False, reason=f"{command.kind.value} is not supported by the account model")
        except (RiskViolationError, LimitViolationError, EpochViolationError, RiskStateTransitionError) as exc:
            return CommandPlan(approved=False, reason=f"{type(exc).__name__}: {exc}")
        except MissingRuleError as exc:
            return CommandPlan(approved=False, reason=f"MissingRuleError: {exc}")
        except ValueError as exc:
            return CommandPlan(approved=False, reason=f"invalid command: {exc}")
        return CommandPlan(
            approved=True,
            reason=f"{command.kind.value} accepted by the account model",
            state_updates={**self._fact_updates(self._pending(fact)), VIEW_KEY: self._view(kernel)},
        )

    def stage_send_result(self, command: ExecutionCommand, result: LocalSendResult) -> Mapping[str, object]:
        if command.kind == CommandKind.SUBMIT:
            assert isinstance(command.payload, OrderIntent)
            fact = {"kind": "send_result", "client_order_id": command.payload.client_order_id, "result": result}
        elif command.kind == CommandKind.CANCEL:
            assert isinstance(command.payload, OrderIdentity)
            fact = {"kind": "cancel_result", "client_order_id": command.payload.client_order_id, "result": result}
        else:
            return {}
        kernel = self.replica()
        pending = self._pending(fact)
        self._apply_facts(kernel, pending)
        return {**self._fact_updates(pending), VIEW_KEY: self._view(kernel)}

    def stage_fact(self, event: CanonicalEvent) -> Mapping[str, object]:
        payload = event.payload
        if event.kind == EventKind.MARKET_DATA:
            return self._stage_market(payload)
        fact: Mapping[str, Any] | None = None
        if event.kind == EventKind.TRADE_REPORT and isinstance(payload, Trade):
            if payload.account_id != self.account_id:
                raise JournalConflictError("trade belongs to another account")
            if payload.instrument not in self._economics:
                # 真实成交不能丢弃，也不能用猜测的乘数入账：明确失败并保留事件待修复
                raise MissingRuleError(f"no instrument economics registered for {payload.instrument}")
            fact = {"kind": "trade", "trade": payload}
        elif event.kind == EventKind.ORDER_REPORT and isinstance(payload, OrderUpdate):
            fact = {"kind": "order_report", "update": payload}
        elif event.kind == EventKind.SETTLEMENT and isinstance(payload, Settlement):
            fact = {
                "kind": "settlement_price",
                "trading_day": payload.meta.trading_day,
                "instrument": payload.instrument,
                "price": payload.settlement_price,
                "is_final": payload.is_final,
            }
        elif event.kind == EventKind.CONTROL and isinstance(payload, Mapping):
            action = payload.get("action")
            if action == ADVANCE_TRADING_DAY:
                fact = {
                    "kind": ADVANCE_TRADING_DAY,
                    "trading_day": payload["trading_day"],
                    "new_trading_day": payload["new_trading_day"],
                    "version": str(payload.get("version", "v1")),
                }
        if fact is None:
            return {}
        kernel = self.replica()
        day_before = kernel.trading_day
        pending = self._pending(fact)
        self._apply_facts(kernel, pending)
        view = self._view(kernel)
        if kernel.trading_day > day_before and not kernel.settlement_pending:
            # 结算完成、交易日前进：检查点覆盖到本次追加的最后一条事实
            compacted = self._compact(kernel, keep_from=day_before, through=self._next_sequence + len(pending) - 1)
            if compacted is not None:
                return {**compacted, VIEW_KEY: view}
        return {**self._fact_updates(pending), VIEW_KEY: view}

    def opening_fact(self) -> Mapping[str, Any]:
        """账户开立事实；装配在首次启动时持久化，之后重建只认持久化的开立事实."""
        return freeze_payload(
            {
                "kind": "opened",
                "initial_capital": self._opening.initial_capital,
                "trading_day": self._opening.trading_day,
                "economics": self._economics_record(),
            }
        )

    def opening_updates(self) -> Mapping[str, object]:
        """首次启动写入的账户开立事实 (序号 1)."""
        return {fact_key(1): self.opening_fact()}

    @property
    def _next_sequence(self) -> int:
        return self._base + len(self._facts) + 1

    def _pending(self, fact: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
        """待追加的事实；尚无任何持久化事实时先写账户开立参数."""
        frozen = freeze_payload(fact)
        if self._facts or self._checkpoint is not None:
            return (frozen,)
        return (self.opening_fact(), frozen)

    def _fact_updates(self, pending: Sequence[Mapping[str, Any]]) -> dict[str, object]:
        start = self._next_sequence
        return {fact_key(start + index): fact for index, fact in enumerate(pending)}

    # ------------------------------------------------------------------ 日终检查点 (R11)
    def _compact(self, kernel: _Kernel, *, keep_from: date, through: int) -> dict[str, object] | None:
        """把刚完成结算的暂存内核写成检查点，并删除被它覆盖的事实键；校验不通过时返回 None."""
        figures = self._figures(kernel)
        retired, candidates = checkpoints.retire_history(
            kernel.ledger,
            kernel.orders,
            kernel.positions,
            kernel.risk,
            keep_from=keep_from,
            candidates=kernel.retire_candidates,
        )
        kernel.retired_order_ids = kernel.retired_order_ids | retired
        kernel.retire_candidates = candidates
        kernel.history_start = keep_from
        reason: str | None = None
        try:
            data = freeze_payload(self._dump_kernel(kernel))
            restored = self._restore_kernel(data)
            if freeze_payload(self._dump_kernel(restored)) != data:
                reason = "restored kernel differs from the checkpoint it was built from"
            elif self._figures(kernel) != figures:
                reason = "retiring settled history changed current account figures"
            elif self._figures(restored) != figures:
                reason = "restored kernel reports different account figures"
        except (checkpoints.CheckpointFormatError, KeyError, TypeError, ValueError) as exc:
            reason = f"checkpoint cannot be restored: {type(exc).__name__}: {exc}"
        if reason is not None:
            # 放弃压缩只影响性能、不影响正确性：事实保持原样，错误留给运维处理
            self.checkpoint_refusals.append(reason)
            LOGGER.error("account checkpoint refused; keeping all account facts: %s", reason)
            return None
        self.checkpoints_written += 1
        updates: dict[str, object] = {
            CHECKPOINT_KEY: {
                "version": CHECKPOINT_VERSION,
                "through": through,
                "trading_day": kernel.trading_day,
                "kernel": data,
            }
        }
        for sequence in range(self._base + 1, self._next_sequence):
            updates[fact_key(sequence)] = None
        return updates

    def _figures(self, kernel: _Kernel) -> tuple[object, ...]:
        """当前账户口径 (资金、持仓、预占、活动委托、风控状态)；退役与恢复都不得改变它们."""
        return (
            kernel.trading_day,
            self._funds_state(kernel),
            tuple(position.to_position_snapshot() for position in kernel.positions.all_positions()),
            tuple(sorted(order.client_order_id for order in kernel.orders.active_orders())),
            kernel.risk.risk_state,
            kernel.settlement_pending,
        )

    def _dump_kernel(self, kernel: _Kernel) -> dict[str, Any]:
        start = kernel.history_start
        return {
            "economics": self._economics_record(),
            "opening": {"initial_capital": kernel.opening.initial_capital, "trading_day": kernel.opening.trading_day},
            "ledger": checkpoints.dump_ledger(kernel.ledger),
            "positions": checkpoints.dump_positions(kernel.positions),
            "orders": checkpoints.dump_orders(kernel.orders),
            "risk": checkpoints.dump_risk(kernel.risk),
            "settlement_prices": tuple(
                {"trading_day": day, "instrument": instrument, "price": price}
                for day, prices in kernel.settlement_prices.items()
                if start is None or day >= start
                for instrument, price in prices.items()
            ),
            "pending_advance": None
            if kernel.pending_advance is None
            else dict(zip(("trading_day", "new_trading_day", "version"), kernel.pending_advance, strict=True)),
            "last_trade_prices": tuple(
                {"instrument": instrument, "price": price} for instrument, price in kernel.last_trade_prices.items()
            ),
            "history_start": start,
            "retire_candidates": tuple(sorted(kernel.retire_candidates)),
            "retired_order_ids": tuple(sorted(kernel.retired_order_ids)),
        }

    def _restore_kernel(self, data: Mapping[str, Any]) -> _Kernel:
        self._validate_economics_record(data.get("economics"))
        opening = AccountOpening(data["opening"]["initial_capital"], data["opening"]["trading_day"])
        kernel = _Kernel(
            self.account_id,
            opening,
            limits=self._limits,
            holiday_hook=self._holiday_hook,
            holiday_dates=self._holiday_dates,
            control=self._control,
        )
        checkpoints.load_ledger(data["ledger"], kernel.ledger)
        checkpoints.load_positions(data["positions"], kernel.positions)
        checkpoints.load_orders(data["orders"], kernel.orders)
        checkpoints.load_risk(data["risk"], kernel.risk)
        kernel.settlement_prices = {}
        for item in data["settlement_prices"]:
            kernel.settlement_prices.setdefault(item["trading_day"], {})[item["instrument"]] = item["price"]
        advance = data["pending_advance"]
        kernel.pending_advance = (
            None if advance is None else (advance["trading_day"], advance["new_trading_day"], advance["version"])
        )
        kernel.last_trade_prices = {item["instrument"]: item["price"] for item in data["last_trade_prices"]}
        kernel.history_start = data["history_start"]
        kernel.retire_candidates = frozenset(data["retire_candidates"])
        kernel.retired_order_ids = frozenset(data["retired_order_ids"])
        kernel.restored = True
        return kernel

    # ------------------------------------------------------------------ 暂存：命令
    def _stage_submit(self, kernel: _Kernel, command: ExecutionCommand) -> Mapping[str, Any]:
        intent = command.payload
        assert isinstance(intent, OrderIntent)
        if (
            kernel.orders.get_order(intent.client_order_id) is not None
            or intent.client_order_id in kernel.retired_order_ids
        ):
            raise ValueError(f"duplicate client_order_id {intent.client_order_id}")
        if kernel.settlement_pending and intent.offset == Offset.OPEN:
            raise RiskViolationError("settlement is pending; new risk is not allowed until the day is settled")
        if intent.offset != Offset.OPEN and intent.offset not in (Offset.CLOSE_TODAY, Offset.CLOSE_YESTERDAY):
            raise ValueError("SUBMIT must carry a final child order with an explicit close bucket")
        economics = self._economics_for(intent.instrument)
        margin, fee = self._estimate_reservation(kernel, intent, economics)
        now = command.submitted_at if self._now is None else self._now()
        kernel.risk.check_and_reserve(
            intent,
            command.control,
            kernel.ledger,
            kernel.positions,
            self._live_orders(kernel, exclude=intent.client_order_id),
            margin,
            fee,
            kernel.trading_day,
            funds=self._funds_state(kernel),
            now=now,
        )
        # 副本上的预占只用于本次检查；持久化事实在 publish 时按同一顺序重新应用
        kernel.positions.release_reservation(intent.client_order_id)
        kernel.ledger.release_funds(intent.client_order_id)
        fact = {
            "kind": "intent",
            "command_id": command.command_id,
            "intent": intent,
            "margin": margin,
            "fee": fee,
            "control": command.control,
        }
        self._apply_facts(kernel, self._pending(fact))
        return fact

    def _stage_cancel(self, kernel: _Kernel, command: ExecutionCommand) -> Mapping[str, Any]:
        identity = command.payload
        assert isinstance(identity, OrderIdentity)
        if not identity.client_order_id:
            raise ValueError("cancel requires the local client_order_id of the order")
        order = kernel.orders.get_order(identity.client_order_id)
        if order is None:
            raise ValueError(f"unknown order {identity.client_order_id}")
        if order.cancel_pending:
            raise ValueError(f"cancel already pending for {identity.client_order_id}")
        kernel.risk.check_cancel_command(order, command.control, kernel.trading_day)
        fact = {"kind": "cancel", "command_id": command.command_id, "client_order_id": order.client_order_id}
        self._apply_facts(kernel, self._pending(fact))
        return fact

    def _stage_control(self, kernel: _Kernel, command: ExecutionCommand) -> Mapping[str, Any]:
        payload = command.payload
        assert isinstance(payload, Mapping)
        reason = str(payload.get("reason", "")).strip()
        if not reason:
            raise ValueError("control commands must state a reason")
        kernel.risk.check_command_epoch(command.control, command.kind.value.lower())
        fact: dict[str, Any] = {
            "kind": "control",
            "command_id": command.command_id,
            "action": command.kind.value,
            "reason": reason,
            "at": command.submitted_at,
        }
        if command.kind == CommandKind.RESUME:
            cleared = payload.get("cause_cleared")
            consistent = payload.get("account_consistent")
            if cleared is not True or consistent is not True:
                raise RiskStateTransitionError("resume requires cause_cleared=true and account_consistent=true")
            fact["cause_cleared"] = True
            fact["account_consistent"] = True
        self._apply_facts(kernel, self._pending(fact))
        return fact

    # ------------------------------------------------------------------ 暂存：行情
    def _stage_market(self, payload: object) -> Mapping[str, object]:
        if isinstance(payload, Bar) and isinstance(payload.instrument, InstrumentId):
            instrument, price = payload.instrument, payload.close
        elif isinstance(payload, Tick) and payload.last_price is not None:
            instrument, price = payload.instrument, payload.last_price
        else:
            return {}
        prices = dict(self._mark_prices)
        prices[instrument] = price
        return {MARK_PRICES_KEY: {str(inst): {"instrument": inst, "price": px} for inst, px in prices.items()}}

    # ------------------------------------------------------------------ 内核构建与事实应用
    def _new_kernel(self, checkpoint: Mapping[str, Any] | None, facts: Sequence[Mapping[str, Any]]) -> _Kernel:
        if checkpoint is not None:
            return self._restore_kernel(checkpoint["kernel"])
        opening = self._opening
        for fact in facts:
            if fact.get("kind") == "opened":
                opening = AccountOpening(fact["initial_capital"], fact["trading_day"])
                break
        return _Kernel(
            self.account_id,
            opening,
            limits=self._limits,
            holiday_hook=self._holiday_hook,
            holiday_dates=self._holiday_dates,
            control=self._control,
        )

    def _apply_facts(self, kernel: _Kernel, facts: Sequence[Mapping[str, Any]]) -> None:
        for fact in facts:
            self._apply_fact(kernel, fact)
            kernel.applied += 1

    def _apply_fact(self, kernel: _Kernel, fact: Mapping[str, Any]) -> None:
        kind = fact["kind"]
        if kind == "opened":
            if kernel.applied != 0 or kernel.restored:
                raise AccountModelCorruptionError("account opening fact must be the first fact")
            opened = AccountOpening(fact["initial_capital"], fact["trading_day"])
            if opened != kernel.opening:
                raise AccountModelCorruptionError("account opening fact differs from the kernel opening")
            self._validate_economics_record(fact.get("economics"))
            return
        if kind == "intent":
            intent: OrderIntent = fact["intent"]
            kernel.orders.create_order(intent)
            # 开仓也登记预占 (不冻结持仓)，使在途开仓计入持仓限额 (与 check_and_reserve 口径一致)
            kernel.positions.reserve_for_order(
                intent.client_order_id, intent.instrument, intent.side, intent.offset, intent.quantity
            )
            kernel.ledger.reserve_funds(intent.client_order_id, fact["margin"], fact["fee"])
            control: ControlEpoch = fact["control"]
            created = kernel.orders.get_order(intent.client_order_id)
            assert created is not None
            created.mark_submitting(control.epoch)
            return
        if kind == "cancel":
            order = kernel.orders.get_order(fact["client_order_id"])
            if order is None:
                raise AccountModelCorruptionError("cancel fact references an unknown order")
            order.request_cancel()
            return
        if kind == "send_result":
            result: LocalSendResult = fact["result"]
            order = kernel.orders.record_send_result(fact["client_order_id"], result)
            if result.state == SendState.NOT_SENT:
                kernel.positions.release_reservation(order.client_order_id)
                kernel.ledger.release_funds(order.client_order_id)
            return
        if kind == "cancel_result":
            result = fact["result"]
            order = kernel.orders.get_order(fact["client_order_id"])
            if order is not None and result.state == SendState.NOT_SENT and not order.is_terminal:
                order.cancel_rejected(result.evidence)
            return
        if kind == "order_report":
            self._apply_order_report(kernel, fact["update"])
            return
        if kind == "trade":
            self._apply_trade(kernel, fact["trade"])
            return
        if kind == "settlement_price":
            day: date = fact["trading_day"]
            kernel.settlement_prices.setdefault(day, {})[fact["instrument"]] = fact["price"]
            kernel.ledger.set_pre_settlement_price(fact["instrument"], fact["price"])
            self._try_advance(kernel)
            return
        if kind == ADVANCE_TRADING_DAY:
            kernel.pending_advance = (fact["trading_day"], fact["new_trading_day"], fact["version"])
            self._try_advance(kernel)
            return
        if kind == "control":
            self._apply_control(kernel, fact)
            return
        raise AccountModelCorruptionError(f"unknown account fact kind {kind!r}")

    def _apply_order_report(self, kernel: _Kernel, update: OrderUpdate) -> None:
        order = kernel.orders.process_order_update(update)
        if order is None:
            return
        day = kernel.trading_day
        if update.status == OrderStatus.ACCEPTED:
            kernel.risk.on_order_accepted(order.intent, day)
        elif update.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
            unfilled = order.quantity - order.cum_filled_qty
            if update.status == OrderStatus.CANCELLED:
                kernel.risk.on_order_canceled(order.intent, unfilled, day)
            elif update.status == OrderStatus.REJECTED:
                kernel.risk.on_order_rejected(order.intent, was_accepted=False, trading_day=day)
            kernel.positions.on_order_canceled_or_rejected(order.client_order_id, order.cum_filled_qty)
            fraction = Decimal(order.unaccounted_fill_qty) / Decimal(order.quantity)
            kernel.ledger.release_funds(order.client_order_id, fraction)

    def _apply_trade(self, kernel: _Kernel, trade: Trade) -> None:
        order, is_new = kernel.orders.process_trade(trade)
        if not is_new:
            return
        economics = self._economics_for(trade.instrument)
        commission = economics.commission(trade.price, trade.quantity, trade.offset)
        client_order_id = order.client_order_id if order is not None else None
        kernel.last_trade_prices[trade.instrument] = trade.price
        kernel.ledger.on_trade(
            trade, commission=commission, multiplier=economics.multiplier, client_order_id=client_order_id
        )
        if order is not None and order.is_terminal and order.unaccounted_fill_qty == 0:
            kernel.positions.on_order_canceled_or_rejected(order.client_order_id, order.cum_filled_qty)
            kernel.ledger.release_funds(order.client_order_id)

    def _try_advance(self, kernel: _Kernel) -> None:
        if kernel.pending_advance is None:
            return
        settle_day, new_day, version = kernel.pending_advance
        current = kernel.ledger.current_trading_day
        if new_day <= settle_day or (current is not None and new_day < current):
            kernel.pending_advance = None
            raise AccountModelCorruptionError("trading day cannot move backwards")
        if current is not None and current == new_day:
            # 重复的日终任务：不重复结算、不重复转换持仓 (A26)
            kernel.pending_advance = None
            return
        prices = kernel.settlement_prices.get(settle_day, {})
        try:
            kernel.ledger.settle_day(prices, new_day, trading_day=settle_day, version=version)
        except SettlementPendingError:
            return
        kernel.risk.reset_daily_counters(new_day)
        kernel.pending_advance = None

    @staticmethod
    def _apply_control(kernel: _Kernel, fact: Mapping[str, Any]) -> None:
        action = fact["action"]
        at = fact.get("at")
        if action == CommandKind.PAUSE.value:
            kernel.risk.escalate(fact["reason"], target=RiskState.HALTED, at=at)
        elif action == CommandKind.REDUCE_ONLY.value:
            kernel.risk.escalate(fact["reason"], target=RiskState.REDUCE_ONLY, at=at)
        elif action == CommandKind.RESUME.value:
            kernel.risk.reset_risk_state(
                cause_cleared=fact.get("cause_cleared") is True,
                account_consistent=fact.get("account_consistent") is True,
                reason=fact["reason"],
                at=at,
            )
        else:
            raise AccountModelCorruptionError(f"unknown control action {action!r}")

    # ------------------------------------------------------------------ 估值与辅助
    def _economics_record(self) -> Mapping[str, object]:
        return {str(inst): eco.as_dict() for inst, eco in sorted(self._economics.items(), key=lambda row: str(row[0]))}

    def _validate_economics_record(self, declared: object) -> None:
        if declared is None:
            if any(eco.commission_schedule is not None for eco in self._economics.values()):
                raise AccountModelCorruptionError(
                    "legacy account has no persisted commission schedule; explicit economics migration is required"
                )
            return
        if not isinstance(declared, Mapping) or freeze_payload(declared) != freeze_payload(self._economics_record()):
            raise AccountModelCorruptionError("configured economics differs from the persisted account declaration")

    def _economics_for(self, instrument: InstrumentId) -> InstrumentEconomics:
        economics = self._economics.get(instrument)
        if economics is None:
            raise MissingRuleError(f"no instrument economics registered for {instrument}")
        return economics

    def _estimate_reservation(
        self, kernel: _Kernel, intent: OrderIntent, economics: InstrumentEconomics
    ) -> tuple[Decimal, Decimal]:
        if intent.order_type == OrderType.LIMIT and intent.limit_price_ticks is not None:
            price: Decimal | None = Decimal(intent.limit_price_ticks) * economics.price_tick
        else:
            price = self._mark_prices.get(intent.instrument)
            if price is None:
                instrument_ledger = kernel.ledger.get_instrument_ledger(intent.instrument, economics.multiplier)
                price = instrument_ledger.pre_settlement_price
            if price is None:
                raise MissingRuleError(f"no visible reference price to reserve funds for {intent.instrument}")
        assert price is not None
        margin = Decimal("0")
        if intent.offset == Offset.OPEN:
            margin = price * economics.multiplier * Decimal(intent.quantity) * economics.margin_ratio
        fee = economics.commission(price, intent.quantity, intent.offset)
        return margin, fee

    def _valuation_prices(self, kernel: _Kernel) -> tuple[dict[InstrumentId, Decimal], tuple[InstrumentId, ...]]:
        """估值价回退：行情标记价 → 官方结算价 → 本方最近成交价 (降级)；没有任何价格时不估值 (FR-LED-09)."""
        prices = dict(self._mark_prices)
        degraded: list[InstrumentId] = []
        for instrument, ledger in kernel.ledger._instrument_ledgers.items():  # noqa: SLF001 - 只读估值
            if instrument in prices or ledger.pre_settlement_price is not None:
                continue
            last = kernel.last_trade_prices.get(instrument)
            if last is not None:
                prices[instrument] = last
                degraded.append(instrument)
        return prices, tuple(degraded)

    def _funds_state(self, kernel: _Kernel) -> AccountFundsState:
        rates = {inst: eco.margin_ratio for inst, eco in self._economics.items()}
        prices, _ = self._valuation_prices(kernel)
        return kernel.ledger.get_funds_state(current_prices=prices, margin_rates=rates)

    @staticmethod
    def _live_orders(kernel: _Kernel, *, exclude: str | None = None) -> list:
        return [order for order in kernel.orders.active_orders() if order.client_order_id != exclude]

    def _view(self, kernel: _Kernel) -> Mapping[str, object]:
        funds = self._funds_state(kernel)
        _, degraded = self._valuation_prices(kernel)
        return {
            "valuation_degraded": tuple(str(instrument) for instrument in degraded),
            "trading_day": kernel.trading_day,
            "balance": kernel.ledger.balance,
            "total_equity": funds.total_equity,
            "margin_used": funds.margin_used,
            "frozen_margin": funds.frozen_margin,
            "total_commission": kernel.ledger.total_commission,
            "realized_mtm_pnl": kernel.ledger.realized_mtm_pnl,
            "realized_trade_pnl": kernel.ledger.realized_trade_pnl,
            "risk_state": kernel.risk.risk_state.value,
            "settlement_pending": kernel.settlement_pending,
            "history_start": kernel.history_start,
            "active_orders": len(kernel.orders.active_orders()),
            "positions": tuple(
                position.to_position_snapshot()
                for position in kernel.positions.all_positions()
                if position.total_position or position.total_frozen
            ),
        }
