"""[Domain 层] 恢复协调器、查询合并与状态对账 (S2-08, FR-REC-04, FR-ORD-07, A04).

恢复协议: DISCONNECTED -> RECOVERING -> RECONCILING -> READY

1. RECOVERING: 恢复持久化控制记录与已提交快照，重放快照之后的事件 (去重保证不重复记账)，
   将无法确认远端结果的活动订单标记为未知 (SENT_UNKNOWN) 并进入对账。
2. RECONCILING: 按查询批次合并订单、成交、持仓与资金。每个批次保存查询范围、交易日、
   完成标志与合并水位；未完成 (complete=False) 或有错误码的查询不视为完整快照；
   跨交易日混合的查询不能发布为可交易快照。
3. 差异双向识别：远端有本地无 (外部委托 / 外部持仓)、本地有远端无 (查无此单，只升级核对，不释放预占、不重发)。
4. READY: 只有查询完整、无阻断差异、无待关联成交与外部委托时才切换；READY 不解除既有熔断状态。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from qh_trader.core.constants import EventKind, PositionSide, SendState
from qh_trader.core.event import CanonicalEvent
from qh_trader.core.objects import (
    AccountFunds,
    ControlRecord,
    InstrumentId,
    OrderUpdate,
    Position,
    QueryResult,
    Trade,
)
from qh_trader.core.ports import JournalPort
from qh_trader.domain.orders import OrderManager
from qh_trader.domain.positions import PositionManager


class DiffSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    BLOCKING = "BLOCKING"


class RecoveryPhase(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    RECOVERING = "RECOVERING"
    RECONCILING = "RECONCILING"
    READY = "READY"


class RecoveryStateError(RuntimeError):
    """恢复协议状态不允许当前操作。"""


@dataclass
class ReconciliationDiff:
    """对账差异项."""

    category: str  # "order", "trade", "position", "funds", "query"
    identifier: str
    severity: DiffSeverity
    message: str
    local_value: Any = None
    remote_value: Any = None
    resolved: bool = False
    resolution: str | None = None


@dataclass(frozen=True, slots=True)
class QueryWatermark:
    """查询范围、交易日、完成标志与合并水位 (FR-ORD-07)."""

    kind: str
    batch_id: str
    trading_day: date
    complete: bool
    error_code: int | None
    record_count: int
    merged_at: datetime


@dataclass
class RecoveryReport:
    phase: RecoveryPhase
    diffs: list[ReconciliationDiff] = field(default_factory=list)
    watermarks: dict[str, QueryWatermark] = field(default_factory=dict)
    replayed_events: int = 0
    unknown_orders: list[str] = field(default_factory=list)
    alarms: list[str] = field(default_factory=list)

    @property
    def blocking(self) -> list[ReconciliationDiff]:
        return [d for d in self.diffs if d.severity == DiffSeverity.BLOCKING and not d.resolved]


class RecoveryCoordinator:
    """恢复对账与协调器."""

    REQUIRED_QUERIES = ("orders", "trades", "positions")

    def __init__(
        self,
        order_manager: OrderManager,
        position_manager: PositionManager | None = None,
        journal: JournalPort | None = None,
        trade_sink: Callable[[Trade, str | None], None] | None = None,
        funds_tolerance: Decimal = Decimal("0"),
    ) -> None:
        self.order_manager = order_manager
        self.position_manager = position_manager
        self.journal = journal
        # 新成交去重后交给账本入账的回调 (trade, client_order_id)
        self.trade_sink = trade_sink
        self.funds_tolerance = funds_tolerance
        self.phase: RecoveryPhase = RecoveryPhase.DISCONNECTED
        self.report = RecoveryReport(phase=self.phase)
        self.control_record: ControlRecord | None = None
        self.replay_cursor: int = 0
        self.expected_trading_day: date | None = None
        self.deadline: datetime | None = None
        self.disconnect_count: int = 0

    # ------------------------------------------------------------------ 阶段
    def _set_phase(self, phase: RecoveryPhase) -> None:
        self.phase = phase
        self.report.phase = phase

    def on_disconnected(self, reason: str = "") -> None:
        self.disconnect_count += 1
        if self.disconnect_count > 1:
            self.report.alarms.append(f"repeated disconnect #{self.disconnect_count}: {reason}")
        self._set_phase(RecoveryPhase.DISCONNECTED)

    def start_recovery(
        self, expected_trading_day: date | None = None, deadline: datetime | None = None
    ) -> RecoveryReport:
        """DISCONNECTED -> RECOVERING: 恢复控制记录与快照，标记未知发送."""
        if self.phase not in {RecoveryPhase.DISCONNECTED, RecoveryPhase.RECOVERING}:
            raise RecoveryStateError(f"cannot start recovery from {self.phase}")
        self.report = RecoveryReport(phase=RecoveryPhase.RECOVERING)
        self._set_phase(RecoveryPhase.RECOVERING)
        self.expected_trading_day = expected_trading_day
        self.deadline = deadline

        if self.journal is not None:
            self.control_record = self.journal.load_control_record()
            checkpoint = self.journal.load_checkpoint()
            self.replay_cursor = checkpoint.cursor

        for order in self.order_manager.orders():
            if order.is_active and order.send_state != SendState.CONFIRMED_REMOTE:
                if order.send_state == SendState.NOT_SENT and order.identity is None and order.send_attempts:
                    continue  # 明确未发送的本地失败不属于未知
                order.send_state = SendState.SENT_UNKNOWN
                order.flag_reconciliation("remote result unknown at recovery start")
                self.report.unknown_orders.append(order.client_order_id)
        return self.report

    def replay_events(self, events: Iterable[CanonicalEvent] | None = None) -> int:
        """重放快照之后的规范事件；去重保证游标重放与查询合并不重复记账 (A04)."""
        if self.phase != RecoveryPhase.RECOVERING:
            raise RecoveryStateError(f"replay only allowed in RECOVERING, not {self.phase}")
        if events is None:
            if self.journal is None:
                return 0
            events = self.journal.replay_from(self.replay_cursor)
        count = 0
        for event in events:
            if event.kind == EventKind.ORDER_REPORT:
                self.order_manager.process_order_update(event.payload)
            elif event.kind == EventKind.TRADE_REPORT:
                self._ingest_trade(event.payload)
            else:
                continue
            count += 1
        self.report.replayed_events += count
        return count

    def begin_reconciliation(self) -> None:
        if self.phase != RecoveryPhase.RECOVERING:
            raise RecoveryStateError(f"cannot reconcile from {self.phase}")
        self._set_phase(RecoveryPhase.RECONCILING)

    # ------------------------------------------------------------------ 合并
    def _ingest_trade(self, trade: Trade) -> None:
        order, is_new = self.order_manager.process_trade(trade)
        if not is_new:
            return
        if order is None:
            self._add_diff(
                "trade",
                trade.trade_id,
                DiffSeverity.BLOCKING,
                "real trade cannot be uniquely attributed; kept as unlinked placeholder",
                remote_value=trade.quantity,
            )
            return
        if self.trade_sink is not None:
            self.trade_sink(trade, order.client_order_id)

    def _add_diff(
        self,
        category: str,
        identifier: str,
        severity: DiffSeverity,
        message: str,
        local_value: Any = None,
        remote_value: Any = None,
    ) -> ReconciliationDiff:
        diff = ReconciliationDiff(category, identifier, severity, message, local_value, remote_value)
        self.report.diffs.append(diff)
        return diff

    def _record_watermark(self, kind: str, result: QueryResult) -> bool:
        """保存查询水位；返回该批次是否可作为完整快照使用."""
        wm = QueryWatermark(
            kind=kind,
            batch_id=result.batch.batch_id,
            trading_day=result.batch.trading_day,
            complete=result.complete,
            error_code=result.error_code,
            record_count=len(result.records),
            merged_at=result.available_at,
        )
        self.report.watermarks[kind] = wm
        usable = True
        if not result.complete or result.error_code is not None:
            self._add_diff(
                "query",
                f"{kind}:{result.batch.batch_id}",
                DiffSeverity.BLOCKING,
                f"{kind} query incomplete (complete={result.complete}, error_code={result.error_code}); "
                "not a full snapshot",
            )
            usable = False
        if self.expected_trading_day is not None and result.batch.trading_day != self.expected_trading_day:
            self._add_diff(
                "query",
                f"{kind}:{result.batch.batch_id}",
                DiffSeverity.BLOCKING,
                "query trading day differs from expected trading day; mixed-day snapshot cannot be published",
                local_value=self.expected_trading_day,
                remote_value=result.batch.trading_day,
            )
            usable = False
        return usable

    def _require_reconciling(self) -> None:
        if self.phase != RecoveryPhase.RECONCILING:
            raise RecoveryStateError(f"query merge only allowed in RECONCILING, not {self.phase}")

    def merge_order_query(self, result: QueryResult[OrderUpdate]) -> list[ReconciliationDiff]:
        """合并远端订单查询 (A04): 远端未知 -> 外部委托; 本地活动单远端缺失 -> 只升级核对."""
        self._require_reconciling()
        before = len(self.report.diffs)
        usable = self._record_watermark("orders", result)

        seen_local: set[str] = set()
        for remote in result.records:
            order = self.order_manager.process_order_update(remote)
            if order is None:
                ident = remote.identity
                self._add_diff(
                    "order",
                    f"{ident.exchange}.{ident.exchange_order_id or ident.order_ref}",
                    DiffSeverity.BLOCKING,
                    "external order detected from broker query; not recognized by local system",
                    remote_value=remote.status,
                )
            else:
                seen_local.add(order.client_order_id)

        if usable:
            for order in self.order_manager.orders():
                if (
                    order.is_active
                    and order.client_order_id not in seen_local
                    and order.send_state != SendState.NOT_SENT
                ):
                    # 查无此单：不能自动释放预占或重发 (FR-ORD-07)
                    order.send_state = SendState.SENT_UNKNOWN
                    order.flag_reconciliation("active order missing from complete broker order query")
                    self._add_diff(
                        "order",
                        order.client_order_id,
                        DiffSeverity.BLOCKING,
                        "local active order missing from remote query; reservation kept, manual confirmation required",
                        local_value=order.status,
                    )
        return self.report.diffs[before:]

    def merge_trade_query(self, result: QueryResult[Trade]) -> list[ReconciliationDiff]:
        """合并远端成交查询：已知成交去重跳过，新成交经归属后入账."""
        self._require_reconciling()
        before = len(self.report.diffs)
        self._record_watermark("trades", result)
        for trade in result.records:
            self._ingest_trade(trade)
        return self.report.diffs[before:]

    def reconcile_positions(
        self,
        remote: QueryResult[Position] | Iterable[Position],
        local_positions: dict[tuple[InstrumentId, PositionSide], Any] | None = None,
    ) -> list[ReconciliationDiff]:
        """双向比对本地与远端持仓 (A04)."""
        before = len(self.report.diffs)
        if isinstance(remote, QueryResult):
            self._record_watermark("positions", remote)
            remote_records = remote.records
        else:
            remote_records = tuple(remote)

        if local_positions is None:
            if self.position_manager is None:
                raise ValueError("reconcile_positions needs local positions or a position manager")
            local_positions = {(p.instrument, p.side): p for p in self.position_manager.all_positions()}

        remote_map = {(p.instrument, p.side): p for p in remote_records}
        keys = set(local_positions) | set(remote_map)
        for inst, side in sorted(keys, key=lambda k: (str(k[0]), str(k[1]))):
            local = local_positions.get((inst, side))
            rem = remote_map.get((inst, side))
            l_yd = local.pos_yd if local else 0
            l_td = local.pos_td if local else 0
            r_yd = rem.pos_yd if rem else 0
            r_td = rem.pos_td if rem else 0
            ident = f"{inst}.{side}"
            if local is None and (r_yd or r_td):
                self._add_diff(
                    "position",
                    ident,
                    DiffSeverity.BLOCKING,
                    "remote position unknown to local system",
                    local_value=0,
                    remote_value=r_yd + r_td,
                )
                continue
            if rem is None and (l_yd or l_td):
                self._add_diff(
                    "position",
                    ident,
                    DiffSeverity.BLOCKING,
                    "local has position but remote reports none",
                    local_value=l_yd + l_td,
                    remote_value=0,
                )
                continue
            if l_td != r_td:
                self._add_diff(
                    "position",
                    f"{ident}.pos_td",
                    DiffSeverity.BLOCKING,
                    "today position mismatch",
                    local_value=l_td,
                    remote_value=r_td,
                )
            if l_yd != r_yd:
                self._add_diff(
                    "position",
                    f"{ident}.pos_yd",
                    DiffSeverity.BLOCKING,
                    "yesterday position mismatch",
                    local_value=l_yd,
                    remote_value=r_yd,
                )
            if local is not None and rem is not None:
                if (local.frozen_yd, local.frozen_td) != (rem.frozen_yd, rem.frozen_td):
                    self._add_diff(
                        "position",
                        f"{ident}.frozen",
                        DiffSeverity.WARNING,
                        "frozen quantity mismatch",
                        local_value=(local.frozen_yd, local.frozen_td),
                        remote_value=(rem.frozen_yd, rem.frozen_td),
                    )
                if local.hedge_flag != rem.hedge_flag:
                    self._add_diff(
                        "position",
                        f"{ident}.hedge_flag",
                        DiffSeverity.BLOCKING,
                        "hedge flag mismatch",
                        local_value=local.hedge_flag,
                        remote_value=rem.hedge_flag,
                    )
        return self.report.diffs[before:]

    def reconcile_funds(self, remote: QueryResult[AccountFunds], local_balance: Decimal) -> list[ReconciliationDiff]:
        before = len(self.report.diffs)
        self._record_watermark("funds", remote)
        for funds in remote.records:
            if funds.balance is None:
                self._add_diff("funds", "balance", DiffSeverity.WARNING, "remote balance unavailable")
                continue
            if abs(funds.balance - local_balance) > self.funds_tolerance:
                self._add_diff(
                    "funds",
                    "balance",
                    DiffSeverity.BLOCKING,
                    "balance mismatch beyond tolerance",
                    local_value=local_balance,
                    remote_value=funds.balance,
                )
        return self.report.diffs[before:]

    # ------------------------------------------------------------------ 处置
    def resolve_diff(self, diff: ReconciliationDiff, resolution: str) -> None:
        """人工或自动处置一条差异，留痕."""
        diff.resolved = True
        diff.resolution = resolution

    def check_timeout(self, now: datetime) -> bool:
        if self.deadline is not None and now > self.deadline and self.phase != RecoveryPhase.READY:
            self.report.alarms.append(f"recovery deadline {self.deadline.isoformat()} exceeded at {now.isoformat()}")
            return True
        return False

    def can_enter_ready(self, diffs: list[ReconciliationDiff] | None = None) -> bool:
        """全部一致性检查通过才允许 READY (A04)."""
        if diffs is not None:
            return not any(d.severity == DiffSeverity.BLOCKING and not d.resolved for d in diffs)
        if self.phase != RecoveryPhase.RECONCILING:
            return False
        for kind in self.REQUIRED_QUERIES:
            wm = self.report.watermarks.get(kind)
            if wm is None or not wm.complete or wm.error_code is not None:
                return False
        days = {wm.trading_day for wm in self.report.watermarks.values()}
        if len(days) > 1:
            return False
        if self.report.blocking:
            return False
        if self.order_manager.pending_unlinked_trades() or self.order_manager.external_orders:
            return False
        return True

    def try_enter_ready(self) -> bool:
        """READY 只切换恢复阶段，不解除既有熔断或暂停状态."""
        if not self.can_enter_ready():
            return False
        self._set_phase(RecoveryPhase.READY)
        return True
