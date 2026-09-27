"""S5-06 看门狗与人工控制：存活判断、单次告警、接管申请条件、人工命令的本地拒绝与经执行服务的端到端执行."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from qh_trader.core.constants import Exchange, Offset, OrderStatus, Side
from qh_trader.core.execution import CommandKind, CommandStatus
from qh_trader.core.objects import ControlEpoch, ControlRecord, InstrumentId, OrderIdentity
from qh_trader.domain.risk import RiskState
from qh_trader.infrastructure.command_queue import SQLiteCommandClient
from qh_trader.monitor import control
from qh_trader.monitor.heartbeat import Heartbeat
from qh_trader.monitor.watchdog import (
    EXECUTION_ROLE,
    DecisionKind,
    Liveness,
    Watchdog,
    WatchdogAction,
    WatchedRole,
)
from scripts import control as control_script
from scripts import watchdog as watchdog_script
from scripts.live_assembly import open_model_read_only
from tests.unit.test_s5_scripts import ACCOUNT, ROOT, run_script, submit_from_another_process, write_settings

NOW = datetime(2024, 9, 10, 1, 30, tzinfo=timezone.utc)
RB = InstrumentId(Exchange.SHFE, "rb2410")
CATALOG = str(ROOT / "config" / "contract_catalog_s4_2024v1.json")


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class Beats:
    """按路径返回心跳；值为异常时读取抛出 (模拟损坏或被占用的文件)."""

    def __init__(self) -> None:
        self.values: dict[Path, object] = {}

    def set(self, path: str, sequence: int, *, instance: str = "a", epoch: int | None = 1, ready: bool = True):
        self.values[Path(path)] = Heartbeat("r", instance, 0.0, NOW, sequence, epoch, ready)

    def __call__(self, path: Path) -> Heartbeat | None:
        value = self.values.get(Path(path))
        if isinstance(value, Exception):
            raise value
        return value


def make_watchdog(clock: Clock, beats: Beats, *, strategy_action=WatchdogAction.REQUEST_TAKEOVER) -> Watchdog:
    return Watchdog(
        [WatchedRole(EXECUTION_ROLE, Path("exec"), 5), WatchedRole("strategy", Path("strat"), 10, strategy_action)],
        monotonic=clock,
        reader=beats,
    )


def record(controller: str = "execution-service", epoch: int = 1) -> ControlRecord:
    return ControlRecord(ControlEpoch(controller, epoch), NOW, 1)


# ---- 存活判断 ----


def test_liveness_follows_sequence_progress_on_the_watchdog_clock():
    clock, beats = Clock(), Beats()
    watchdog = make_watchdog(clock, beats, strategy_action=WatchdogAction.ALERT)
    monitor = watchdog.monitors["strategy"]
    assert monitor.observe().liveness == Liveness.WAITING
    clock.now += 11
    assert monitor.observe().liveness == Liveness.MISSING
    beats.set("strat", 1)
    assert monitor.observe().liveness == Liveness.ALIVE
    # 心跳文件仍在，但序号不推进：墙钟内容再新也不算存活
    clock.now += 11
    assert monitor.observe().liveness == Liveness.STALE
    # 进程重启：实例号变化即算推进，即使序号回到 1
    beats.set("strat", 1, instance="b")
    assert monitor.observe().liveness == Liveness.ALIVE
    # 损坏文件不是存活证据，超时后报 UNREADABLE
    beats.values[Path("strat")] = ValueError("broken json")
    clock.now += 11
    status = monitor.observe()
    assert status.liveness == Liveness.UNREADABLE and status.read_error == "ValueError"
    assert monitor.read_failures == 1


def test_execution_watch_cannot_request_takeover_and_must_be_present():
    with pytest.raises(ValueError, match="arbitrate"):
        WatchedRole(EXECUTION_ROLE, Path("exec"), 5, WatchdogAction.REQUEST_TAKEOVER)
    with pytest.raises(ValueError, match="execution service"):
        Watchdog([WatchedRole("strategy", Path("strat"), 5)])
    with pytest.raises(ValueError, match="positive"):
        WatchedRole("strategy", Path("strat"), 0)


# ---- 决策 ----


def test_each_incident_alerts_once_and_requests_takeover_once():
    clock, beats = Clock(), Beats()
    watchdog = make_watchdog(clock, beats)
    beats.set("exec", 1)
    beats.set("strat", 1)
    _, decisions = watchdog.evaluate()
    assert [d.kind for d in decisions] == [DecisionKind.CONTROL_CHANGED]
    clock.now += 11
    beats.set("exec", 2)
    _, decisions = watchdog.evaluate()
    assert [d.kind for d in decisions] == [DecisionKind.ALERT, DecisionKind.REQUEST_TAKEOVER]
    incident = decisions[1].status.incident_id
    assert incident == "strategy:a:1"
    beats.set("exec", 3)
    assert watchdog.evaluate()[1] == []
    # 写入申请失败：同一事件下一轮重试
    watchdog.takeover_failed(incident)
    beats.set("exec", 4)
    assert [d.kind for d in watchdog.evaluate()[1]] == [DecisionKind.REQUEST_TAKEOVER]
    beats.set("strat", 2)
    beats.set("exec", 5)
    assert [d.kind for d in watchdog.evaluate()[1]] == [DecisionKind.RECOVERED]


def test_takeover_is_deferred_while_the_execution_service_is_lost_or_not_ready():
    clock, beats = Clock(), Beats()
    watchdog = make_watchdog(clock, beats)
    beats.set("exec", 1, ready=False)
    beats.set("strat", 1)
    watchdog.evaluate()
    clock.now += 11
    beats.set("exec", 2, ready=False)
    _, decisions = watchdog.evaluate()
    assert [d.kind for d in decisions] == [DecisionKind.ALERT, DecisionKind.TAKEOVER_DEFERRED]
    assert "not ready" in decisions[1].reason
    beats.set("exec", 3, ready=False)
    assert watchdog.evaluate()[1] == []
    # 执行服务就绪后，同一事件才真正申请
    beats.set("exec", 4, ready=True)
    assert [d.kind for d in watchdog.evaluate()[1]] == [DecisionKind.REQUEST_TAKEOVER]


def test_lost_execution_service_only_alerts():
    clock, beats = Clock(), Beats()
    watchdog = make_watchdog(clock, beats)
    beats.set("exec", 1)
    beats.set("strat", 1)
    watchdog.evaluate()
    clock.now += 11
    kinds = sorted(d.kind.value for d in watchdog.evaluate()[1])
    assert kinds == ["alert", "alert", "takeover_deferred"]


# ---- 人工命令 ----


def test_operator_commands_require_the_current_controller():
    with pytest.raises(control.ControlRefusedError, match="no control record"):
        control.pause(account_id=ACCOUNT, operator_id="ops", current=None, reason="x", at=NOW)
    with pytest.raises(control.ControlRefusedError, match="not the current controller"):
        control.pause(account_id=ACCOUNT, operator_id="ops", current=record(), reason="x", at=NOW)
    with pytest.raises(control.ControlRefusedError, match="reason"):
        control.pause(account_id=ACCOUNT, operator_id="execution-service", current=record(), reason="  ", at=NOW)
    command = control.reduce_only(
        account_id=ACCOUNT, operator_id="execution-service", current=record(epoch=3), reason="margin", at=NOW
    )
    assert command.kind == CommandKind.REDUCE_ONLY and command.control == ControlEpoch("execution-service", 3)
    assert command.payload == {"reason": "margin"}


def test_resume_needs_both_confirmations():
    kwargs = dict(account_id=ACCOUNT, operator_id="execution-service", current=record(), reason="ok", at=NOW)
    for cleared, consistent in ((False, True), (True, False)):
        with pytest.raises(control.ControlRefusedError, match="resume requires"):
            control.resume(cause_cleared=cleared, account_consistent=consistent, **kwargs)
    command = control.resume(cause_cleared=True, account_consistent=True, **kwargs)
    assert command.payload == {"reason": "ok", "cause_cleared": True, "account_consistent": True}


def test_manual_close_and_cancel_refuse_guessing():
    kwargs = dict(account_id=ACCOUNT, operator_id="execution-service", current=record(), at=NOW)
    with pytest.raises(control.ControlRefusedError, match="CLOSE_TODAY"):
        control.close_position(
            instrument=RB, side=Side.SELL, offset=Offset.CLOSE, quantity=1, limit_price_ticks=3500, **kwargs
        )
    with pytest.raises(ValueError):
        control.close_position(
            instrument=RB, side=Side.SELL, offset=Offset.CLOSE_TODAY, quantity=0, limit_price_ticks=3500, **kwargs
        )
    command = control.close_position(
        instrument=RB, side=Side.SELL, offset=Offset.CLOSE_YESTERDAY, quantity=2, limit_price_ticks=3500, **kwargs
    )
    assert command.kind == CommandKind.SUBMIT and command.payload.strategy_id == control.OPERATOR_STRATEGY_ID
    with pytest.raises(control.ControlRefusedError, match="session triple"):
        control.cancel(
            identity=OrderIdentity(account_id=ACCOUNT, exchange=Exchange.SHFE, client_order_id="o1"), **kwargs
        )
    identity = OrderIdentity(
        account_id=ACCOUNT, exchange=Exchange.SHFE, client_order_id="o1", front_id=1, session_id=-7, order_ref="12"
    )
    assert control.cancel(identity=identity, **kwargs).payload == identity


def test_takeover_request_carries_the_observed_epoch_and_grants_nothing():
    first = control.takeover_request(account_id=ACCOUNT, applicant_id="standby", current=None, reason="boot", at=NOW)
    assert first.control == ControlEpoch("standby", 0)
    later = control.takeover_request(
        account_id=ACCOUNT, applicant_id="standby", current=record(epoch=4), reason="lost", at=NOW
    )
    assert later.kind == CommandKind.TAKEOVER_REQUEST and later.control == ControlEpoch("execution-service", 4)
    with pytest.raises(control.ControlRefusedError, match="already holds"):
        control.takeover_request(
            account_id=ACCOUNT, applicant_id="execution-service", current=record(), reason="x", at=NOW
        )


def test_watch_spec_parsing_keeps_windows_drive_letters():
    role = watchdog_script.parse_watch(r"strategy=C:\runs\hb.json:10:takeover")
    assert role.path == Path(r"C:\runs\hb.json") and role.timeout_s == 10
    assert role.action == WatchdogAction.REQUEST_TAKEOVER
    role = watchdog_script.parse_watch(r"recorder=C:\runs\rec.json:30")
    assert role.path == Path(r"C:\runs\rec.json") and role.action == WatchdogAction.ALERT
    with pytest.raises(ValueError):
        watchdog_script.parse_watch("strategy")


# ---- 端到端：人工入口与看门狗都只写命令，由执行服务校验执行 ----


def control_cli(db: Path, *args: str) -> int:
    return control_script.main(["--journal", str(db), "--account", ACCOUNT, *args])


def risk_state(db: Path) -> RiskState:
    journal, model = open_model_read_only(db, ACCOUNT, Path(CATALOG))
    try:
        return model.risk.risk_state
    finally:
        journal.close()


def test_operator_pause_and_resume_run_through_the_execution_service(tmp_path, capsys):
    settings = write_settings(tmp_path)
    db = tmp_path / "trading.db"
    assert run_script(settings, "--request-control", "bootstrap", "--max-iterations", "1") == 0

    # 非当前控制者：本地拒绝，不写入命令表
    assert control_cli(db, "--operator", "someone-else", "pause", "--reason", "x") == 2
    assert control_cli(db, "pause", "--reason", "x") == 2
    assert control_cli(db, "--operator", "execution-service", "pause", "--reason", "manual halt") == 0
    assert run_script(settings, "--max-iterations", "2") == 0
    assert risk_state(db) == RiskState.HALTED

    # 暂停期间新委托被风控拒绝
    assert submit_from_another_process(db, "order-while-halted", 1).returncode == 0
    assert run_script(settings, "--max-iterations", "2") == 0
    with SQLiteCommandClient(db, account_id=ACCOUNT) as client:
        assert client.get("order-while-halted").status == CommandStatus.REJECTED
        kinds = [item.command.kind for item in client.commands()]
        assert kinds.count(CommandKind.PAUSE) == 1

    # 恢复缺少确认即本地拒绝；两项确认齐全才写入
    assert control_cli(db, "--operator", "execution-service", "resume", "--reason", "ok", "--cause-cleared") == 2
    assert (
        control_cli(
            db, "--operator", "execution-service", "resume", "--reason", "ok", "--cause-cleared", "--account-consistent"
        )
        == 0
    )
    assert run_script(settings, "--max-iterations", "2") == 0
    assert risk_state(db) == RiskState.NORMAL

    capsys.readouterr()
    assert control_cli(db, "status") == 0
    status = json.loads(capsys.readouterr().out)
    assert status["control"]["controller_id"] == "execution-service" and status["control"]["epoch"] == 1
    assert status["risk_state"] == RiskState.NORMAL.value and status["open_commands"] == []


def test_operator_cancel_uses_the_persisted_order_identity(tmp_path):
    settings = write_settings(tmp_path)
    db = tmp_path / "trading.db"
    assert run_script(settings, "--request-control", "bootstrap", "--max-iterations", "1") == 0
    assert submit_from_another_process(db, "order-to-cancel", 1).returncode == 0
    assert run_script(settings, "--max-iterations", "4") == 0
    assert (
        control_cli(
            db, "--operator", "execution-service", "cancel", "--client-order-id", "missing", "--catalog", CATALOG
        )
        == 2
    )
    assert (
        control_cli(
            db,
            "--operator",
            "execution-service",
            "cancel",
            "--client-order-id",
            "order-to-cancel",
            "--catalog",
            CATALOG,
        )
        == 0
    )
    with SQLiteCommandClient(db, account_id=ACCOUNT) as client:
        cancels = [item for item in client.commands() if item.command.kind == CommandKind.CANCEL]
        assert len(cancels) == 1 and cancels[0].command.payload.client_order_id == "order-to-cancel"
    # 撤单经执行服务的代次与账户模型校验后到达网关。纸面网关每次启动都是新的内存实例，不认识上一进程的
    # 在途委托 (07 A04 纸面重启的既有限制)，于是明确回答 NOT_SENT：撤单挂起被释放，原单仍在
    assert run_script(settings, "--max-iterations", "4") == 0
    with SQLiteCommandClient(db, account_id=ACCOUNT) as client:
        assert client.get(cancels[0].command.command_id).status == CommandStatus.NOT_SENT
    journal, model = open_model_read_only(db, ACCOUNT, Path(CATALOG))
    try:
        order = model.orders.get_order("order-to-cancel")
        assert order.status == OrderStatus.ACCEPTED and not order.cancel_pending
    finally:
        journal.close()


class StubOrder:
    def __init__(self, *, status=OrderStatus.ACCEPTED, cancel_pending=False, identity=None) -> None:
        self.status, self.cancel_pending, self.identity = status, cancel_pending, identity

    @property
    def is_terminal(self) -> bool:
        return self.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED)


def test_operator_cancel_refuses_orders_the_service_would_reject(tmp_path, monkeypatch, capsys):
    settings = write_settings(tmp_path)
    db = tmp_path / "trading.db"
    assert run_script(settings, "--request-control", "bootstrap", "--max-iterations", "1") == 0
    identity = OrderIdentity(
        account_id=ACCOUNT, exchange=Exchange.SHFE, client_order_id="o1", front_id=1, session_id=-7, order_ref="12"
    )
    cases = {
        "already": StubOrder(status=OrderStatus.CANCELLED, identity=identity),
        "pending": StubOrder(cancel_pending=True, identity=identity),
        "counter identity": StubOrder(),
    }
    for expected, order in cases.items():

        class Orders:
            @staticmethod
            def get_order(_: str, order=order) -> StubOrder:
                return order

        class Model:
            orders = Orders()

        class Journal:
            @staticmethod
            def close() -> None:
                pass

        monkeypatch.setattr(control_script, "open_model_read_only", lambda *_: (Journal(), Model()))
        capsys.readouterr()
        args = ("--operator", "execution-service", "cancel", "--client-order-id", "o1", "--catalog", CATALOG)
        assert control_cli(db, *args) == 2
        assert expected in capsys.readouterr().err
    with SQLiteCommandClient(db, account_id=ACCOUNT) as client:
        assert [item.command.kind for item in client.commands()].count(CommandKind.CANCEL) == 0


def run_watchdog(db: Path, heartbeat_dir: Path, iterations: int) -> tuple[int, list[dict]]:
    """新起一个看门狗进程的等价物：策略心跳停在序号 1，执行服务心跳每轮推进."""
    clock, beats = Clock(), Beats()
    execution, strategy = heartbeat_dir / "hb.json", heartbeat_dir / "strategy.json"
    watchdog = Watchdog(
        [
            WatchedRole(EXECUTION_ROLE, execution, 5),
            WatchedRole("strategy", strategy, 10, WatchdogAction.REQUEST_TAKEOVER),
        ],
        monotonic=clock,
        reader=beats,
    )
    beats.set(str(execution), 1)
    beats.set(str(strategy), 1)

    def tick(_: float) -> None:
        clock.now += 6
        beats.set(str(execution), beats.values[execution].sequence + 1)

    out = io.StringIO()
    with SQLiteCommandClient(db, account_id=ACCOUNT) as client:
        submitted = watchdog_script.run(
            watchdog,
            client=client,
            account_id=ACCOUNT,
            applicant="standby",
            out=out,
            interval=0,
            max_iterations=iterations,
            sleep=tick,
        )
    return submitted, [json.loads(line) for line in out.getvalue().splitlines()]


def test_watchdog_requests_takeover_that_a_new_instance_must_accept(tmp_path):
    settings = write_settings(tmp_path)
    db = tmp_path / "trading.db"
    assert run_script(settings, "--request-control", "bootstrap", "--max-iterations", "1") == 0

    submitted, lines = run_watchdog(db, tmp_path, 4)
    assert submitted == 1
    assert [line["decision"] for line in lines] == ["control_changed", "alert", "request_takeover"]
    with SQLiteCommandClient(db, account_id=ACCOUNT) as client:
        requests = [item for item in client.commands() if item.command.kind == CommandKind.TAKEOVER_REQUEST]
        pending = [item for item in requests if item.status == CommandStatus.PENDING]
        assert len(pending) == 1 and pending[0].command.control == ControlEpoch("execution-service", 1)
        request_id = pending[0].command.command_id
    assert lines[-1]["command_id"] == request_id

    # 看门狗重启后面对同一事件：沿用已写入的申请，不另起一条，也不因内容不同而写入冲突
    submitted, lines = run_watchdog(db, tmp_path, 4)
    assert submitted == 1 and lines[-1]["command_id"] == request_id and "takeover_error" not in lines[-1]
    with SQLiteCommandClient(db, account_id=ACCOUNT) as client:
        mine = [item for item in client.commands() if item.command.producer_id == "watchdog:standby"]
        assert [item.command.command_id for item in mine] == [request_id]

    # 申请本身不改变控制权；新实例受理 (纸面需确认隔离) 后代次才提升
    with SQLiteCommandClient(db, account_id=ACCOUNT) as client:
        assert client.control().epoch == ControlEpoch("execution-service", 1)
    assert (
        run_script(
            settings,
            "--controller-id",
            "standby",
            "--take-over",
            request_id,
            "--confirm-isolated",
            "--max-iterations",
            "1",
        )
        == 0
    )
    with SQLiteCommandClient(db, account_id=ACCOUNT) as client:
        assert client.control().epoch == ControlEpoch("standby", 2)
        assert client.get(request_id).status == CommandStatus.COMPLETED
    # 旧控制者的人工命令此后在本地即被拒绝
    assert control_cli(db, "--operator", "execution-service", "pause", "--reason", "late") == 2


def test_watchdog_script_rejects_takeover_without_applicant(tmp_path):
    db = tmp_path / "trading.db"
    db.touch()
    code = watchdog_script.main(
        [
            "--journal",
            str(db),
            "--account",
            ACCOUNT,
            "--execution-heartbeat",
            str(tmp_path / "hb.json"),
            "--watch",
            f"strategy={tmp_path / 's.json'}:10:takeover",
            "--max-iterations",
            "1",
        ]
    )
    assert code == 2


def test_describe_reports_wall_age_for_display_only():
    clock, beats = Clock(), Beats()
    watchdog = make_watchdog(clock, beats)
    beats.set("exec", 1)
    beats.set("strat", 1)
    statuses, _ = watchdog.evaluate()
    summary = watchdog_script.describe(statuses[EXECUTION_ROLE], now_wall=NOW + timedelta(seconds=3))
    assert summary["wall_age_s"] == 3.0 and summary["liveness"] == Liveness.ALIVE.value
