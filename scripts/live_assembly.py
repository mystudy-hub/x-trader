"""[入口装配] 执行服务的本地装配：Journal / 命令表 / 账户模型 / 网关 / 查询 / 恢复 (S5-04, S5-05 前置).

装配只在入口层完成 (04 ADR-01：领域与引擎只接收注入端口)。纸面模式用模拟网关加回报投影查询；
实盘模式需要 S5-01 的 CTP 网关与查询适配器，未交付前明确拒绝启动，不用模拟件冒充。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from qh_trader.core.constants import EventKind, Exchange, PositionSide
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
from qh_trader.gateway.epoch_fence import EpochFencedGateway
from qh_trader.gateway.paper_query import PaperQueryAdapter
from qh_trader.gateway.simulated_gateway import SimulatedGateway
from qh_trader.infrastructure.command_queue import SQLiteCommandClient, SQLiteExecutionStore
from qh_trader.infrastructure.journal import SQLiteJournal
from qh_trader.monitor.heartbeat import HeartbeatFile

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

    def __post_init__(self) -> None:
        if self.mode not in SUPPORTED_MODES:
            raise AssemblyError(f"unsupported execution mode {self.mode!r}; expected one of {SUPPORTED_MODES}")
        if not self.symbols:
            raise AssemblyError("at least one actual contract symbol is required")


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
    _last_beat_state: tuple[int | None, bool] | None = None
    _last_beat_at: float = 0.0

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
        processed = self.pump_gateway()
        processed += self.service.run_once(wait=True)
        processed += self.pump_gateway()
        self._beat()
        return processed

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
            "instruments": {str(instrument): eco.source for instrument, eco in self.economics.items()},
            "account_facts": self.model.fact_count,
            "assumptions": [
                "paper mode: simulated matching, query mirror of the same report stream; not broker evidence",
                "commission/margin from product_registry research assumptions pending rule verification (FR-RULE-05)",
            ],
        }


def assemble(spec: ExecutionSpec) -> AssembledExecution:
    if spec.mode == "live":
        raise AssemblyError("live mode requires the CTP gateway and query adapter (S5-01); not delivered yet")
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
