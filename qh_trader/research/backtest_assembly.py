"""[Research 层] 回测装配：把目录、日历、数据快照、网关与引擎按声明的假设组装并生成运行清单 (S3-05/06/09, FR-VAL-07).

脚本只负责参数解析与文件写出；装配逻辑在此以便回测、回放、敏感性矩阵与测试复用同一条链路。
(research 层不受引擎层依赖限制，可同时装配 data / gateway / infrastructure / strategy 适配器。)
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from qh_trader.analysis.performance import PerformanceMetrics, calculate_performance
from qh_trader.core.constants import (
    AuctionFillPolicy,
    ExecutionPolicy,
    IntrabarTouchRule,
    LimitLiquidityScenario,
    MissedExecutionPolicy,
    MissingRuleError,
)
from qh_trader.core.objects import Bar, InstrumentId
from qh_trader.data.calendar import TradingCalendar
from qh_trader.data.contracts import ContractResolver
from qh_trader.data.session_gate import CalendarSessionGate
from qh_trader.data.storage import ParquetDataStorage, normalize_interval
from qh_trader.engine.backtest_engine import BacktestEngine, BacktestResult
from qh_trader.engine.base_engine import SIMULATED_EVENT_PRIORITIES, InstrumentEconomics
from qh_trader.gateway.simulated_gateway import SimulatedGateway
from qh_trader.infrastructure.memory_journal import MemoryJournal
from qh_trader.strategy.examples.trend_following import DualMovingAverageStrategy

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class BacktestSpec:
    """一次实验的全部声明；序列化后即运行清单的输入部分."""

    symbol: str = "SHFE.rb2410"
    interval: str = "1d"
    storage_dir: str = "data_storage"
    catalog_path: str = "config/contract_catalog_2024v1.json"
    calendar_path: str | None = "config/calendar_2024v1.json"
    snapshot_id: str | None = None
    account_id: str = "backtest-account"
    initial_capital: Decimal = Decimal("1000000")
    commission_per_lot: Decimal = Decimal("5.0")
    margin_ratio: Decimal = Decimal("0.10")
    slippage_ticks: int = 0
    participation_rate: Decimal = Decimal("1.0")
    limit_liquidity_scenario: LimitLiquidityScenario = LimitLiquidityScenario.DIRECTION_CONSERVATIVE
    intrabar_touch_rule: IntrabarTouchRule = IntrabarTouchRule.TOUCH
    auction_fill_policy: AuctionFillPolicy = AuctionFillPolicy.ASSUME_PARTICIPATION
    order_delay_ms: int = 0
    cancel_delay_ms: int = 0
    execution_policy: ExecutionPolicy = ExecutionPolicy.NEXT_BAR_OPEN
    missed_execution: MissedExecutionPolicy = MissedExecutionPolicy.DEFER
    use_official_settlement: bool = True
    strategy_name: str = "dual_moving_average"
    fast_window: int = 5
    slow_window: int = 20
    order_size: int = 1
    random_seed: int = 0
    sample_split_ratio: float | None = None
    annual_trading_days: int = 242
    risk_free_rate: Decimal = Decimal("0.02")

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, value in self.__dict__.items():
            if isinstance(value, Decimal):
                out[name] = str(value)
            elif hasattr(value, "value"):
                out[name] = value.value
            else:
                out[name] = value
        return out


@dataclass(frozen=True)
class AssembledBacktest:
    spec: BacktestSpec
    instrument: InstrumentId
    bars: tuple[Bar, ...]
    settlement_prices: Mapping[tuple[InstrumentId, date], Decimal]
    engine: BacktestEngine
    gateway: SimulatedGateway
    journal: MemoryJournal
    catalog_version: str
    calendar_version: str | None
    snapshot: Mapping[str, Any]
    price_limits: Mapping[tuple[InstrumentId, date], tuple[Decimal, Decimal]] | None = None


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_state(root: Path = ROOT) -> dict[str, Any]:
    def run(*args: str) -> str | None:
        try:
            raw = subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL)
            return raw.decode("utf-8", errors="replace").strip()
        except (OSError, subprocess.CalledProcessError):
            return None

    commit = run("rev-parse", "HEAD")
    status = run("status", "--porcelain")
    return {"commit": commit, "dirty": bool(status) if status is not None else None}


def assemble(spec: BacktestSpec, *, root: Path = ROOT, bars: Sequence[Bar] | None = None) -> AssembledBacktest:
    catalog_path = root / spec.catalog_path
    if not catalog_path.is_file():
        raise MissingRuleError(f"contract catalog not found at: {catalog_path}")
    catalog = ContractResolver.from_file(catalog_path)
    instrument, _, _ = catalog.resolve(spec.symbol)
    contract = catalog.get_spec(spec.symbol)

    storage = ParquetDataStorage(root / spec.storage_dir)
    snapshot = storage.capture_snapshot(spec.snapshot_id)
    interval = normalize_interval(spec.interval)
    if bars is None:
        bars = storage.read_bars(instrument, interval, snapshot=snapshot)
    bars = tuple(sorted(bars, key=lambda b: (b.open_time, b.bar_end)))
    if not bars:
        raise ValueError(f"no bars found for {spec.symbol} at interval {spec.interval}")

    settlement_prices: dict[tuple[InstrumentId, date], Decimal] = {}
    if spec.use_official_settlement:
        for record in storage.read_settlements(instrument, snapshot=snapshot):
            if record.is_final:
                settlement_prices[(instrument, record.meta.trading_day)] = record.settlement_price

    calendar_version: str | None = None
    session_gate = None
    trading_days: tuple[date, ...] | None = None
    if spec.calendar_path:
        calendar = TradingCalendar.from_file(root / spec.calendar_path)
        calendar_version = calendar.version
        session_gate = CalendarSessionGate(calendar)
        trading_days = tuple(sorted(calendar.trading_days))

    gateway = SimulatedGateway(
        account_id=spec.account_id,
        trading_day=bars[0].meta.trading_day,
        slippage_ticks=spec.slippage_ticks,
        price_tick=contract.price_tick,
        participation_rate=spec.participation_rate,
        limit_liquidity_scenario=spec.limit_liquidity_scenario,
        intrabar_touch_rule=spec.intrabar_touch_rule,
        auction_fill_policy=spec.auction_fill_policy,
        order_delay=timedelta(milliseconds=spec.order_delay_ms),
        cancel_delay=timedelta(milliseconds=spec.cancel_delay_ms),
    )
    journal = MemoryJournal(spec.account_id)
    engine = BacktestEngine(
        account_id=spec.account_id,
        gateway=gateway,
        start_time=bars[0].bar_start,
        initial_capital=spec.initial_capital,
        journal=journal,
        session_gate=session_gate,
        execution_policy=spec.execution_policy,
        missed_execution=spec.missed_execution,
        trading_days=trading_days,
    )
    engine.register_instrument(
        instrument,
        InstrumentEconomics(
            multiplier=contract.multiplier,
            price_tick=contract.price_tick,
            commission_per_lot=spec.commission_per_lot,
            margin_ratio=spec.margin_ratio,
            source=f"catalog:{catalog.catalog_version}; commission/margin: research assumption in spec",
        ),
    )
    if spec.strategy_name != "dual_moving_average":
        raise MissingRuleError(f"unknown strategy: {spec.strategy_name}")
    engine.add_strategy(
        DualMovingAverageStrategy(
            strategy_id=f"dma-{instrument.symbol}",
            context=engine,
            instrument=instrument,
            fast_window=spec.fast_window,
            slow_window=spec.slow_window,
            order_size=spec.order_size,
        )
    )
    return AssembledBacktest(
        spec=spec,
        instrument=instrument,
        bars=bars,
        settlement_prices=settlement_prices,
        engine=engine,
        gateway=gateway,
        journal=journal,
        catalog_version=str(catalog.catalog_version),
        calendar_version=calendar_version,
        snapshot=snapshot.as_dict(),
    )


def run_assembled(assembled: AssembledBacktest, *, on_bar_processed: Any = None) -> BacktestResult:
    return assembled.engine.run(
        assembled.bars,
        settlement_prices=assembled.settlement_prices or None,
        price_limits=assembled.price_limits,
        on_bar_processed=on_bar_processed,
    )


def build_manifest(
    assembled: AssembledBacktest,
    result: BacktestResult,
    metrics: PerformanceMetrics,
    *,
    root: Path = ROOT,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """完整 run_manifest (FR-VAL-07)：数据、代码、依赖、策略、账户规则、执行策略、撮合假设、事件优先级、输出哈希."""
    spec = assembled.spec
    inputs = {
        "spec": spec.as_dict(),
        "data": {
            "storage_dir": spec.storage_dir,
            "dataset_snapshot": assembled.snapshot,
            "bar_count": len(assembled.bars),
            "start_time": assembled.bars[0].bar_start.isoformat(),
            "end_time": assembled.bars[-1].bar_end.isoformat(),
            "source_versions": sorted({b.meta.source_version for b in assembled.bars}),
            "settlement_source": result.settlement_source,
            "settlement_days": len(assembled.settlement_prices),
        },
        "rules": {
            "contract_catalog": {
                "path": spec.catalog_path,
                "version": assembled.catalog_version,
                "sha256": file_sha256(root / spec.catalog_path),
            },
            "calendar": (
                {
                    "path": spec.calendar_path,
                    "version": assembled.calendar_version,
                    "sha256": file_sha256(root / spec.calendar_path),
                }
                if spec.calendar_path
                else None
            ),
            "instrument_economics": {
                k: {
                    "multiplier": str(v.multiplier),
                    "price_tick": str(v.price_tick),
                    "commission_per_lot": str(v.commission_per_lot),
                    "margin_ratio": str(v.margin_ratio),
                    "source": v.source,
                }
                for k, v in assembled.engine.economics_table().items()
            },
            "close_capability_table": assembled.engine.smart_router.capabilities.version,
            "funds_policy": assembled.engine.ledger.funds_policy.version,
        },
        "execution": {
            "execution_policy": spec.execution_policy.value,
            "missed_execution": spec.missed_execution.value,
            "matching_assumptions": asdict(assembled.gateway.assumptions()),
            "event_priorities": {k.value: v for k, v in SIMULATED_EVENT_PRIORITIES.items()},
            "random_seed": spec.random_seed,
            "signal_resolution": spec.interval,
            "execution_resolution": spec.interval,
        },
        "sample_split": {"ratio": spec.sample_split_ratio} if spec.sample_split_ratio else None,
        "code": git_state(root),
        "environment": {
            "python_version": sys.version.split()[0],
            "platform": platform.platform(),
            "lockfile_sha256": file_sha256(root / "uv.lock") if (root / "uv.lock").is_file() else None,
        },
    }
    input_digest = hashlib.sha256(
        json.dumps(inputs, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()
    manifest = {
        "manifest_version": "2.0",
        "run_id": input_digest[:16],
        "input_digest": input_digest,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "account_id": result.account_id,
        "instrument": str(assembled.instrument),
        "inputs": inputs,
        "outputs": {
            "canonical_hashes": result.canonical_hashes(),
            "journal_head_seq": assembled.journal.head_seq,
            "summary": {
                "initial_capital": str(result.initial_capital),
                "final_equity": str(result.final_equity),
                "total_pnl": str(result.total_pnl),
                "total_commission": str(result.total_commission),
                "total_trades": result.total_trades,
                "closed_trades": metrics.closed_trades,
                "rejected_intents": len(result.rejected_intents),
                "missed_executions": len(result.missed_executions),
                "unfilled_orders": len(result.unfilled_orders),
                "total_return_pct": str(metrics.total_return * 100),
                "annualized_return_pct": str(metrics.annualized_return * 100),
                "annualized_volatility_pct": str(metrics.annualized_volatility * 100),
                "sharpe_ratio": str(metrics.sharpe_ratio),
                "max_drawdown_pct": str(metrics.max_drawdown_percent * 100),
                "calmar_ratio": str(metrics.calmar_ratio),
                "win_rate_pct": str(metrics.win_rate * 100),
                "profit_loss_ratio": str(metrics.profit_loss_ratio),
                "average_holding_days": str(metrics.average_holding_days),
                "turnover_ratio": str(metrics.turnover_ratio),
                "peak_margin_used": str(metrics.peak_margin_used),
                "commission_ratio_pct": str(metrics.commission_ratio * 100),
                "trading_days": metrics.trading_days,
                "sample_start": str(metrics.sample_start),
                "sample_end": str(metrics.sample_end),
                "monthly_returns": {k: str(v) for k, v in metrics.monthly_returns.items()},
            },
            "metric_conventions": {
                "return_frequency": metrics.return_frequency,
                "annual_trading_days": metrics.annual_trading_days,
                "risk_free_rate": str(metrics.risk_free_rate),
                "external_cash_flow": str(metrics.external_cash_flow),
                "equity_includes_open_positions": True,
                "pairing_rule": "ledger closed-trade records (FIFO by close offset bucket), net of actual commission",
            },
            "rejected_intents": [
                {
                    "client_order_id": r.client_order_id,
                    "strategy_id": r.strategy_id,
                    "at": r.at.isoformat(),
                    "stage": r.stage,
                    "reason": r.reason,
                }
                for r in result.rejected_intents
            ],
        },
    }
    if extra:
        manifest["extra"] = dict(extra)
    return manifest


def run_backtest(
    spec: BacktestSpec, *, root: Path = ROOT
) -> tuple[BacktestResult, PerformanceMetrics, dict[str, Any], AssembledBacktest]:
    assembled = assemble(spec, root=root)
    result = run_assembled(assembled)
    metrics = calculate_performance(result, annual_trading_days=spec.annual_trading_days, rf_rate=spec.risk_free_rate)
    manifest = build_manifest(assembled, result, metrics, root=root)
    return result, metrics, manifest, assembled


def write_run_artifacts(out_root: Path, manifest: dict[str, Any], result: BacktestResult, summary_text: str) -> Path:
    """按 run_id 写入独立目录，不覆盖既有实验；同 run_id 重跑写 rerun-N 子目录并比对哈希."""
    from qh_trader.analysis.visualizer import equity_curve_csv, equity_curve_svg

    run_dir = out_root / manifest["run_id"]
    if run_dir.exists():
        previous = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
        same = previous["outputs"]["canonical_hashes"] == manifest["outputs"]["canonical_hashes"]
        manifest["rerun_of"] = {"run_dir": str(run_dir), "canonical_hashes_match": same}
        n = 1
        while (run_dir / f"rerun-{n}").exists():
            n += 1
        run_dir = run_dir / f"rerun-{n}"
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    (run_dir / "equity_curve.csv").write_text(equity_curve_csv(result.equity_snapshots), encoding="utf-8")
    (run_dir / "equity_curve.svg").write_text(equity_curve_svg(result.equity_snapshots), encoding="utf-8")
    (run_dir / "summary.txt").write_text(summary_text, encoding="utf-8")
    trades = [
        {
            "trade_id": t.trade_id,
            "trading_day": t.trading_day.isoformat(),
            "event_time": t.event_time.isoformat(),
            "side": t.side.value,
            "offset": t.offset.value,
            "quantity": t.quantity,
            "price": str(t.price),
            "client_order_id": t.order_identity.client_order_id if t.order_identity else None,
        }
        for t in result.trades
    ]
    (run_dir / "trades.json").write_text(json.dumps(trades, indent=2, ensure_ascii=False), encoding="utf-8")
    return run_dir
