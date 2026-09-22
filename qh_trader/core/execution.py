"""[Core 层] Durable execution commands and staged account changes (S5-04)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .objects import (
    ControlEpoch,
    OrderIdentity,
    OrderIntent,
    freeze_payload,
    normalize_times,
    require_bool,
    require_enum,
    require_int,
    require_text,
)


class CommandKind(StrEnum):
    SUBMIT = "SUBMIT"
    CANCEL = "CANCEL"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    REDUCE_ONLY = "REDUCE_ONLY"
    FLATTEN = "FLATTEN"
    AMEND = "AMEND"
    TAKEOVER_REQUEST = "TAKEOVER_REQUEST"


class CommandStatus(StrEnum):
    PENDING = "PENDING"
    DISPATCHING = "DISPATCHING"
    COMPLETED = "COMPLETED"
    NOT_SENT = "NOT_SENT"
    SENT_UNKNOWN = "SENT_UNKNOWN"
    REJECTED = "REJECTED"
    REJECTED_STALE = "REJECTED_STALE"


@dataclass(frozen=True, slots=True, kw_only=True)
class TakeoverRequest:
    """An application for control, never evidence that the applicant owns it."""

    controller_id: str
    reason: str

    def __post_init__(self) -> None:
        require_text(self.controller_id, "requested controller_id")
        require_text(self.reason, "takeover reason")


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionCommand:
    """SUBMIT carries a final child order; routing belongs to the account model.

    For TAKEOVER_REQUEST, control is the *observed* epoch. It does not authorize
    trading. Control commands carry normalized parameters, never gateway objects.
    """

    command_id: str
    account_id: str
    producer_id: str
    control: ControlEpoch
    kind: CommandKind
    submitted_at: datetime
    payload: OrderIntent | OrderIdentity | TakeoverRequest | Mapping[str, object]

    def __post_init__(self) -> None:
        for name in ("command_id", "account_id", "producer_id"):
            require_text(getattr(self, name), name)
        if not isinstance(self.control, ControlEpoch):
            raise TypeError("execution command requires a controller and epoch")
        require_enum(self.kind, CommandKind)
        normalize_times(self, "submitted_at")
        expected = {
            CommandKind.SUBMIT: OrderIntent,
            CommandKind.CANCEL: OrderIdentity,
            CommandKind.TAKEOVER_REQUEST: TakeoverRequest,
        }.get(self.kind, Mapping)
        if not isinstance(self.payload, expected):
            raise TypeError("command kind does not match its normalized payload")
        if isinstance(self.payload, (OrderIntent, OrderIdentity)) and self.payload.account_id != self.account_id:
            raise ValueError("command payload belongs to another account")
        object.__setattr__(self, "payload", freeze_payload(self.payload))


@dataclass(frozen=True, slots=True)
class QueuedCommand:
    sequence: int
    command: ExecutionCommand
    status: CommandStatus
    processed_seq: int | None = None

    def __post_init__(self) -> None:
        require_int(self.sequence, "command sequence", 1)
        if not isinstance(self.command, ExecutionCommand):
            raise TypeError("queued command must be normalized")
        require_enum(self.status, CommandStatus)
        if self.processed_seq is not None:
            require_int(self.processed_seq, "processed journal sequence", 1)
        if (self.status == CommandStatus.PENDING) != (self.processed_seq is None):
            raise ValueError("processed commands must reference their journal transaction")


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandPlan:
    """Prepared risk/reservation changes, invisible until the durable commit.

    Approval is explicit. SUBMIT/CANCEL dispatch their original normalized payload
    only. Other kinds change account state without calling a gateway; a model must
    reject unsupported controls, including an AMEND it cannot safely implement.
    """

    approved: bool
    reason: str
    state_updates: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_bool(self.approved, "approved")
        require_text(self.reason, "command decision reason")
        if not isinstance(self.state_updates, Mapping):
            raise TypeError("staged account changes must be a named mapping")
        if not self.approved and self.state_updates:
            raise ValueError("a rejected command cannot reserve funds or change account state")
        object.__setattr__(self, "state_updates", freeze_payload(self.state_updates))


class ExecutionOwnershipError(RuntimeError):
    """The account execution sequence is already owned, closed or on another thread."""


class ExecutionNotReadyError(RuntimeError):
    """Isolation, reconciliation or durable storage has not established readiness."""
