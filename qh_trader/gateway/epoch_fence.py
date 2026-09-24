"""[Gateway 适配器] 网关最终调用处的控制代次复核 (S5-04, ADR-X1, FR-RISK-07, A23).

执行服务在发送前已复核代次；本封装把同一复核压到实际网关调用的最后一刻：
传入的代次与当前持久化控制记录不一致时返回明确 ``NOT_SENT``，不调用底层网关。
真实 CTP 适配器 (S5-01) 在 ``ReqOrderInsert`` / ``ReqOrderAction`` 前必须做同样的检查。
"""

from __future__ import annotations

from collections.abc import Callable

from qh_trader.core.constants import SendState
from qh_trader.core.objects import (
    CapabilityProfile,
    ControlEpoch,
    LocalSendResult,
    OrderIdentity,
    OrderIntent,
    VersionedValue,
)
from qh_trader.core.ports import ExecutionPort

FENCE_CODE = -9


class EpochFencedGateway:
    """``ExecutionPort`` 装饰器：``authority`` 返回当前持久化控制代次 (无控制记录时返回 None)."""

    def __init__(self, inner: ExecutionPort, authority: Callable[[], ControlEpoch | None]) -> None:
        if not isinstance(inner, ExecutionPort):
            raise TypeError("fenced gateway requires an ExecutionPort")
        self._inner = inner
        self._authority = authority
        self.fenced_calls = 0

    @property
    def inner(self) -> ExecutionPort:
        return self._inner

    def _fence(self, epoch: ControlEpoch) -> LocalSendResult | None:
        if not isinstance(epoch, ControlEpoch):
            raise TypeError("gateway calls require a ControlEpoch")
        current = self._authority()
        if current is None or current != epoch:
            self.fenced_calls += 1
            return LocalSendResult(SendState.NOT_SENT, FENCE_CODE, "control epoch fence at gateway call")
        return None

    def submit(self, order: OrderIntent, epoch: ControlEpoch) -> LocalSendResult:
        fenced = self._fence(epoch)
        return fenced if fenced is not None else self._inner.submit(order, epoch)

    def cancel(self, ref: OrderIdentity, epoch: ControlEpoch) -> LocalSendResult:
        fenced = self._fence(epoch)
        return fenced if fenced is not None else self._inner.cancel(ref, epoch)

    def capabilities(self) -> VersionedValue[CapabilityProfile]:
        return self._inner.capabilities()

    def __getattr__(self, name: str):
        # 透传底层网关的非 ExecutionPort 能力 (如模拟网关的 drain_events / 撮合入口)
        return getattr(self._inner, name)
