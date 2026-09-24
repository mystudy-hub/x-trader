"""[Monitor 适配器] 独立于交易库的心跳文件 (S5-04, ADR-X1).

心跳不入 ``trading.db``：同一库只有一把写锁，心跳更新会与命令事务抢锁。每个角色 (策略、执行服务)
向自己的心跳文件原子写入 ``beat_monotonic`` (单调时钟，用于超时判断) 与 ``beat_wall`` (墙钟，只用于展示与审计)。
看门狗读取文件判断存活；心跳超时本身不授予交易权 (FR-RISK-08)。
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from qh_trader.core.objects import require_text

REPLACE_ATTEMPTS = 5
REPLACE_RETRY_SECONDS = 0.01


@dataclass(frozen=True, slots=True)
class Heartbeat:
    role: str
    instance_id: str
    beat_monotonic: float
    beat_wall: datetime
    sequence: int
    control_epoch: int | None
    ready: bool


class HeartbeatFile:
    """单角色心跳写入器；文件内容为一行 JSON，用临时文件 + 原子替换更新."""

    def __init__(
        self,
        path: Path | str,
        *,
        role: str,
        instance_id: str | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        require_text(role, "role")
        self.path = Path(path)
        self.role = role
        self.instance_id = instance_id or uuid4().hex
        self._monotonic = monotonic
        self._wall_time = wall_time
        self.sequence = 0

    def beat(self, *, control_epoch: int | None = None, ready: bool = False) -> Heartbeat:
        self.sequence += 1
        record = Heartbeat(
            role=self.role,
            instance_id=self.instance_id,
            beat_monotonic=self._monotonic(),
            beat_wall=self._wall_time(),
            sequence=self.sequence,
            control_epoch=control_epoch,
            ready=ready,
        )
        payload = json.dumps(
            {
                "role": record.role,
                "instance_id": record.instance_id,
                "beat_monotonic": record.beat_monotonic,
                "beat_wall": record.beat_wall.isoformat(),
                "sequence": record.sequence,
                "control_epoch": record.control_epoch,
                "ready": record.ready,
            },
            ensure_ascii=False,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + f".{uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            for attempt in range(REPLACE_ATTEMPTS):
                try:
                    os.replace(temporary, self.path)
                    break
                except PermissionError:
                    # Windows：读方 (看门狗) 正打开目标文件时替换会被拒绝，短暂重试
                    if attempt == REPLACE_ATTEMPTS - 1:
                        raise
                    time.sleep(REPLACE_RETRY_SECONDS)
        finally:
            temporary.unlink(missing_ok=True)
        return record


def read_heartbeat(path: Path | str) -> Heartbeat | None:
    """读取心跳；文件缺失返回 None，内容损坏明确报错 (不把损坏当作存活)."""
    file = Path(path)
    if not file.exists():
        return None
    data = json.loads(file.read_text(encoding="utf-8"))
    return Heartbeat(
        role=str(data["role"]),
        instance_id=str(data["instance_id"]),
        beat_monotonic=float(data["beat_monotonic"]),
        beat_wall=datetime.fromisoformat(str(data["beat_wall"])),
        sequence=int(data["sequence"]),
        control_epoch=None if data.get("control_epoch") is None else int(data["control_epoch"]),
        ready=bool(data.get("ready", False)),
    )
