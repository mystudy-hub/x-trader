"""[Engine 层] Durable account sequence, command fencing and callback ingress.

S5-04's local coordinator. Assembly must inject the staged account model, gateway,
recovery coordinator and verified connection isolation. No default live gateway,
account model, automatic takeover or implicit recovery approval is provided.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from queue import Empty, Full, Queue
from uuid import uuid4

from qh_trader.core.constants import EventKind, JournalConflictError, SendState
from qh_trader.core.event import CanonicalEvent, JournalTransaction
from qh_trader.core.execution import (
    CommandKind,
    CommandStatus,
    ExecutionNotReadyError,
    ExecutionOwnershipError,
    QueuedCommand,
    TakeoverRequest,
)
from qh_trader.core.objects import (
    ControlEpoch,
    ControlRecord,
    LocalSendResult,
    OrderIdentity,
    OrderIntent,
    Trade,
    require_int,
)
from qh_trader.core.ports import (
    ExecutionIsolationPort,
    ExecutionModelPort,
    ExecutionPort,
    ExecutionStorePort,
)
from qh_trader.domain.recovery import RecoveryCoordinator, RecoveryPhase

LOGGER = logging.getLogger(__name__)
SERVICE_STATE = "execution_service"
DISPATCH_KINDS = frozenset({CommandKind.SUBMIT, CommandKind.CANCEL})
SUPPORTED_KINDS = DISPATCH_KINDS | {CommandKind.PAUSE, CommandKind.RESUME, CommandKind.REDUCE_ONLY}


class ExecutionService:
    """All account mutations and gateway calls run on the store's owner thread.

    CTP threads only enqueue. A bounded trade batch gives commands a turn even
    during sustained callbacks; market events never delay the next trade batch.
    Timeouts use a monotonic clock. Wall time is only used for audit timestamps.
    """

    def __init__(
        self,
        *,
        store: ExecutionStorePort,
        model: ExecutionModelPort,
        gateway: ExecutionPort,
        recovery: RecoveryCoordinator,
        poll_interval: float = 0.1,
        trade_batch_size: int = 32,
        market_capacity: int = 1024,
        trade_high_water: int = 10000,
        wall_time: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0.05 <= poll_interval <= 0.2:
            raise ValueError("command poll interval must be between 50 and 200 ms")
        for name, value in (
            ("trade_batch_size", trade_batch_size),
            ("market_capacity", market_capacity),
            ("trade_high_water", trade_high_water),
        ):
            require_int(value, name, 1)
        store.assert_owner()
        self.store = store
        self.model = model
        self.gateway = gateway
        self.recovery = recovery
        self.poll_interval = poll_interval
        self.trade_batch_size = trade_batch_size
        self._wall_time = wall_time
        self._monotonic = monotonic
        self._trade_queue: Queue[CanonicalEvent] = Queue()
        self._market_queue: Queue[CanonicalEvent] = Queue(maxsize=market_capacity)
        self._high_water = trade_high_water
        self._metrics_lock = threading.Lock()
        self._counts = {
            "trade_high_water": 0,
            "market_dropped": 0,
            "callback_failures": 0,
            "duplicate_facts": 0,
            "storage_or_model_failures": 0,
        }
        self._above_high_water = False
        self._reported_high_water = 0
        self._reported_market_drops = 0
        self._acknowledged_callback_failures = 0
        self._pending_fact: CanonicalEvent | None = None
        self._ready = False
        self._fault: str | None = None
        self._processing = False
        current = store.control()
        self._authority = None if current is None else current.epoch
        self.recovery.on_disconnected("execution service startup requires reconciliation")
        self.model.publish(store.checkpoint())
        # The process may have died on either side of the network call. No replay
        # of DISPATCHING is allowed, even if the call probably never happened.
        for interrupted in store.interrupted():
            self._record_send_result(
                interrupted,
                LocalSendResult(SendState.SENT_UNKNOWN, None, "process stopped before local send result was committed"),
            )

    @property
    def ready(self) -> bool:
        self.store.assert_owner()
        current = self.store.control()
        with self._metrics_lock:
            ingress_healthy = (
                self._counts["market_dropped"] <= self._reported_market_drops
                and self._counts["callback_failures"] <= self._acknowledged_callback_failures
            )
        return (
            self._ready and self._fault is None and current is not None
            and self.recovery.phase == RecoveryPhase.READY
            and current.epoch == self._authority
            and ingress_healthy
        )

    @property
    def metrics(self) -> Mapping[str, int]:
        with self._metrics_lock:
            return dict(self._counts) | {
                "trade_queue_depth": self._trade_queue.qsize(),
                "market_queue_depth": self._market_queue.qsize(),
            }

    @property
    def pending_fact(self) -> CanonicalEvent | None:
        return self._pending_fact

    def enqueue(self, event: CanonicalEvent) -> bool:
        """Callback-safe: no model mutation, database write or gateway call."""
        if not isinstance(event, CanonicalEvent):
            raise TypeError("callbacks must enqueue normalized canonical events")
        if event.kind == EventKind.MARKET_DATA:
            try:
                self._market_queue.put_nowait(event)
            except Full:
                with self._metrics_lock:
                    self._counts["market_dropped"] += 1
                return False
        else:
            with self._metrics_lock:
                # An ingress fault must close the final send fence even if the
                # callback arrives between the preparation commit and API call.
                if self._callback_failed(event):
                    self._counts["callback_failures"] += 1
                self._trade_queue.put_nowait(event)
                above = self._trade_queue.qsize() >= self._high_water
                if above and not self._above_high_water:
                    self._counts["trade_high_water"] += 1
                self._above_high_water = above
        return True

    @staticmethod
    def _callback_failed(event: CanonicalEvent) -> bool:
        return (
            event.kind == EventKind.CONTROL and isinstance(event.payload, Mapping)
            and event.payload.get("callback_failure") is True
        )

    def enqueue_callback_error(self, source_id: str, error: Exception) -> None:
        """Keep a sanitized dead letter; raw callbacks and exception text stay out."""
        now = self._wall_time()
        self.enqueue(
            CanonicalEvent(
                event_id="callback-error:" + uuid4().hex,
                kind=EventKind.CONTROL,
                event_time=now,
                available_at=now,
                sequence=0,
                source_id=source_id,
                payload={"callback_failure": True, "error_type": type(error).__name__},
            )
        )

    def _fail(self, reason: str) -> None:
        self._fault = reason
        self._ready = False
        self.recovery.on_disconnected(reason)
        with self._metrics_lock:
            self._counts["storage_or_model_failures"] += 1
        LOGGER.error("execution service blocked: %s", reason)

    def _check_running(self) -> None:
        self.store.assert_owner()
        if self._processing:
            raise ExecutionOwnershipError("account execution cannot be reentered during a staged change")
        if self._fault is not None:
            raise ExecutionNotReadyError(self._fault)

    @contextmanager
    def _operation(self, *, repair: bool = False) -> Iterator[None]:
        self.store.assert_owner()
        if self._processing:
            raise ExecutionOwnershipError("account execution cannot be reentered during a staged change")
        if not repair:
            self._check_running()
        self._processing = True
        try:
            yield
        finally:
            self._processing = False

    def _audit(self, payload: Mapping[str, object]) -> CanonicalEvent:
        now = self._wall_time()
        return CanonicalEvent(
            event_id="execution:" + uuid4().hex,
            kind=EventKind.CONTROL,
            event_time=now,
            available_at=now,
            sequence=0,
            source_id="execution-service",
            payload=payload,
        )

    def _commit(
        self,
        event: CanonicalEvent,
        updates: Mapping[str, object],
        *,
        command: QueuedCommand | None = None,
        status: CommandStatus | None = None,
        new_control: ControlEpoch | None = None,
        phase: str | None = None,
    ) -> None:
        if SERVICE_STATE in updates:
            raise ValueError("account models cannot overwrite execution service state")
        checkpoint = self.store.checkpoint()
        expected = None if checkpoint.control_record is None else checkpoint.control_record.epoch
        event = replace(event, sequence=self.store.next_ingress_sequence())
        state = dict(updates)
        if phase is not None:
            state[SERVICE_STATE] = {
                "phase": phase,
                "control": new_control or expected,
                "poll_interval": self.poll_interval,
                "trade_batch_size": self.trade_batch_size,
            }
        control_record = (
            None if new_control is None
            else ControlRecord(new_control, event.available_at, checkpoint.journal_seq + 1)
        )
        keys = (event.payload.deduplication_key,) if isinstance(event.payload, Trade) else ()
        transaction = JournalTransaction(
            transaction_id="execution-tx:" + uuid4().hex,
            events=(event,),
            cursor_before=checkpoint.cursor,
            cursor_after=checkpoint.cursor + 1,
            state_updates=state,
            deduplication_keys=keys,
            control_record=control_record,
        )
        self.store.commit(transaction, expected_control=expected, command=command, status=status)
        self.model.publish(self.store.checkpoint())

    def take_over(self, command_id: str, isolation: ExecutionIsolationPort) -> ControlEpoch:
        """Explicit startup arbitration: isolate -> advance -> reconcile -> enable.

        Merely polling a TAKEOVER_REQUEST never calls this method. Isolation runs
        outside SQLite's write transaction. The new epoch commits before any query
        window, so commands from the former controller are already fenced.
        """
        with self._operation():
            return self._take_over(command_id, isolation)

    def _take_over(self, command_id: str, isolation: ExecutionIsolationPort) -> ControlEpoch:
        if self._ready:
            raise ExecutionNotReadyError("takeover arbitration belongs to a stopped or starting execution instance")
        queued = self.store.get(command_id)
        if queued is None or queued.command.kind != CommandKind.TAKEOVER_REQUEST:
            raise ValueError("takeover requires a queued takeover application")
        if queued.status != CommandStatus.PENDING:
            raise JournalConflictError("takeover application has already been processed")
        request = queued.command
        assert isinstance(request.payload, TakeoverRequest)
        previous = self.store.control()
        observed = None if previous is None else previous.epoch
        if (previous is None and request.control.epoch != 0) or (
            previous is not None and request.control != observed
        ):
            self._reject(queued, CommandStatus.REJECTED_STALE, "takeover observation no longer matches current control")
            raise ExecutionNotReadyError("obsolete takeover application must be reviewed again")
        if isolation.isolate(previous, request) is not True:
            self._reject(queued, CommandStatus.REJECTED, "former trading connection could not be isolated")
            raise ExecutionNotReadyError("former trading connection has not been isolated")
        # An isolation adapter must not modify control behind the executor's back.
        if self.store.control() != previous:
            raise JournalConflictError("control changed during connection isolation")
        new_control = ControlEpoch(request.payload.controller_id, 1 if previous is None else previous.epoch.epoch + 1)
        try:
            self._commit(
                self._audit({
                    "action": "takeover",
                    "command_id": request.command_id,
                    "previous_control": observed,
                    "new_control": new_control,
                    "isolation_confirmed": True,
                    "reason": request.payload.reason,
                }),
                {},
                command=queued,
                status=CommandStatus.COMPLETED,
                new_control=new_control,
                phase="RECONCILING",
            )
        except Exception:
            self._fail("takeover_commit_failed")
            raise
        self._authority = new_control
        self._ready = False
        self.recovery.on_disconnected("new control requires fresh account queries")
        return new_control

    def enable_after_reconciliation(self) -> None:
        with self._operation():
            self._enable_after_reconciliation()

    def _enable_after_reconciliation(self) -> None:
        self._check_ingress_health()
        if self._pending_fact is not None or not self._trade_queue.empty():
            raise ExecutionNotReadyError("pending callbacks must be processed before enabling trading")
        current = self.store.control()
        if current is None or current.epoch != self._authority:
            raise ExecutionNotReadyError("this instance does not hold current control")
        report = self.recovery.report
        funds = report.watermarks.get("funds")
        if (
            self.recovery.phase != RecoveryPhase.RECONCILING
            or self.recovery.expected_trading_day is None
            or not self.recovery.can_enter_ready()
            or funds is None or not funds.complete or funds.error_code is not None or funds.record_count != 1
            or any(diff.category == "funds" and not diff.resolved for diff in report.diffs)
        ):
            raise ExecutionNotReadyError("complete orders, trades, positions and funds reconciliation is required")
        try:
            self._commit(self._audit({"action": "reconciliation_ready", "control": current.epoch}), {}, phase="READY")
        except Exception:
            self._fail("readiness_commit_failed")
            raise
        if not self.recovery.try_enter_ready():
            self._fail("reconciliation_changed_before_enable")
            raise ExecutionNotReadyError("reconciliation changed before trading could be enabled")
        self._ready = True

    def _reject(self, command: QueuedCommand, status: CommandStatus, reason: str) -> None:
        self._commit(
            self._audit({"action": "command_rejected", "command": command.command, "reason": reason}),
            {}, command=command, status=status,
        )

    def process_next_command(self) -> bool:
        with self._operation():
            return self._process_next_command()

    def _process_next_command(self) -> bool:
        self._check_ingress_health()
        queued = self.store.next_pending()
        if queued is None:
            return False
        try:
            self._process_command(queued)
        except Exception:
            self._fail("command_processing_failed")
            raise
        return True

    def _process_command(self, queued: QueuedCommand) -> None:
        command = queued.command
        current = self.store.control()
        if current is None or command.control != current.epoch:
            self._reject(queued, CommandStatus.REJECTED_STALE, "controller or control epoch mismatch")
            return
        if not self.ready:
            self._reject(queued, CommandStatus.REJECTED, "account execution is not reconciled and ready")
            return
        if command.kind not in SUPPORTED_KINDS:
            self._reject(queued, CommandStatus.REJECTED, "command needs separately tracked child execution requests")
            return
        plan = self.model.stage_command(command)
        if not plan.approved:
            self._reject(queued, CommandStatus.REJECTED, plan.reason)
            return
        dispatch = command.kind in DISPATCH_KINDS
        self._commit(
            self._audit({"action": "command_prepared", "command": command, "reason": plan.reason}),
            plan.state_updates,
            command=queued,
            status=CommandStatus.DISPATCHING if dispatch else CommandStatus.COMPLETED,
        )
        if not dispatch:
            return
        prepared = self.store.get(command.command_id)
        assert prepared is not None
        # Last check is adjacent to the actual gateway call on the same owner
        # thread. The concrete CTP adapter must also fence immediately at its API.
        current = self.store.control()
        if not self.ready or current is None or current.epoch != command.control:
            result = LocalSendResult(SendState.NOT_SENT, None, "control or readiness changed at final send fence")
        else:
            try:
                if command.kind == CommandKind.SUBMIT:
                    assert isinstance(command.payload, OrderIntent)
                    result = self.gateway.submit(command.payload, command.control)
                else:
                    assert isinstance(command.payload, OrderIdentity)
                    result = self.gateway.cancel(command.payload, command.control)
                if not isinstance(result, LocalSendResult):
                    raise TypeError("gateway returned an invalid local send result")
            except Exception as exc:
                # Exceptions cannot prove that the broker did not receive a call.
                # Do not log exception text, which can contain credentials.
                result = LocalSendResult(SendState.SENT_UNKNOWN, None, "gateway exception: " + type(exc).__name__)
        self._record_send_result(prepared, result)

    def _record_send_result(self, queued: QueuedCommand, result: LocalSendResult) -> None:
        updates = self.model.stage_send_result(queued.command, result)
        self._commit(
            self._audit({"action": "local_send_result", "command_id": queued.command.command_id, "result": result}),
            updates,
            command=queued,
            status=CommandStatus.NOT_SENT if result.state == SendState.NOT_SENT else CommandStatus.SENT_UNKNOWN,
        )

    def _process_fact(self, event: CanonicalEvent) -> None:
        with self._operation():
            self._apply_fact(event)

    def _apply_fact(self, event: CanonicalEvent) -> None:
        self._pending_fact = event
        callback_failed = self._callback_failed(event)
        try:
            existing = self.store.event(event.event_id)
            if existing is not None:
                if replace(existing, sequence=event.sequence) != event:
                    raise JournalConflictError("callback event_id was reused with different contents")
                # The preceding commit may have succeeded while publish raised.
                # Rehydrate the committed model before declaring this retry done.
                self.model.publish(self.store.checkpoint())
                with self._metrics_lock:
                    self._counts["duplicate_facts"] += 1
            elif isinstance(event.payload, Trade) and self.store.contains_trade(event.payload.deduplication_key):
                # Deduplication belongs to the account sequence, never callbacks.
                self._commit(
                    self._audit({
                        "action": "duplicate_trade", "source_event_id": event.event_id,
                        "trade_key": event.payload.deduplication_key,
                    }), {},
                )
                with self._metrics_lock:
                    self._counts["duplicate_facts"] += 1
            else:
                updates = {} if callback_failed else self.model.stage_fact(event)
                self._commit(event, updates, phase="RECONCILING" if callback_failed else None)
            if callback_failed:
                self._ready = False
                self.recovery.on_disconnected("callback conversion failed; broker query reconciliation required")
                with self._metrics_lock:
                    self._acknowledged_callback_failures += 1
                LOGGER.error("callback conversion failed; account reconciliation required")
            self._pending_fact = None
        except Exception:
            self._fail("fact_persistence_or_projection_failed")
            raise

    def retry_pending_fact(self) -> None:
        """Explicit repair retry; success still requires fresh reconciliation."""
        with self._operation(repair=True):
            if self._pending_fact is None:
                raise ExecutionNotReadyError("there is no retained callback to retry")
            self._apply_fact(self._pending_fact)
            self._fault = None
            self._ready = False

    def _check_ingress_health(self) -> None:
        """Report queue health on the consumer, never inside a CTP callback."""
        with self._metrics_lock:
            high_water = self._counts["trade_high_water"]
            market_drops = self._counts["market_dropped"]
        if high_water > self._reported_high_water:
            LOGGER.warning("trade callback queue exceeded its high-water mark")
            self._reported_high_water = high_water
        if market_drops > self._reported_market_drops:
            # No verified loss tolerance is enabled in this local foundation.
            # Persist the gap before any command can use an incomplete quote view.
            self._ready = False
            self.recovery.on_disconnected("market callback queue lost data")
            try:
                self._commit(
                    self._audit({"action": "market_data_loss", "dropped": market_drops}),
                    {}, phase="RECONCILING",
                )
            except Exception:
                self._fail("market_data_loss_audit_failed")
                raise
            self._reported_market_drops = market_drops
            LOGGER.error("market callback queue lost data; account reconciliation required")

    def run_once(self, *, wait: bool = False) -> int:
        self._check_running()
        with self._operation():
            self._check_ingress_health()
        processed = 0
        deadline = self._monotonic() + self.poll_interval
        for index in range(self.trade_batch_size):
            try:
                timeout = max(0.0, deadline - self._monotonic()) if wait and index == 0 else 0.0
                event = self._trade_queue.get(timeout=timeout)
            except Empty:
                break
            self._process_fact(event)
            processed += 1
            if self._monotonic() >= deadline:
                break
        with self._metrics_lock:
            self._above_high_water = self._trade_queue.qsize() >= self._high_water
        # Poll after every bounded batch, not only when get() times out.
        processed += int(self.process_next_command())
        if self._trade_queue.empty():
            try:
                market = self._market_queue.get_nowait()
            except Empty:
                pass
            else:
                self._process_fact(market)
                processed += 1
        return processed

    def run(self, stop: threading.Event) -> None:
        self._check_running()
        while not stop.is_set():
            self.run_once(wait=True)
