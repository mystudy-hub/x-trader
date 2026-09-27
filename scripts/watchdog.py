#!/usr/bin/env python
"""[脚本工具] 看门狗进程：读取各角色心跳，告警、恢复通知与接管申请 (S5-06, FR-RISK-08, A23, ADR-X1).

用法：
    python scripts/watchdog.py --journal data_storage/live/trading.db --account ACC \\
        --execution-heartbeat runs/live/heartbeat/ACC-execution.json \\
        --watch strategy=runs/live/heartbeat/ACC-strategy.json:10:takeover --applicant standby-service

看门狗只读心跳文件与控制记录，决策结果写成 JSON 行 (``--alerts``，默认标准输出)。接管申请是一条
``TAKEOVER_REQUEST`` 命令；它不授予交易权，须由新的执行服务实例 (``run_execution_service.py --take-over``)
按"隔离 → 提升代次 → 对账 → 放行"受理。执行服务自身失联只告警。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.core.execution import ExecutionCommand  # noqa: E402
from qh_trader.infrastructure.command_queue import SQLiteCommandClient  # noqa: E402
from qh_trader.monitor import control  # noqa: E402
from qh_trader.monitor.watchdog import (  # noqa: E402
    EXECUTION_ROLE,
    DecisionKind,
    Watchdog,
    WatchdogAction,
    WatchdogDecision,
    WatchedRole,
    describe,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="心跳看门狗：只告警与申请接管，不交易")
    parser.add_argument("--journal", required=True, help="交易库 trading.db (写入接管申请)")
    parser.add_argument("--account", required=True, help="账户标识")
    parser.add_argument("--execution-heartbeat", required=True, help="执行服务心跳文件")
    parser.add_argument("--execution-timeout", type=float, default=5.0, help="执行服务心跳超时 (秒)")
    parser.add_argument(
        "--watch",
        action="append",
        default=[],
        metavar="ROLE=PATH:TIMEOUT[:takeover]",
        help="额外监视的角色；带 :takeover 时超时后申请接管",
    )
    parser.add_argument("--applicant", default=None, help="接管申请人 (新执行服务实例的控制者标识)")
    parser.add_argument("--interval", type=float, default=1.0, help="检查周期 (秒)")
    parser.add_argument("--max-iterations", type=int, default=None, help="运行固定轮数后退出 (测试 / 演练)")
    parser.add_argument("--alerts", default=None, help="决策记录 JSON 行输出文件 (默认标准输出)")
    return parser


def parse_watch(spec: str) -> WatchedRole:
    role, separator, rest = spec.partition("=")
    parts = rest.rsplit(":", 2) if separator else []
    action = WatchdogAction.ALERT
    if len(parts) == 3 and parts[2] == "takeover":
        action = WatchdogAction.REQUEST_TAKEOVER
        path, timeout = parts[0], parts[1]
    elif len(parts) >= 2:
        # 路径本身可能含盘符冒号：只有最后一段是超时
        path, timeout = rest.rsplit(":", 1)
    else:
        raise ValueError(f"--watch expects ROLE=PATH:TIMEOUT[:takeover], got {spec!r}")
    try:
        seconds = float(timeout)
    except ValueError as exc:
        raise ValueError(f"--watch timeout must be seconds, got {timeout!r}") from exc
    return WatchedRole(role.strip(), Path(path), seconds, action)


def takeover_command(
    decision: WatchdogDecision, *, client: SQLiteCommandClient, account_id: str, applicant: str
) -> ExecutionCommand:
    """同一事件、同一观测代次只对应一条申请；看门狗重启后沿用已写入的那条，不另起一条."""
    current = client.control()
    incident = decision.status.incident_id
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in incident)
    identifier = f"watchdog-takeover-{safe}-e{0 if current is None else current.epoch.epoch}"
    existing = client.get(identifier)
    if existing is not None:
        return existing.command
    return control.takeover_request(
        account_id=account_id,
        applicant_id=applicant,
        current=current,
        reason=f"watchdog: {decision.reason}",
        at=datetime.now(timezone.utc),
        producer_id=f"watchdog:{applicant}",
        identifier=identifier,
    )


def run(
    watchdog: Watchdog,
    *,
    client: SQLiteCommandClient,
    account_id: str,
    applicant: str | None,
    out: TextIO,
    interval: float,
    max_iterations: int | None,
    sleep=time.sleep,
) -> int:
    """返回写入的接管申请条数；每轮决策逐条写成 JSON 行."""
    pending: dict[str, ExecutionCommand] = {}  # 事件号 -> 已构造的申请，写入失败时原样重试 (幂等)
    submitted = 0
    iteration = 0
    while max_iterations is None or iteration < max_iterations:
        iteration += 1
        _, decisions = watchdog.evaluate()
        for decision in decisions:
            record: dict[str, object] = {
                "at": datetime.now(timezone.utc).isoformat(),
                "decision": decision.kind.value,
                "reason": decision.reason,
                **describe(decision.status, now_wall=datetime.now(timezone.utc)),
            }
            if decision.kind == DecisionKind.REQUEST_TAKEOVER:
                incident = decision.status.incident_id
                try:
                    if applicant is None:
                        raise control.ControlRefusedError("no --applicant configured; takeover cannot be requested")
                    command = pending.get(incident) or takeover_command(
                        decision, client=client, account_id=account_id, applicant=applicant
                    )
                    pending[incident] = command
                    queued = client.submit(command)
                except control.ControlRefusedError as exc:
                    # 申请人已持有控制权或未配置：记录即可，不重试
                    record["takeover_refused"] = str(exc)
                except Exception as exc:  # noqa: BLE001 - 写库失败保守处理为未申请，下一轮重试
                    watchdog.takeover_failed(incident)
                    record["takeover_error"] = f"{type(exc).__name__}: {exc}"
                else:
                    submitted += 1
                    record["command_id"] = command.command_id
                    record["command_status"] = queued.status.value
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()
        if max_iterations is None or iteration < max_iterations:
            sleep(interval)
    return submitted


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    journal = Path(args.journal) if Path(args.journal).is_absolute() else ROOT / args.journal
    try:
        roles = [WatchedRole(EXECUTION_ROLE, Path(args.execution_heartbeat), args.execution_timeout)]
        roles.extend(parse_watch(spec) for spec in args.watch)
        watchdog = Watchdog(roles)
    except ValueError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    if any(role.action == WatchdogAction.REQUEST_TAKEOVER for role in roles) and not args.applicant:
        print("配置了 :takeover 的角色必须同时给出 --applicant", file=sys.stderr)
        return 2
    if not journal.exists():
        print(f"交易库不存在: {journal}", file=sys.stderr)
        return 2
    # 只告警的配置连命令表也不写：只读打开
    read_only = not any(role.action == WatchdogAction.REQUEST_TAKEOVER for role in roles)
    stream: TextIO = sys.stdout
    try:
        if args.alerts:
            path = Path(args.alerts)
            path.parent.mkdir(parents=True, exist_ok=True)
            stream = path.open("a", encoding="utf-8")
        with SQLiteCommandClient(journal, account_id=args.account, read_only=read_only) as client:
            run(
                watchdog,
                client=client,
                account_id=args.account,
                applicant=args.applicant,
                out=stream,
                interval=args.interval,
                max_iterations=args.max_iterations,
            )
    except KeyboardInterrupt:
        pass
    finally:
        if stream is not sys.stdout:
            stream.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
