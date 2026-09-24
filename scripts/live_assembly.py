"""[入口装配] 执行服务装配：Journal / 命令表 / 账户模型 / 网关 / 查询 / 恢复 (S5-04, S5-05, S5-01).

装配只在入口层完成 (04 ADR-01：领域与引擎只接收注入端口)。纸面模式用模拟网关加回报投影查询；
实盘模式装配 S5-01 的 CTP 网关与查询适配器，并按 A23 顺序连接柜台：
连接（登录即持有会话）→ 隔离旧出口 → 提升代次 → 对账 → 放行。
柜台登记里未核验的能力由网关在发送前拒绝，装配不提供默认值。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from qh_trader.core.constants import EventKind, Exchange, MissingRuleError, PositionSide
from qh_trader.core.event import CanonicalEvent, JournalTransaction
from qh_trader.core.execution import CommandKind, ExecutionCommand, TakeoverRequest
from qh_trader.core.objects import ControlEpoch, ControlRecord, InstrumentId, QueryBatch
from qh_trader.core.ports import AccountQueryPort, ExecutionIsolationPort, ExecutionPort
from qh_trader.data.contracts import ContractResolver
from qh_trader.data.product_registry import get_product_spec, normalize_product
from qh_trader.data.statement import LocalDayFigures
from qh_trader.domain.ledger import AccountLedger, LedgerEntryKind
from qh_trader.domain.positions import PositionManager
from qh_trader.domain.recovery import RecoveryCoordinator
from qh_trader.engine.base_engine import InstrumentEconomics
from qh_trader.engine.execution_service import ExecutionService
from qh_trader.engine.live_account_model import FACTS_KEY, AccountOpening, LiveAccountModel
from qh_trader.gateway.ctp_gateway import (
    CtpOrderRefBook,
    CtpTraderGateway,
    restore_order_refs,
)
from qh_trader.gateway.ctp_query import CtpQueryAdapter
from qh_trader.gateway.epoch_fence import EpochFencedGateway
from qh_trader.gateway.feedback_normalizer import build_normalizer
from qh_trader.gateway.paper_query import PaperQueryAdapter
from qh_trader.gateway.simulated_gateway import SimulatedGateway
from qh_trader.infrastructure.command_queue import SQLiteCommandClient, SQLiteExecutionStore
from qh_trader.infrastructure.journal import SQLiteJournal
from qh_trader.monitor.heartbeat import HeartbeatFile, read_heartbeat
from scripts import ctp_setup

ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_MODES = ("paper", "live")
LOGGER = logging.getLogger(__name__)


class AssemblyError(RuntimeError):
    """装配输入缺失或不允许 (例如实盘网关未交付)."""


@dataclass(frozen=True, slots=True)
class ExecutionSpec:
    mode: str
    account_id: str
    journal_path: Path
    catalog_path: Path
    symbols: tuple[str, ...]
    initial_capital: Decimal
    trading_day: date
    controller_id: str
    heartbeat_path: Path
    poll_interval: float = 0.1
    heartbeat_interval: float = 1.0
    config_path: Path | None = None
    config_sha256: str | None = None
    broker_profile: str | None = None
    broker: Mapping[str, object] = field(default_factory=dict)
    ctp_flow_dir: str = "runs/live/ctp_flow"
    query_interval_ms: int = 1000

    def __post_init__(self) -> None:
        if self.mode not in SUPPORTED_MODES:
            raise AssemblyError(f"unsupported execution mode {self.mode!r}; expected one of {SUPPORTED_MODES}")
        if not self.symbols:
            raise AssemblyError("at least one actual contract symbol is required")
        object.__setattr__(self, "broker", MappingProxyType(dict(self.broker)))
        if self.query_interval_ms < 0:
            raise AssemblyError("query interval cannot be negative")


def load_settings(path: Path) -> Mapping[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, Mapping):
        raise AssemblyError("settings must be a YAML mapping")
    return data


def spec_from_settings(
    settings: Mapping[str, Any],
    *,
    config_path: Path | None,
    mode: str | None = None,
    symbols: Sequence[str] | None = None,
    catalog_path: str | None = None,
    controller_id: str = "execution-service",
    heartbeat_path: str | None = None,
    trading_day: date | None = None,
    poll_interval: float = 0.1,
) -> ExecutionSpec:
    system = settings.get("system", {})
    risk = settings.get("risk", {})
    storage = settings.get("storage", {})
    strategy = settings.get("strategy", {})
    chosen_mode = mode or str(system.get("mode", ""))
    account_id = str(risk.get("account_id", "")).strip()
    if not account_id:
        raise AssemblyError("risk.account_id is required")
    journal = storage.get("journal_db_path")
    if not journal:
        raise AssemblyError("storage.journal_db_path is required")
    chosen_symbols = tuple(symbols) if symbols else tuple(str(item) for item in strategy.get("symbols", ()))
    if trading_day is None:
        # 交易日按交易所日历归属 (夜盘属于下一交易日)，不能用 UTC 或本地自然日代替；日历接入前由操作员显式给出
        raise AssemblyError("an explicit expected trading day is required (--trading-day YYYY-MM-DD)")
    digest = None
    if config_path is not None and config_path.exists():
        digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
    broker = settings.get("broker") or {}
    if not isinstance(broker, Mapping):
        raise AssemblyError("broker section must be a mapping")
    profile_name = None if not broker.get("profile") else str(broker["profile"])
    if chosen_mode == "live" and not profile_name:
        raise AssemblyError("live mode requires broker.profile pointing at a registered counter profile")
    return ExecutionSpec(
        mode=chosen_mode,
        account_id=account_id,
        journal_path=ROOT / str(journal),
        catalog_path=ROOT
        / (catalog_path or str(settings.get("data", {}).get("catalog_path", "config/contract_catalog_s4_2024v1.json"))),
        symbols=chosen_symbols,
        initial_capital=Decimal(str(risk.get("initial_capital", "0"))),
        trading_day=trading_day,
        controller_id=controller_id,
        heartbeat_path=ROOT / (heartbeat_path or f"runs/live/heartbeat/{account_id}-execution.json"),
        poll_interval=poll_interval,
        config_path=config_path,
        config_sha256=digest,
        broker_profile=profile_name,
        broker={
            key: broker.get(key)
            for key in ("front_trade_uri", "front_market_uri", "broker_id", "investor_id", "user_id", "app_id")
        },
        ctp_flow_dir=str(broker.get("flow_dir") or f"runs/live/ctp_flow/{account_id}"),
        query_interval_ms=int(broker.get("query_interval_ms") or 1000),
    )


def economics_from_catalog(
    catalog_path: Path, symbols: Sequence[str], *, as_of: date | None = None
) -> dict[InstrumentId, InstrumentEconomics]:
    """乘数 / 最小变动来自版本化合约目录；手续费与保证金来自品种登记 (研究假设，写入来源字段)."""
    if not catalog_path.is_file():
        raise AssemblyError(f"contract catalog not found: {catalog_path}")
    catalog = ContractResolver.from_file(catalog_path)
    economics: dict[InstrumentId, InstrumentEconomics] = {}
    for raw in symbols:
        exchange = None
        code = raw
        if "." in raw:
            prefix, code = raw.split(".", 1)
            exchange = Exchange(prefix)
        instrument, _, _ = catalog.resolve(raw, as_of=as_of, exchange=exchange)
        contract = catalog.get_spec(raw, as_of=as_of, exchange=exchange)
        product = get_product_spec(normalize_product("".join(ch for ch in code if ch.isalpha())))
        economics[instrument] = InstrumentEconomics(
            multiplier=contract.multiplier,
            price_tick=contract.price_tick,
            commission_per_lot=product.commission_per_lot,
            margin_ratio=product.margin_ratio,
            source=f"catalog:{catalog.catalog_version}; commission/margin: product_registry:{product.source}",
        )
    return economics


class PaperIsolation(ExecutionIsolationPort):
    """纸面模式没有旧柜台连接可隔离：首次接管 (无控制记录) 直接成立；此后须操作员显式确认."""

    def __init__(self, *, operator_confirmed: bool) -> None:
        self.operator_confirmed = operator_confirmed
        self.calls: list[tuple[ControlRecord | None, ExecutionCommand]] = []

    def isolate(self, previous: ControlRecord | None, request: ExecutionCommand) -> bool:
        self.calls.append((previous, request))
        return previous is None or self.operator_confirmed


class CounterEventForwarder:
    """回调入队出口：执行服务的装配晚于网关，转发引用在服务创建后绑定 (ADR-X2).

    CTP 回调只在 ``connect()`` 之后才可能到达，而连接又发生在装配之后；缓冲只是防御：
    未绑定前到达的回调不得丢失，超出上限即明确失败并要求重新对账，不静默丢弃。
    """

    def __init__(self, *, capacity: int = 1024) -> None:
        self._target: ExecutionService | None = None
        self._buffer: list[CanonicalEvent] = []
        self._errors: list[tuple[str, str]] = []
        self.capacity = capacity
        self.forwarded = 0
        self.buffer_overflows = 0

    @property
    def bound(self) -> bool:
        return self._target is not None

    def bind(self, service: ExecutionService) -> None:
        self._target = service
        for event in self._buffer:
            service.enqueue(event)
            self.forwarded += 1
        self._buffer.clear()
        for source_id, error_type in self._errors:
            service.enqueue_callback_error(source_id, RuntimeError(error_type))
        self._errors.clear()

    def enqueue(self, event: CanonicalEvent) -> bool:
        if self._target is None:
            if len(self._buffer) >= self.capacity:
                self.buffer_overflows += 1
                raise RuntimeError("callback buffer overflowed before the execution service was bound")
            self._buffer.append(event)
            return True
        self.forwarded += 1
        return self._target.enqueue(event)

    def enqueue_callback_error(self, source_id: str, error: Exception) -> None:
        if self._target is None:
            self._errors.append((source_id, type(error).__name__))
            return
        self._target.enqueue_callback_error(source_id, error)


class CtpIsolation(ExecutionIsolationPort):
    """接管前旧出口隔离：本地可核验的部分 + 操作员确认 (A23, FR-RISK-08).

    柜台侧“重复登录是否强制旧会话下线”尚未核验 (GAP-S0-05)，因此**登录成功不作为隔离证据**：
    只有旧的执行实例心跳已经过期（或从未登记）时，才结合操作员确认判定隔离成立。
    心跳仍新鲜 → 拒绝提升代次；操作员未确认 → 同样拒绝。
    """

    def __init__(
        self,
        *,
        gateway: CtpTraderGateway,
        heartbeat_path: Path,
        operator_confirmed: bool,
        max_age_s: float = 30.0,
        wall_time: Callable[[], datetime] | None = None,
    ) -> None:
        self.gateway = gateway
        self.heartbeat_path = Path(heartbeat_path)
        self.operator_confirmed = operator_confirmed
        self.max_age_s = float(max_age_s)
        self._wall_time = wall_time or (lambda: datetime.now(timezone.utc))
        self.evidence: list[Mapping[str, object]] = []

    def isolate(self, previous: ControlRecord | None, request: ExecutionCommand) -> bool:
        recorded: dict[str, object] = {
            "previous_controller": None if previous is None else previous.epoch.controller_id,
            "requested_by": request.producer_id,
            "applied": False,
        }
        if self.gateway.trading_day is None:
            recorded["reason"] = "counter session is not established; isolation cannot be judged"
            self.evidence.append(recorded)
            return False
        heartbeat = read_heartbeat(self.heartbeat_path)
        if heartbeat is not None:
            age = (self._wall_time() - heartbeat.beat_wall).total_seconds()
            recorded["heartbeat_age_s"] = round(age, 3)
            recorded["heartbeat_instance"] = heartbeat.instance_id
            if age <= self.max_age_s:
                # 心跳新鲜说明本机还有实例在运行（可能是旧出口）；此时不提升代次，不猜它是谁
                recorded["reason"] = "a fresh execution heartbeat exists; the former connection is not isolated"
                self.evidence.append(recorded)
                return False
        recorded["heartbeat_missing"] = heartbeat is None
        if not self.operator_confirmed:
            recorded["reason"] = "operator confirmation is required: counter-side session takeover is unverified"
            self.evidence.append(recorded)
            return False
        recorded["applied"] = True
        status = self.gateway.status()
        recorded["counter_session"] = {"front_id": status.get("front_id"), "session_id": status.get("session_id")}
        self.evidence.append(recorded)
        return True


@dataclass
class AssembledExecution:
    spec: ExecutionSpec
    journal: SQLiteJournal
    store: SQLiteExecutionStore
    client: SQLiteCommandClient
    model: LiveAccountModel
    gateway: ExecutionPort
    query: AccountQueryPort
    recovery: RecoveryCoordinator
    service: ExecutionService
    heartbeat: HeartbeatFile
    economics: Mapping[InstrumentId, InstrumentEconomics]
    stack: ExitStack = field(default_factory=ExitStack)
    heartbeat_failures: int = 0
    counter_gateway: CtpTraderGateway | None = None
    forwarder: CounterEventForwarder | None = None
    profile_summary: Mapping[str, object] = field(default_factory=dict)
    session_report: Mapping[str, object] | None = None
    _last_beat_state: tuple[int | None, bool] | None = None
    _last_beat_at: float = 0.0
    _session_lost_logged: bool = False

    @property
    def live(self) -> bool:
        return self.counter_gateway is not None

    def isolation(self, *, operator_confirmed: bool) -> ExecutionIsolationPort:
        if self.counter_gateway is None:
            return PaperIsolation(operator_confirmed=operator_confirmed)
        return CtpIsolation(
            gateway=self.counter_gateway,
            heartbeat_path=self.spec.heartbeat_path,
            operator_confirmed=operator_confirmed,
        )

    def connect_counter(self) -> Mapping[str, object] | None:
        """连接柜台并校验交易日；纸面模式无操作 (S5-01 连接 + FR-CAL-03 交易日核验)."""
        gateway = self.counter_gateway
        if gateway is None:
            return None
        report = gateway.connect()
        self.session_report = report.as_mapping()
        if report.trading_day is not None and report.trading_day != self.spec.trading_day:
            raise AssemblyError(
                f"counter trading day {report.trading_day.isoformat()} differs from the expected "
                f"{self.spec.trading_day.isoformat()}; the operator must confirm the trading day first"
            )
        return self.session_report

    def close(self) -> None:
        self.stack.close()

    # ------------------------------------------------------------------ 控制权
    def request_control(self, reason: str) -> ExecutionCommand:
        """以本实例名义写入接管申请 (观测到的当前代次)；申请本身不授予交易权."""
        current = self.store.control()
        observed = ControlEpoch(self.spec.controller_id, 0) if current is None else current.epoch
        command = ExecutionCommand(
            command_id=f"takeover-{self.spec.controller_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}",
            account_id=self.spec.account_id,
            producer_id=self.spec.controller_id,
            control=observed,
            kind=CommandKind.TAKEOVER_REQUEST,
            submitted_at=datetime.now(timezone.utc),
            payload=TakeoverRequest(controller_id=self.spec.controller_id, reason=reason),
        )
        self.client.submit(command)
        return command

    def take_over(self, command_id: str, isolation: ExecutionIsolationPort) -> ControlEpoch:
        return self.service.take_over(command_id, isolation)

    # ------------------------------------------------------------------ 对账与放行
    def reconcile_and_enable(self, *, expected_trading_day: date | None = None) -> None:
        day = expected_trading_day or self.spec.trading_day
        batch = QueryBatch(
            f"startup-{datetime.now(timezone.utc).isoformat()}", self.spec.account_id, day, datetime.now(timezone.utc)
        )
        # 每次对账都在按当前已发布事实重建的副本上合并查询：装配时的副本早于首次发布，且会随交易过时
        replica = self.model.replica()
        self.recovery.order_manager = replica.orders
        self.recovery.position_manager = replica.positions
        self.recovery.start_recovery(expected_trading_day=day)
        self.recovery.begin_reconciliation()
        self.recovery.merge_order_query(self.query.query_orders(batch))
        self.recovery.merge_trade_query(self.query.query_trades(batch))
        self.recovery.reconcile_positions(self.query.query_positions(batch))
        self.recovery.reconcile_funds(self.query.query_account(batch), self.model.ledger.balance)
        # 柜台侧放行：只有会话与对账都成立才重新打开网关自己的发送门禁 (FR-REC-04)
        gateway = self.counter_gateway
        if gateway is not None and not gateway.mark_reconciled():
            raise AssemblyError("counter session is not established; the gateway keeps new risk closed")
        self.service.enable_after_reconciliation()

    # ------------------------------------------------------------------ 主循环
    def pump_gateway(self) -> int:
        """纸面模式：把模拟网关的出站回报送入执行服务并同步查询投影；真实网关在回调线程自行入队."""
        drain = getattr(self.gateway, "drain_events", None)
        if drain is None:
            return 0
        events = drain()
        if not events:
            return 0
        observe = getattr(self.query, "observe", None)
        if observe is not None:
            observe(events)
        for event in events:
            self.service.enqueue(event)
        return len(events)

    def step(self) -> int:
        self.maintain_counter_session()
        processed = self.pump_gateway()
        processed += self.service.run_once(wait=True)
        processed += self.pump_gateway()
        self._beat()
        return processed

    def maintain_counter_session(self) -> None:
        """断线 / 重连后重新登录；失去会话期间不得放行新风险 (FR-REC-04).

        重新登录成功也不自动放行：网关自己的发送门禁要由显式对账重新打开。
        """
        gateway = self.counter_gateway
        if gateway is None:
            return
        gateway.maintain()
        if gateway.ready_to_send or not self.service.ready:
            return
        if self._session_lost_logged:
            return
        self._session_lost_logged = True
        LOGGER.error(
            "CTP session is not dispatchable (fault=%s, needs_reconciliation=%s); "
            "closing the trading gate until the operator reconciles again",
            gateway.fault,
            gateway.needs_reconciliation,
        )
        self.recovery.on_disconnected("ctp session requires account queries before new risk")

    def _beat(self) -> None:
        """按间隔写心跳；就绪或代次变化立即写。心跳写失败只告警，不能中断交易主循环."""
        current = self.store.control()
        state = (None if current is None else current.epoch.epoch, self.service.ready)
        now = time.monotonic()
        if state == self._last_beat_state and now - self._last_beat_at < self.spec.heartbeat_interval:
            return
        try:
            self.heartbeat.beat(control_epoch=state[0], ready=state[1])
        except OSError as exc:
            self.heartbeat_failures += 1
            LOGGER.warning("heartbeat write failed (%d so far): %s", self.heartbeat_failures, exc)
            return
        self._last_beat_state = state
        self._last_beat_at = now

    def manifest(self) -> dict[str, Any]:
        from qh_trader.research.backtest_assembly import git_state

        current = self.store.control()
        return {
            "kind": "execution_service",
            "mode": self.spec.mode,
            "account_id": self.spec.account_id,
            "journal_path": str(self.spec.journal_path.relative_to(ROOT))
            if self.spec.journal_path.is_relative_to(ROOT)
            else str(self.spec.journal_path),
            "config": {
                "path": None if self.spec.config_path is None else str(self.spec.config_path),
                "sha256": self.spec.config_sha256,
            },
            "code": git_state(ROOT),
            "control": None
            if current is None
            else {"controller_id": current.epoch.controller_id, "epoch": current.epoch.epoch},
            "poll_interval": self.spec.poll_interval,
            "trade_batch_size": self.service.trade_batch_size,
            "gateway": type(getattr(self.gateway, "inner", self.gateway)).__name__,
            "query_source": type(self.query).__name__,
            "counter": None
            if self.counter_gateway is None
            else {
                "profile": dict(self.profile_summary),
                "status": dict(self.counter_gateway.status()),
                "session": self.session_report,
            },
            "instruments": {str(instrument): eco.source for instrument, eco in self.economics.items()},
            "account_facts": self.model.fact_count,
            "assumptions": [
                "paper mode: simulated matching, query mirror of the same report stream; not broker evidence",
                "commission/margin from product_registry research assumptions pending rule verification (FR-RULE-05)",
            ],
        }


def assemble(spec: ExecutionSpec) -> AssembledExecution:
    stack = ExitStack()
    try:
        journal = stack.enter_context(SQLiteJournal(spec.journal_path, account_id=spec.account_id))
        journal.migrate()
        store = stack.enter_context(SQLiteExecutionStore(journal))
        store.migrate()
        client = stack.enter_context(SQLiteCommandClient(spec.journal_path, account_id=spec.account_id))
        economics = economics_from_catalog(spec.catalog_path, spec.symbols, as_of=spec.trading_day)
        opening = AccountOpening(spec.initial_capital, spec.trading_day)
        model = LiveAccountModel(spec.account_id, opening, economics)
        ensure_opened(store, model)
        if spec.mode == "live":
            return _assemble_live(spec, stack, journal, store, client, economics, model)
        raw_gateway = SimulatedGateway(
            spec.account_id, spec.trading_day, price_tick=min(e.price_tick for e in economics.values())
        )
        gateway = EpochFencedGateway(raw_gateway, lambda: _current_epoch(store))
        query = PaperQueryAdapter(
            spec.account_id,
            trading_day=spec.trading_day,
            balance=lambda: model.ledger.balance if model.kernel_ready else None,
            now=lambda: datetime.now(timezone.utc),
        )
        # 纸面“柜台”视图来自回报流；重启时从 Journal 已持久化的回报重建，否则对账面对的是空柜台
        query.observe(journal.replay_from(0))
        query.trading_day = spec.trading_day
        # 对账前由 reconcile_and_enable 换成按已发布事实重建的副本
        placeholder = model.replica()
        recovery = RecoveryCoordinator(placeholder.orders, placeholder.positions)
        service = ExecutionService(
            store=store, model=model, gateway=gateway, recovery=recovery, poll_interval=spec.poll_interval
        )
        heartbeat = HeartbeatFile(spec.heartbeat_path, role="execution", instance_id=spec.controller_id)
    except Exception:
        stack.close()
        raise
    return AssembledExecution(
        spec=spec,
        journal=journal,
        store=store,
        client=client,
        model=model,
        gateway=gateway,
        query=query,
        recovery=recovery,
        service=service,
        heartbeat=heartbeat,
        economics=economics,
        stack=stack,
    )


def _account_facts(store: SQLiteExecutionStore) -> tuple[Mapping[str, Any], ...]:
    checkpoint = store.checkpoint()
    return tuple(checkpoint.state.get(FACTS_KEY, ()))  # type: ignore[arg-type]


def _assemble_live(
    spec: ExecutionSpec,
    stack: ExitStack,
    journal: SQLiteJournal,
    store: SQLiteExecutionStore,
    client: SQLiteCommandClient,
    economics: Mapping[InstrumentId, InstrumentEconomics],
    model: LiveAccountModel,
) -> AssembledExecution:
    """实盘装配：CTP 网关 + 查询适配器 + 事件转发；柜台连接由 :meth:`AssembledExecution.connect_counter` 建立.

    未核验能力（今昨仓映射、市价单）由网关在发送前拒绝；秘密只从环境变量读取。
    """
    try:
        profile = ctp_setup.load_broker_profile(spec.broker_profile)
        settings = ctp_setup.ctp_settings(
            profile,
            user_id=None if not spec.broker.get("user_id") else str(spec.broker["user_id"]),
            investor_id=None if not spec.broker.get("investor_id") else str(spec.broker["investor_id"]),
            front=None if not spec.broker.get("front_trade_uri") else str(spec.broker["front_trade_uri"]),
            flow_dir=spec.ctp_flow_dir,
            query_interval_ms=spec.query_interval_ms,
        )
        if spec.broker.get("broker_id") and str(spec.broker["broker_id"]) != settings.broker_id:
            raise AssemblyError(
                f"broker.broker_id {spec.broker['broker_id']!r} differs from the registered profile "
                f"{settings.broker_id!r}"
            )
        forwarder = CounterEventForwarder()
        ref_book = CtpOrderRefBook(restored=restore_order_refs(_account_facts(store), {}))
        normalizer = build_normalizer(spec.account_id, ref_book)
        price_ticks = {instrument: item.price_tick for instrument, item in economics.items()}

        def price_tick(instrument: InstrumentId) -> Decimal:
            tick = price_ticks.get(instrument)
            if tick is None:
                raise MissingRuleError(
                    f"no registered contract economics for {instrument}; price needs a verified tick"
                )
            return tick

        counter = CtpTraderGateway(
            settings=settings,
            account_id=spec.account_id,
            events=forwarder,
            normalizer=normalizer,
            price_tick=price_tick,
            capability_profile=ctp_setup.capability_profile(profile),
            capability_version="registered:" + str(spec.broker_profile),
            authority=lambda: _current_epoch(store),
            offset_mappings=ctp_setup.offset_mappings(profile),
            ref_book=ref_book,
        )
        query = CtpQueryAdapter(
            account_id=spec.account_id,
            channel=counter,
            normalizer=normalizer,
            investor_id=settings.investor_id,
            broker_id=settings.broker_id,
            trading_day=lambda: counter.trading_day,
            interval_ms=spec.query_interval_ms,
            timeout_s=settings.query_timeout_s,
            source_version="ctp:" + counter.binding.version,
        )
        counter.router.queries = query
        gateway = EpochFencedGateway(counter, lambda: _current_epoch(store))
        placeholder = model.replica()
        recovery = RecoveryCoordinator(placeholder.orders, placeholder.positions)
        service = ExecutionService(
            store=store, model=model, gateway=gateway, recovery=recovery, poll_interval=spec.poll_interval
        )
        forwarder.bind(service)
        heartbeat = HeartbeatFile(spec.heartbeat_path, role="execution", instance_id=spec.controller_id)
    except Exception:
        stack.close()
        raise
    return AssembledExecution(
        spec=spec,
        journal=journal,
        store=store,
        client=client,
        model=model,
        gateway=gateway,
        query=query,
        recovery=recovery,
        service=service,
        heartbeat=heartbeat,
        economics=economics,
        stack=stack,
        counter_gateway=counter,
        forwarder=forwarder,
        profile_summary=ctp_setup.profile_summary(profile),
    )


def _current_epoch(store: SQLiteExecutionStore) -> ControlEpoch | None:
    current = store.control()
    return None if current is None else current.epoch


def ensure_opened(store: SQLiteExecutionStore, model: LiveAccountModel) -> bool:
    """首次启动把账户开立事实写入 Journal (审计事件 + 状态)；已有事实时不改动. 返回是否写入."""
    checkpoint = store.checkpoint()
    if checkpoint.state.get(FACTS_KEY):
        return False
    now = datetime.now(timezone.utc)
    event = CanonicalEvent(
        event_id="account-opened:" + now.strftime("%Y%m%dT%H%M%S%fZ"),
        kind=EventKind.CONTROL,
        event_time=now,
        available_at=now,
        sequence=store.next_ingress_sequence(),
        source_id="live-assembly",
        payload={"action": "account_opened", "account_id": model.account_id},
    )
    store.commit(
        JournalTransaction(
            transaction_id="account-opened:" + now.strftime("%Y%m%dT%H%M%S%fZ"),
            events=(event,),
            cursor_before=checkpoint.cursor,
            cursor_after=checkpoint.cursor + 1,
            state_updates={FACTS_KEY: (model.opening_fact(),)},
        ),
        expected_control=None if checkpoint.control_record is None else checkpoint.control_record.epoch,
    )
    return True


def open_model_read_only(
    journal_path: Path, account_id: str, catalog_path: Path, symbols: Sequence[str] | None = None
) -> tuple[SQLiteJournal, LiveAccountModel]:
    """脚本侧只读重建账户模型 (结算单比对、状态查询)；不取执行锁、不写 Journal."""
    journal = SQLiteJournal(journal_path, account_id=account_id)
    checkpoint = journal.load_checkpoint()
    facts: tuple[Mapping[str, Any], ...] = tuple(checkpoint.state.get(FACTS_KEY, ()))  # type: ignore[arg-type]
    instruments: set[str] = set(symbols or ())
    for fact in facts:
        for key in ("intent", "trade", "update"):
            value = fact.get(key)
            instrument = getattr(value, "instrument", None)
            if instrument is not None:
                instruments.add(str(instrument))
        if fact.get("kind") == "settlement_price":
            instruments.add(str(fact["instrument"]))
    opening = AccountOpening(
        Decimal("0"), checkpoint.control_record.acquired_at.date() if checkpoint.control_record else date.today()
    )
    for fact in facts:
        if fact.get("kind") == "opened":
            opening = AccountOpening(fact["initial_capital"], fact["trading_day"])
    economics = economics_from_catalog(catalog_path, sorted(instruments)) if instruments else {}
    model = LiveAccountModel(account_id, opening, economics)
    model.publish(checkpoint)
    return journal, model


def write_manifest(out_dir: Path, manifest: Mapping[str, Any]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "run_manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def local_day_figures(
    ledger: AccountLedger,
    trading_day: date,
    *,
    positions: PositionManager | None = None,
    margin_used: Decimal | None = None,
) -> LocalDayFigures:
    """按账本条目汇总某交易日的口径 (FR-LED-03：盯市平仓 + 结算盈亏；逐笔盈亏不进余额).

    期末结存取"截至该交易日"的余额：当前余额减去之后交易日的全部条目，使结算单迟到时仍可比。
    持仓手数只在账本尚未越过该交易日时可比 (否则记为 None，由比对方跳过)。放在入口层：data 层的
    结算单模块只依赖 Core 类型，账本读取不进入适配器 (NFR-03)。
    """
    close_pnl = Decimal(0)
    mtm_pnl = Decimal(0)
    commission = Decimal(0)
    cash_flow = Decimal(0)
    later = Decimal(0)
    for entry in ledger.entries:
        if entry.trading_day > trading_day:
            later += entry.amount
            continue
        if entry.trading_day != trading_day:
            continue
        if entry.kind == LedgerEntryKind.MTM_CLOSE:
            close_pnl += entry.amount
        elif entry.kind in (LedgerEntryKind.SETTLEMENT, LedgerEntryKind.SETTLEMENT_CORRECTION):
            mtm_pnl += entry.amount
        elif entry.kind == LedgerEntryKind.COMMISSION:
            commission += -entry.amount
        elif entry.kind == LedgerEntryKind.CASH_TRANSFER:
            cash_flow += entry.amount
    manager = positions if positions is not None else ledger.position_manager
    current_day = ledger.current_trading_day
    held: Mapping[tuple[InstrumentId, PositionSide], int] | None
    if current_day is not None and current_day > trading_day and later != 0:
        held = None
    else:
        held = {
            (position.instrument, position.side): position.total_position
            for position in manager.all_positions()
            if position.total_position
        }
    return LocalDayFigures(
        balance_end=ledger.balance - later,
        close_pnl=close_pnl,
        mtm_pnl=mtm_pnl,
        commission=commission,
        margin=margin_used,
        positions=held,
        cash_flow=cash_flow,
    )
