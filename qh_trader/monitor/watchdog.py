"""[Monitor 适配器] 看门狗：心跳存活判断与接管申请决策 (S5-06, FR-RISK-08, A23, ADR-X1).

看门狗只观察、告警、申请，不交易：

- 存活按"心跳序号是否推进"判断，计时用看门狗自己的单调时钟，不比较跨进程时钟；心跳文件里的墙钟只用于
  展示。实例号变化 (进程重启) 也算推进。文件缺失、内容损坏都不当作存活。
- 每个异常事件只告警一次，恢复时再报一次恢复。
- 策略角色可配置为超时后**申请**接管：只在执行服务心跳仍在推进且已就绪时才申请，申请是一条
  ``TAKEOVER_REQUEST``，由新的执行服务实例按"隔离 → 提升代次 → 对账 → 放行"受理；心跳超时本身不授予交易权。
- 执行服务自身失联只告警：没有唯一出口就没有人能仲裁接管，旧出口无法隔离时不另建交易连接 (FR-RISK-08)。

本模块不写交易库、不连接柜台；把决策交给 ``scripts/watchdog.py`` 组装的命令客户端执行。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from qh_trader.core.objects import require_text
from qh_trader.monitor.heartbeat import Heartbeat, read_heartbeat

EXECUTION_ROLE = "execution"


class Liveness(StrEnum):
    WAITING = "WAITING"  # 启动宽限期内还没见到心跳
    ALIVE = "ALIVE"
    STALE = "STALE"  # 见过心跳，但超过时限没有推进
    MISSING = "MISSING"  # 超过时限仍没有心跳文件
    UNREADABLE = "UNREADABLE"  # 超过时限，且最近一次读取失败 (损坏或被占用)


UNHEALTHY = frozenset({Liveness.STALE, Liveness.MISSING, Liveness.UNREADABLE})


class WatchdogAction(StrEnum):
    ALERT = "alert"
    REQUEST_TAKEOVER = "request_takeover"


class DecisionKind(StrEnum):
    ALERT = "alert"
    RECOVERED = "recovered"
    REQUEST_TAKEOVER = "request_takeover"
    TAKEOVER_DEFERRED = "takeover_deferred"
    CONTROL_CHANGED = "control_changed"


@dataclass(frozen=True, slots=True)
class WatchedRole:
    role: str
    path: Path
    timeout_s: float
    action: WatchdogAction = WatchdogAction.ALERT

    def __post_init__(self) -> None:
        require_text(self.role, "watched role")
        object.__setattr__(self, "path", Path(self.path))
        if isinstance(self.timeout_s, bool) or not isinstance(self.timeout_s, (int, float)) or self.timeout_s <= 0:
            raise ValueError(f"heartbeat timeout for {self.role} must be a positive number of seconds")
        object.__setattr__(self, "action", WatchdogAction(self.action))
        if self.role == EXECUTION_ROLE and self.action == WatchdogAction.REQUEST_TAKEOVER:
            raise ValueError("a lost execution service cannot arbitrate a takeover; its watch can only alert")


@dataclass(frozen=True, slots=True)
class RoleStatus:
    role: str
    liveness: Liveness
    silent_for_s: float
    heartbeat: Heartbeat | None
    read_error: str | None

    @property
    def incident_id(self) -> str:
        """最后一次观测到的心跳标识；同一事件重复评估得到同一个值，用于幂等申请."""
        beat = self.heartbeat
        return f"{self.role}:{'none' if beat is None else beat.instance_id}:{0 if beat is None else beat.sequence}"


@dataclass(frozen=True, slots=True)
class WatchdogDecision:
    kind: DecisionKind
    status: RoleStatus
    reason: str


class HeartbeatMonitor:
    """单个角色的存活判断；只信任本进程单调时钟上的"最近一次看到心跳推进"的时刻."""

    def __init__(
        self,
        watched: WatchedRole,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        reader: Callable[[Path], Heartbeat | None] = read_heartbeat,
    ) -> None:
        self.watched = watched
        self._monotonic = monotonic
        self._reader = reader
        self._started = monotonic()
        self._last_progress: float | None = None
        self._last: Heartbeat | None = None
        self.read_failures = 0

    def observe(self) -> RoleStatus:
        now = self._monotonic()
        error: str | None = None
        try:
            beat = self._reader(self.watched.path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # 损坏或正被替换的文件不是存活证据；推进时刻保持不变
            self.read_failures += 1
            beat, error = self._last, type(exc).__name__
        else:
            previous = self._last
            if beat is not None and (
                previous is None or (beat.instance_id, beat.sequence) != (previous.instance_id, previous.sequence)
            ):
                self._last_progress = now
            if beat is not None:
                self._last = beat
        silent = now - (self._last_progress if self._last_progress is not None else self._started)
        if silent <= self.watched.timeout_s:
            state = Liveness.ALIVE if self._last_progress is not None else Liveness.WAITING
        elif error is not None:
            state = Liveness.UNREADABLE
        elif self._last is None:
            state = Liveness.MISSING
        else:
            state = Liveness.STALE
        return RoleStatus(self.watched.role, state, round(silent, 3), self._last, error)


class Watchdog:
    """汇总各角色的存活状态并给出决策；每个异常事件只告警 / 申请一次."""

    def __init__(
        self,
        roles: Sequence[WatchedRole],
        *,
        monotonic: Callable[[], float] = time.monotonic,
        reader: Callable[[Path], Heartbeat | None] = read_heartbeat,
    ) -> None:
        names = [role.role for role in roles]
        if len(set(names)) != len(names):
            raise ValueError("each watched role must be unique")
        if EXECUTION_ROLE not in names:
            raise ValueError("the watchdog must also watch the execution service heartbeat")
        self.monitors = {role.role: HeartbeatMonitor(role, monotonic=monotonic, reader=reader) for role in roles}
        self._open: dict[str, str] = {}  # role -> 当前异常事件号
        self._requested: set[str] = set()
        self._deferred: set[str] = set()
        self._control_epoch: int | None = None

    def evaluate(self) -> tuple[dict[str, RoleStatus], list[WatchdogDecision]]:
        statuses = {role: monitor.observe() for role, monitor in self.monitors.items()}
        decisions: list[WatchdogDecision] = []
        execution = statuses[EXECUTION_ROLE]
        for role, status in statuses.items():
            watched = self.monitors[role].watched
            if status.liveness in UNHEALTHY:
                incident = self._open.get(role)
                if incident is None:
                    incident = status.incident_id
                    self._open[role] = incident
                    decisions.append(
                        WatchdogDecision(
                            DecisionKind.ALERT,
                            status,
                            f"{role} heartbeat {status.liveness.value.lower()} for {status.silent_for_s:.1f}s "
                            f"(limit {watched.timeout_s:g}s)",
                        )
                    )
                if watched.action == WatchdogAction.REQUEST_TAKEOVER and incident not in self._requested:
                    decisions.extend(self._takeover(role, incident, status, execution))
            elif role in self._open:
                self._open.pop(role)
                decisions.append(WatchdogDecision(DecisionKind.RECOVERED, status, f"{role} heartbeat advancing again"))
        beat = execution.heartbeat
        if execution.liveness == Liveness.ALIVE and beat is not None and beat.control_epoch != self._control_epoch:
            if self._control_epoch is not None or beat.control_epoch is not None:
                decisions.append(
                    WatchdogDecision(
                        DecisionKind.CONTROL_CHANGED,
                        execution,
                        f"execution control epoch {self._control_epoch} -> {beat.control_epoch}",
                    )
                )
            self._control_epoch = beat.control_epoch
        return statuses, decisions

    def _takeover(self, role: str, incident: str, status: RoleStatus, execution: RoleStatus) -> list[WatchdogDecision]:
        beat = execution.heartbeat
        if execution.liveness == Liveness.ALIVE and beat is not None and beat.ready:
            self._requested.add(incident)
            return [
                WatchdogDecision(
                    DecisionKind.REQUEST_TAKEOVER,
                    status,
                    f"{role} heartbeat lost; applying for takeover through the execution service",
                )
            ]
        if incident in self._deferred:
            return []
        self._deferred.add(incident)
        return [
            WatchdogDecision(
                DecisionKind.TAKEOVER_DEFERRED,
                status,
                f"{role} heartbeat lost but the execution service is {execution.liveness.value.lower()}"
                f"{'' if beat is None or beat.ready else ' and not ready'}; alert only, new risk stays blocked",
            )
        ]

    def takeover_failed(self, incident: str) -> None:
        """申请写入失败：保守处理为未申请，下一轮在事件仍未恢复时重试 (幂等命令号)."""
        self._requested.discard(incident)


def describe(status: RoleStatus, *, now_wall: datetime | None = None) -> dict[str, object]:
    """告警与状态输出用的可序列化摘要；墙钟年龄只作展示，不参与判断."""
    beat = status.heartbeat
    summary: dict[str, object] = {
        "role": status.role,
        "liveness": status.liveness.value,
        "silent_for_s": status.silent_for_s,
        "read_error": status.read_error,
    }
    if beat is not None:
        summary.update(
            instance_id=beat.instance_id,
            sequence=beat.sequence,
            beat_wall=beat.beat_wall.isoformat(),
            control_epoch=beat.control_epoch,
            ready=beat.ready,
        )
        if now_wall is not None:
            summary["wall_age_s"] = round((now_wall - beat.beat_wall).total_seconds(), 3)
    return summary
