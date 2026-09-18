"""[Infrastructure 层] 纯内存 JournalPort 适配器 (S2-11, FR-REC-01).

用于回测与极速单元测试环境，与 SQLiteJournal 遵守同一份 JournalPort 契约：原子事务提交、
事实去重、游标/事件序/控制纪元守卫、按历史重建的不可变快照与已物化的回放，
回测结束后可通过 ``export_events`` 完整导出规范事件序列。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import fields, is_dataclass
from typing import Any

from qh_trader.core.constants import DuplicateFactError, JournalConflictError, JournalCorruptionError
from qh_trader.core.event import CanonicalEvent, JournalSnapshot, JournalTransaction
from qh_trader.core.objects import ControlRecord, Trade, TradeKey, freeze_payload, require_int, require_text
from qh_trader.infrastructure import journal_codec as codec

_RESERVED_STATE_KEYS = frozenset({"journal_seq", "head_seq", "cursor", "control_record", "control_epoch", "account_id"})


class MemoryJournal:
    """纯内存 JournalPort 适配器实现；语义与 SQLiteJournal 保持一致，仅去掉物理持久化."""

    def __init__(self, account_id: str) -> None:
        require_text(account_id, "account_id")
        self.account_id = account_id
        self._transactions: list[JournalTransaction] = []
        self._payloads: dict[str, tuple[int, str]] = {}  # transaction_id -> (journal_seq, canonical payload)
        self._events: list[CanonicalEvent] = []
        self._event_ids: set[str] = set()
        self._last_ingress_seq: int = -1
        self._trade_keys: set[TradeKey] = set()
        self._cursor: int = 0
        self._control_record: ControlRecord | None = None
        self._state: dict[str, object] = {}
        self._snapshots: dict[int, JournalSnapshot] = {}

    @property
    def head_seq(self) -> int:
        return len(self._transactions)

    @property
    def cursor(self) -> int:
        return self._cursor

    def _check_account(self, value: Any) -> None:
        if is_dataclass(value) and not isinstance(value, type):
            if hasattr(value, "account_id") and value.account_id != self.account_id:
                raise JournalConflictError("cross-account values cannot enter this journal")
            for member in fields(value):
                self._check_account(getattr(value, member.name))
        elif isinstance(value, Mapping):
            if "account_id" in value and value["account_id"] != self.account_id:
                raise JournalConflictError("cross-account state cannot enter this journal")
            for item in value.values():
                self._check_account(item)
        elif isinstance(value, (tuple, list, set, frozenset)):
            for item in value:
                self._check_account(item)

    def contains_trade(self, key: TradeKey) -> bool:
        if not isinstance(key, TradeKey):
            raise TypeError("trade lookup requires a scoped TradeKey")
        self._check_account(key)
        return key in self._trade_keys

    def append(self, transaction: JournalTransaction) -> int:
        """原子提交事务 (FR-DATA-07, FR-REC-01)；任何守卫失败都不发布部分状态."""
        if not isinstance(transaction, JournalTransaction):
            raise TypeError("append requires a normalized JournalTransaction")
        self._check_account(transaction)
        payload = codec.dumps(transaction)
        keys = transaction.deduplication_keys
        if set(transaction.state_updates) & _RESERVED_STATE_KEYS:
            raise JournalConflictError("reserved journal metadata must use transaction fields, not state projections")
        if len(set(keys)) != len(keys):
            raise JournalConflictError("transaction repeats a scoped trade identity")
        required = {event.payload.deduplication_key for event in transaction.events if isinstance(event.payload, Trade)}
        if not required <= set(keys):
            raise JournalConflictError("trade reports must declare their atomic deduplication keys")
        if transaction.cursor_after != transaction.cursor_before and not transaction.events:
            raise JournalConflictError("cursor advancement requires persisted event evidence")

        existing = self._payloads.get(transaction.transaction_id)
        if existing is not None:
            if existing[1] != payload:
                raise JournalConflictError("transaction ID was reused with different contents")
            return existing[0]
        if transaction.cursor_before != self._cursor:
            raise JournalConflictError("stale processing cursor; reload committed state before retrying")

        sequence = self.head_seq + 1
        ingress = self._last_ingress_seq
        identities: set[str] = set()
        for event in transaction.events:
            if event.sequence <= ingress or event.event_id in identities:
                raise JournalConflictError("events must preserve unique, increasing ingress order")
            if event.event_id in self._event_ids:
                raise JournalConflictError("event identity has already been committed")
            identities.add(event.event_id)
            ingress = event.sequence
        for key in keys:
            if self.contains_trade(key):
                raise DuplicateFactError("scoped trade fact has already been committed")
        control = transaction.control_record
        if control is not None:
            if control.journal_seq != sequence:
                raise JournalConflictError("control record must bind the new journal sequence")
            previous = self._control_record
            if previous is not None and control.epoch.epoch <= previous.epoch.epoch:
                raise JournalConflictError("control epoch must advance monotonically")

        # 全部守卫通过后才发布状态，保证提交原子性。
        self._transactions.append(transaction)
        self._payloads[transaction.transaction_id] = (sequence, payload)
        self._events.extend(transaction.events)
        self._event_ids.update(identities)
        self._last_ingress_seq = ingress
        self._state.update(transaction.state_updates)
        self._trade_keys.update(keys)
        if control is not None:
            self._control_record = control
        self._cursor = transaction.cursor_after
        return sequence

    def load_state(self) -> Mapping[str, object]:
        return freeze_payload(dict(sorted(self._state.items())))

    def load_control_record(self) -> ControlRecord | None:
        return self._control_record

    def replay_from(self, seq: int) -> Iterator[CanonicalEvent]:
        """回放指定序号之后的所有事件 (不包含该序号本身)；结果在调用时物化，后续提交不会泄漏."""
        require_int(seq, "journal sequence")
        if seq > self.head_seq:
            raise JournalConflictError("replay cursor is beyond committed journal history")
        events = [event for transaction in self._transactions[seq:] for event in transaction.events]
        return iter(events)

    def export_events(self) -> tuple[CanonicalEvent, ...]:
        """导出全部已提交的规范事件序列 (回测结束导出, S2-11)."""
        return tuple(self._events)

    def snapshot(self, seq: int) -> None:
        """按历史重建 seq 处的一致性快照；已存在的快照必须与历史一致."""
        require_int(seq, "snapshot sequence")
        if seq > self.head_seq:
            raise JournalConflictError("snapshot cursor is beyond committed history")
        state: dict[str, object] = {}
        keys: set[TradeKey] = set()
        control = None
        cursor = 0
        for transaction in self._transactions[:seq]:
            if transaction.cursor_before != cursor:
                raise JournalCorruptionError("transaction history does not form a contiguous processing cursor")
            state.update(transaction.state_updates)
            keys.update(transaction.deduplication_keys)
            if transaction.control_record is not None:
                control = transaction.control_record
            cursor = transaction.cursor_after
        snapshot = JournalSnapshot(self.account_id, seq, cursor, freeze_payload(state), frozenset(keys), control)
        previous = self._snapshots.get(seq)
        if previous is not None:
            if codec.dumps(previous) != codec.dumps(snapshot):
                raise JournalCorruptionError("existing snapshot does not match committed history")
            return
        self._snapshots[seq] = snapshot

    def load_snapshot(self, seq: int | None = None) -> JournalSnapshot | None:
        """返回序号不超过 seq 的最新快照；仅为历史视图，不回退实时状态或控制纪元."""
        head = self.head_seq
        if seq is None:
            seq = head
        require_int(seq, "snapshot sequence")
        if seq > head:
            raise JournalConflictError("snapshot cursor is beyond committed history")
        candidates = [stored for stored in self._snapshots if stored <= seq]
        if not candidates:
            return None
        return self._snapshots[max(candidates)]

    def load_checkpoint(self) -> JournalSnapshot:
        """读取当前投影、游标、去重集合与控制记录的一致性检查点."""
        return JournalSnapshot(
            self.account_id,
            self.head_seq,
            self._cursor,
            self.load_state(),
            frozenset(self._trade_keys),
            self._control_record,
        )
