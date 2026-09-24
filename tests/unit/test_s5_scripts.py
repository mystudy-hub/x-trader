"""S5-04 / S5-08 入口脚本：纸面装配、跨进程命令投递、重启接管、结算单比对与 REDUCE_ONLY 保护."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from qh_trader.core.constants import (
    Exchange,
    MissingRuleError,
    Offset,
    OrderStatus,
    OrderType,
    PositionSide,
    Side,
)
from qh_trader.core.execution import CommandKind, CommandStatus, ExecutionCommand, ExecutionNotReadyError
from qh_trader.core.objects import InstrumentId, OrderIntent, QueryBatch
from qh_trader.data.statement import statement_from_mapping
from qh_trader.domain.risk import RiskState
from qh_trader.infrastructure.command_queue import SQLiteCommandClient
from qh_trader.infrastructure.journal import SQLiteJournal
from qh_trader.monitor.heartbeat import read_heartbeat
from scripts import parse_statement, run_execution_service
from scripts.live_assembly import (
    AssemblyError,
    PaperIsolation,
    assemble,
    economics_from_catalog,
    open_model_read_only,
    spec_from_settings,
)

ROOT = Path(__file__).resolve().parents[2]
PROCESS_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ACCOUNT = "paper-script-account"
RB = InstrumentId(Exchange.SHFE, "rb2410")
DAY = date(2024, 9, 10)


def write_settings(tmp_path: Path, *, mode: str = "paper") -> Path:
    template = yaml.safe_load((ROOT / "config" / "settings.yaml.example").read_text(encoding="utf-8"))
    template["system"]["mode"] = mode
    template["risk"]["account_id"] = ACCOUNT
    template["risk"]["initial_capital"] = "100000.00"
    template["storage"]["journal_db_path"] = str(tmp_path / "trading.db")
    template["strategy"]["symbols"] = ["SHFE.rb2410"]
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(template, allow_unicode=True), encoding="utf-8")
    return path


def run_script(path: Path, *extra: str) -> int:
    return run_execution_service.main(
        [
            "--config",
            str(path),
            "--trading-day",
            DAY.isoformat(),
            "--out",
            str(path.parent / "out"),
            "--heartbeat",
            str(path.parent / "hb.json"),
            *extra,
        ]
    )


def submit_from_another_process(db: Path, identifier: str, epoch: int) -> subprocess.CompletedProcess:
    code = f"""
import sys
from datetime import datetime, timezone
from decimal import Decimal
from qh_trader.core.constants import Exchange, Offset, OrderType, Side
from qh_trader.core.execution import CommandKind, ExecutionCommand
from qh_trader.core.objects import ControlEpoch, InstrumentId, OrderIntent
from qh_trader.infrastructure.command_queue import SQLiteCommandClient
now = datetime.now(timezone.utc)
intent = OrderIntent(client_order_id={identifier!r}, account_id={ACCOUNT!r}, strategy_id="strategy-process",
    instrument=InstrumentId(Exchange.SHFE, "rb2410"), side=Side.BUY, offset=Offset.OPEN, quantity=1,
    order_type=OrderType.LIMIT, created_at=now, limit_price_ticks=3500)
with SQLiteCommandClient({str(db)!r}, account_id={ACCOUNT!r}) as client:
    queued = client.submit(ExecutionCommand(command_id={identifier!r}, account_id={ACCOUNT!r},
        producer_id="strategy-process", control=ControlEpoch("execution-service", {epoch}),
        kind=CommandKind.SUBMIT, submitted_at=now, payload=intent))
    print(queued.status.value)
"""
    return subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=30, creationflags=PROCESS_FLAGS
    )


def test_template_config_and_live_mode_are_refused(tmp_path):
    assert run_execution_service.main(["--config", "config/settings.yaml.example", "--dry-run"]) == 2
    settings = write_settings(tmp_path, mode="live")
    assert run_script(settings, "--dry-run") == 2
    settings_data = yaml.safe_load(settings.read_text(encoding="utf-8"))
    spec = spec_from_settings(settings_data, config_path=settings, mode="live", trading_day=DAY)
    with pytest.raises(AssemblyError):
        assemble(spec)


def test_expected_trading_day_must_be_explicit(tmp_path):
    # 交易日按交易所日历归属 (夜盘属于下一交易日)，不能静默取 UTC 或本地自然日
    settings = write_settings(tmp_path)
    with pytest.raises(AssemblyError, match="trading day"):
        spec_from_settings(yaml.safe_load(settings.read_text(encoding="utf-8")), config_path=settings)
    assert run_execution_service.main(["--config", str(settings), "--dry-run"]) == 2


def test_economics_come_from_catalog_and_registry():
    economics = economics_from_catalog(ROOT / "config" / "contract_catalog_s4_2024v1.json", ["SHFE.rb2410"])
    assert economics[RB].multiplier == Decimal("10") and economics[RB].price_tick == Decimal("1")
    assert "catalog:s4-2024v1" in economics[RB].source and "product_registry" in economics[RB].source
    with pytest.raises(MissingRuleError):
        economics_from_catalog(ROOT / "config" / "contract_catalog_s4_2024v1.json", ["SHFE.rb9999"])


def test_paper_bootstrap_processes_cross_process_commands_and_restarts_with_new_epoch(tmp_path):
    settings = write_settings(tmp_path)
    db = tmp_path / "trading.db"
    # 1. 首次启动：申请并受理控制权 (无旧出口)，对账放行，跑几轮
    assert run_script(settings, "--request-control", "bootstrap", "--max-iterations", "2") == 0
    heartbeat = read_heartbeat(tmp_path / "hb.json")
    assert heartbeat is not None and heartbeat.ready and heartbeat.control_epoch == 1
    manifest = json.loads(next((tmp_path / "out").rglob("run_manifest.json")).read_text(encoding="utf-8"))
    assert manifest["control"] == {"controller_id": "execution-service", "epoch": 1}
    assert manifest["gateway"] == "SimulatedGateway" and manifest["query_source"] == "PaperQueryAdapter"

    # 2. 另一进程投递委托 (带当前代次)，服务重启后消费并收到模拟回报
    result = submit_from_another_process(db, "strategy-order-1", 1)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "PENDING"
    assert run_script(settings, "--max-iterations", "4") == 0
    with SQLiteCommandClient(db, account_id=ACCOUNT) as client:
        assert client.get("strategy-order-1").status == CommandStatus.SENT_UNKNOWN
    journal, model = open_model_read_only(db, ACCOUNT, ROOT / "config" / "contract_catalog_s4_2024v1.json")
    try:
        order = model.orders.get_order("strategy-order-1")
        assert order is not None and order.status == OrderStatus.ACCEPTED
        assert model.ledger.get_funds_reservation("strategy-order-1") is not None
    finally:
        journal.close()

    # 3. 旧代次命令在新代次接管后被拒；接管需要操作员确认隔离
    assert run_script(settings, "--request-control", "operator takeover", "--max-iterations", "1") == 3
    assert (
        run_script(settings, "--request-control", "operator takeover", "--confirm-isolated", "--max-iterations", "1")
        == 0
    )
    stale = submit_from_another_process(db, "strategy-order-stale", 1)
    assert stale.returncode == 0, stale.stderr
    assert run_script(settings, "--max-iterations", "2") == 0
    with SQLiteCommandClient(db, account_id=ACCOUNT) as client:
        assert client.get("strategy-order-stale").status == CommandStatus.REJECTED_STALE
    assert read_heartbeat(tmp_path / "hb.json").control_epoch == 2


def test_paper_isolation_requires_operator_confirmation_after_first_control():
    isolation = PaperIsolation(operator_confirmed=False)
    assert isolation.isolate(None, None) is True
    assert isolation.isolate(object(), None) is False
    assert PaperIsolation(operator_confirmed=True).isolate(object(), None) is True


def test_restart_reconciliation_compares_the_published_account_state(tmp_path):
    settings = write_settings(tmp_path)
    db = tmp_path / "trading.db"
    assert run_script(settings, "--request-control", "bootstrap", "--max-iterations", "1") == 0
    assert submit_from_another_process(db, "strategy-order-1", 1).returncode == 0
    assert run_script(settings, "--max-iterations", "4") == 0
    spec = spec_from_settings(
        yaml.safe_load(settings.read_text(encoding="utf-8")),
        config_path=settings,
        trading_day=DAY,
        heartbeat_path=str(tmp_path / "hb.json"),
    )

    # 纸面柜台视图从 Journal 中已持久化的回报重建：活动单两边一致，可以放行
    assembled = assemble(spec)
    try:
        batch = QueryBatch("probe", ACCOUNT, DAY, datetime.now(timezone.utc))
        assert [u.identity.client_order_id for u in assembled.query.query_orders(batch).records] == ["strategy-order-1"]
        assembled.reconcile_and_enable()
        assert assembled.service.ready
    finally:
        assembled.close()

    # 柜台查询里缺少本地活动单：对账必须按已发布的本地事实阻塞，而不是拿空账本比对后放行
    assembled = assemble(spec)
    try:
        assembled.query._orders.clear()
        with pytest.raises(ExecutionNotReadyError):
            assembled.reconcile_and_enable()
        assert not assembled.service.ready
        assert [diff.identifier for diff in assembled.recovery.report.blocking] == ["strategy-order-1"]
    finally:
        assembled.close()


def test_instance_cannot_trade_under_another_controllers_epoch(tmp_path, capsys):
    settings = write_settings(tmp_path)
    assert run_script(settings, "--request-control", "bootstrap", "--max-iterations", "1") == 0
    capsys.readouterr()
    assert run_script(settings, "--controller-id", "second-instance", "--max-iterations", "1") == 3
    assert "second-instance" in capsys.readouterr().err
    # 显式申请并受理接管后才可运行 (纸面模式需操作员确认旧出口已隔离)
    assert (
        run_script(
            settings,
            "--controller-id",
            "second-instance",
            "--request-control",
            "operator takeover",
            "--confirm-isolated",
            "--max-iterations",
            "1",
        )
        == 0
    )
    assert read_heartbeat(tmp_path / "hb.json").control_epoch == 2


def test_heartbeat_write_failure_does_not_stop_the_trading_loop(tmp_path, monkeypatch):
    settings = write_settings(tmp_path)
    assert run_script(settings, "--request-control", "bootstrap", "--max-iterations", "1") == 0
    spec = spec_from_settings(
        yaml.safe_load(settings.read_text(encoding="utf-8")),
        config_path=settings,
        trading_day=DAY,
        heartbeat_path=str(tmp_path / "hb.json"),
    )
    assembled = assemble(spec)
    try:

        def locked(**_: object) -> None:
            raise PermissionError("target held open by the watchdog")

        monkeypatch.setattr(assembled.heartbeat, "beat", locked)
        assembled.step()
        assembled.step()
        assert assembled.heartbeat_failures == 2
    finally:
        assembled.close()


def test_parse_statement_script_reconciles_and_holds_on_mismatch(tmp_path, capsys):
    settings = write_settings(tmp_path)
    db = tmp_path / "trading.db"
    assert run_script(settings, "--request-control", "bootstrap", "--max-iterations", "1") == 0
    consistent = {
        "account_id": ACCOUNT,
        "trading_day": DAY.isoformat(),
        "kind": "MTM",
        "summary": {"balance_end": "100000.00", "close_pnl": "0", "mtm_pnl": "0", "commission": "0"},
        "positions": [],
        "trades": [],
    }
    good = tmp_path / "good.json"
    good.write_text(json.dumps(consistent), encoding="utf-8")
    report = tmp_path / "report.md"
    assert parse_statement.main([str(good), "--journal", str(db), "--out", str(report)]) == 0
    assert "一致" in report.read_text(encoding="utf-8")

    bad = tmp_path / "bad.json"
    mismatch = dict(consistent) | {"summary": {"balance_end": "99000.00"}}
    bad.write_text(json.dumps(mismatch), encoding="utf-8")
    capsys.readouterr()
    assert parse_statement.main([str(bad), "--journal", str(db), "--hold", "broker statement differs"]) == 2
    written = [line.split()[-1] for line in capsys.readouterr().err.splitlines() if "REDUCE_ONLY 命令" in line]
    assert len(written) == 1
    with SQLiteCommandClient(db, account_id=ACCOUNT) as client:
        hold = client.get(written[0])
        assert hold is not None and hold.command.kind == CommandKind.REDUCE_ONLY
        assert hold.status == CommandStatus.PENDING
    # 执行服务受理保护命令后进入只减仓
    assert run_script(settings, "--max-iterations", "2") == 0
    with SQLiteCommandClient(db, account_id=ACCOUNT) as client:
        assert client.get(written[0]).status == CommandStatus.COMPLETED
    journal, model = open_model_read_only(db, ACCOUNT, ROOT / "config" / "contract_catalog_s4_2024v1.json")
    try:
        assert model.risk.risk_state == RiskState.REDUCE_ONLY
    finally:
        journal.close()


def test_parse_statement_script_accepts_local_json_and_reports_format_errors(tmp_path):
    local = tmp_path / "local.json"
    local.write_text(
        json.dumps(
            {"balance_end": "1080.00", "close_pnl": "-20.00", "mtm_pnl": "0", "commission": "0", "positions": []}
        ),
        encoding="utf-8",
    )
    fixture = ROOT / "tests" / "fixtures" / "statements" / "a22_cross_day_mtm.txt"
    dump = tmp_path / "normalized.json"
    assert parse_statement.main([str(fixture), "--ledger-json", str(local), "--dump-normalized", str(dump)]) == 0
    assert json.loads(dump.read_text(encoding="utf-8"))[0]["summary"]["balance_end"] == "1080.00"
    broken = tmp_path / "broken.txt"
    broken.write_text("no header here", encoding="utf-8")
    assert parse_statement.main([str(broken), "--ledger-json", str(local)]) == 3
    assert parse_statement.main([str(fixture)]) == 2
    # 本地口径缺项明确失败，不当作 0 去比
    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text(json.dumps({"balance_end": "1080.00", "positions": []}), encoding="utf-8")
    assert parse_statement.main([str(fixture), "--ledger-json", str(incomplete)]) == 3


def test_statement_mapping_kind_defaults_to_mtm():
    statement = statement_from_mapping(
        {"account_id": "x", "trading_day": "2024-09-11", "summary": {"balance_end": "1"}}
    )
    assert statement.kind == "MTM"


def test_read_only_model_rebuild_does_not_take_the_execution_lock(tmp_path):
    settings = write_settings(tmp_path)
    db = tmp_path / "trading.db"
    assert run_script(settings, "--request-control", "bootstrap", "--max-iterations", "1") == 0
    journal, model = open_model_read_only(db, ACCOUNT, ROOT / "config" / "contract_catalog_s4_2024v1.json")
    try:
        assert model.ledger.balance == Decimal("100000")
        # 执行锁未被只读重建占用：服务仍可启动
        assert run_script(settings, "--max-iterations", "1") == 0
    finally:
        journal.close()
    with SQLiteJournal(db, account_id=ACCOUNT) as check:
        assert check.load_control_record().epoch.epoch == 1


def test_unknown_epoch_command_is_left_pending_until_service_rejects_it(tmp_path):
    settings = write_settings(tmp_path)
    db = tmp_path / "trading.db"
    assert run_script(settings, "--request-control", "bootstrap", "--max-iterations", "1") == 0
    now = datetime.now(timezone.utc)
    intent = OrderIntent(
        client_order_id="wrong-epoch",
        account_id=ACCOUNT,
        strategy_id="s",
        instrument=RB,
        side=Side.SELL,
        offset=Offset.OPEN,
        quantity=1,
        order_type=OrderType.LIMIT,
        created_at=now,
        limit_price_ticks=3400,
    )
    from qh_trader.core.objects import ControlEpoch

    with SQLiteCommandClient(db, account_id=ACCOUNT) as client:
        client.submit(
            ExecutionCommand(
                command_id="wrong-epoch",
                account_id=ACCOUNT,
                producer_id="s",
                control=ControlEpoch("someone-else", 1),
                kind=CommandKind.SUBMIT,
                submitted_at=now,
                payload=intent,
            )
        )
    assert run_script(settings, "--max-iterations", "2") == 0
    with SQLiteCommandClient(db, account_id=ACCOUNT) as client:
        assert client.get("wrong-epoch").status == CommandStatus.REJECTED_STALE
    journal, model = open_model_read_only(db, ACCOUNT, ROOT / "config" / "contract_catalog_s4_2024v1.json")
    try:
        assert model.orders.get_order("wrong-epoch") is None
        assert model.positions.get_position(RB, PositionSide.SHORT).total_position == 0
    finally:
        journal.close()
