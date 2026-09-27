"""[Monitor 适配器] 人工控制：暂停、只减仓、恢复、撤单、平仓与接管申请的规范命令 (S5-06, FR-RISK-09, A23).

人工入口没有持久状态，也不直接登录交易接口 (04 §5)：它只构造规范化的 ``ExecutionCommand``，交给命令表，
由唯一执行服务在同一账户执行序列里按控制者与代次校验、经同一风控与记账路径执行并留审计。

- 影响交易状态的命令必须以**当前控制者**身份、携带当前代次发出；操作者不是当前控制者时在本地就拒绝
  (``ControlRefusedError``)，须先申请接管并由新的执行服务实例受理 (08 §7.2 / §7.3)。本地拒绝只是提前告知，
  执行服务收到命令时仍会再校验一次。
- 恢复交易必须显式声明"异常原因已消除"与"账户一致"(FR-RISK-05)，缺一不可。
- 平仓只接受明确的平今 / 平昨与限价 (最小变动单位整数倍)，不猜测今昨仓桶，也不提供市价单。
- 接管申请携带观测到的当前代次，只是申请，不授予交易权 (FR-RISK-08)。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from uuid import uuid4

from qh_trader.core.constants import Offset, OrderType, Side
from qh_trader.core.execution import CommandKind, ExecutionCommand, TakeoverRequest
from qh_trader.core.objects import (
    ControlEpoch,
    ControlRecord,
    InstrumentId,
    OrderIdentity,
    OrderIntent,
    require_int,
    require_text,
)

OPERATOR_STRATEGY_ID = "operator"
CLOSE_OFFSETS = frozenset({Offset.CLOSE_TODAY, Offset.CLOSE_YESTERDAY})


class ControlRefusedError(ValueError):
    """本地即可判定执行服务必然拒绝的人工命令 (非当前控制者、缺少原因或条件)."""


def _stamp(at: datetime) -> str:
    return at.strftime("%Y%m%dT%H%M%S%fZ")


def command_id(kind: CommandKind, operator_id: str, at: datetime) -> str:
    return f"control-{kind.value.lower()}-{operator_id}-{_stamp(at)}-{uuid4().hex[:8]}"


def acting_control(current: ControlRecord | None, operator_id: str) -> ControlEpoch:
    """操作者作为当前控制者时使用的代次；不是当前控制者即拒绝，不借用他人的代次."""
    require_text(operator_id, "operator_id")
    if current is None:
        raise ControlRefusedError("no control record exists yet; the execution service has not taken control")
    if current.epoch.controller_id != operator_id:
        raise ControlRefusedError(
            f"{operator_id} is not the current controller ({current.epoch.controller_id}, epoch "
            f"{current.epoch.epoch}); apply for takeover and let a new execution service instance accept it first"
        )
    return current.epoch


def _reason(reason: str) -> str:
    text = reason.strip() if isinstance(reason, str) else ""
    if not text:
        raise ControlRefusedError("operator commands must state a reason")
    return text


def _command(
    kind: CommandKind,
    *,
    account_id: str,
    operator_id: str,
    control: ControlEpoch,
    payload: OrderIntent | OrderIdentity | TakeoverRequest | Mapping[str, object],
    at: datetime,
) -> ExecutionCommand:
    return ExecutionCommand(
        command_id=command_id(kind, operator_id, at),
        account_id=account_id,
        producer_id=f"control:{operator_id}",
        control=control,
        kind=kind,
        submitted_at=at,
        payload=payload,
    )


def pause(
    *, account_id: str, operator_id: str, current: ControlRecord | None, reason: str, at: datetime
) -> ExecutionCommand:
    """暂停：风控进入 HALTED，阻断一切新委托；撤单仍可执行 (A09)."""
    control = acting_control(current, operator_id)
    return _command(
        CommandKind.PAUSE,
        account_id=account_id,
        operator_id=operator_id,
        control=control,
        payload={"reason": _reason(reason)},
        at=at,
    )


def reduce_only(
    *, account_id: str, operator_id: str, current: ControlRecord | None, reason: str, at: datetime
) -> ExecutionCommand:
    """只减仓：禁止开仓，允许平仓与撤单."""
    control = acting_control(current, operator_id)
    return _command(
        CommandKind.REDUCE_ONLY,
        account_id=account_id,
        operator_id=operator_id,
        control=control,
        payload={"reason": _reason(reason)},
        at=at,
    )


def resume(
    *,
    account_id: str,
    operator_id: str,
    current: ControlRecord | None,
    reason: str,
    cause_cleared: bool,
    account_consistent: bool,
    at: datetime,
) -> ExecutionCommand:
    """恢复：两项条件都须操作者显式确认，缺一即拒绝 (FR-RISK-05)."""
    control = acting_control(current, operator_id)
    if cause_cleared is not True or account_consistent is not True:
        raise ControlRefusedError(
            "resume requires explicit confirmation that the cause is cleared and the account is consistent"
        )
    return _command(
        CommandKind.RESUME,
        account_id=account_id,
        operator_id=operator_id,
        control=control,
        payload={"reason": _reason(reason), "cause_cleared": True, "account_consistent": True},
        at=at,
    )


def cancel(
    *, account_id: str, operator_id: str, current: ControlRecord | None, identity: OrderIdentity, at: datetime
) -> ExecutionCommand:
    """撤单：定位标识 (原会话三元组 / 交易所单号) 由入口脚本从账户已持久化的委托标识读出，不在此拼接或猜测."""
    control = acting_control(current, operator_id)
    if not identity.client_order_id:
        raise ControlRefusedError("cancel needs the local client_order_id of the order")
    if identity.exchange_order_id is None and None in (identity.front_id, identity.session_id, identity.order_ref):
        raise ControlRefusedError(
            f"order {identity.client_order_id} has no persisted session triple or exchange order id to cancel by"
        )
    return _command(
        CommandKind.CANCEL, account_id=account_id, operator_id=operator_id, control=control, payload=identity, at=at
    )


def close_position(
    *,
    account_id: str,
    operator_id: str,
    current: ControlRecord | None,
    instrument: InstrumentId,
    side: Side,
    offset: Offset,
    quantity: int,
    limit_price_ticks: int,
    at: datetime,
) -> ExecutionCommand:
    """人工平仓：一笔明确今昨仓桶的限价子单，经同一风控与预占路径执行."""
    control = acting_control(current, operator_id)
    if offset not in CLOSE_OFFSETS:
        raise ControlRefusedError("manual close needs an explicit CLOSE_TODAY or CLOSE_YESTERDAY bucket")
    require_int(quantity, "close quantity", 1)
    require_int(limit_price_ticks, "limit price ticks", 1)
    identifier = f"operator-{operator_id}-{_stamp(at)}-{uuid4().hex[:8]}"
    intent = OrderIntent(
        client_order_id=identifier,
        account_id=account_id,
        strategy_id=OPERATOR_STRATEGY_ID,
        instrument=instrument,
        side=side,
        offset=offset,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        created_at=at,
        limit_price_ticks=limit_price_ticks,
    )
    return _command(
        CommandKind.SUBMIT, account_id=account_id, operator_id=operator_id, control=control, payload=intent, at=at
    )


def takeover_request(
    *,
    account_id: str,
    applicant_id: str,
    current: ControlRecord | None,
    reason: str,
    at: datetime,
    producer_id: str | None = None,
    identifier: str | None = None,
) -> ExecutionCommand:
    """接管申请：携带观测到的当前代次 (尚无控制记录时为 0)；申请本身不授予交易权 (FR-RISK-08)."""
    require_text(applicant_id, "applicant_id")
    observed = ControlEpoch(applicant_id, 0) if current is None else current.epoch
    if current is not None and current.epoch.controller_id == applicant_id:
        raise ControlRefusedError(f"{applicant_id} already holds control (epoch {current.epoch.epoch})")
    return ExecutionCommand(
        command_id=identifier or command_id(CommandKind.TAKEOVER_REQUEST, applicant_id, at),
        account_id=account_id,
        producer_id=producer_id or f"control:{applicant_id}",
        control=observed,
        kind=CommandKind.TAKEOVER_REQUEST,
        submitted_at=at,
        payload=TakeoverRequest(controller_id=applicant_id, reason=_reason(reason)),
    )
