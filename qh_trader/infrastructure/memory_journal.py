"""[Infrastructure 层] 纯内存 JournalPort 适配器 (S2-11, FR-REC-01).

用于回测与极速单元测试环境，实现原子事务提交、去重检查、不可变快照与历史回放，
无需建立物理 SQLite 文件连接，回测结束后可完整导出规范事件。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from qh_trader.core.constants import DuplicateFactError, JournalConflictError
from qh_trader.core.event import CanonicalEvent, JournalSnapshot, JournalTransaction
from qh_trader.core.objects import ControlRecord, TradeKey, require_text


def _key_str(key: TradeKey) -> str:
    ex_val = key.exchange.value if hasattr(key.exchange, "value") else key.exchange
    parts = [key.account_id, str(ex_val), str(key.trading_day), key.trade_id]
    if key.extra_scope:
        parts.extend(key.extra_scope)
    return "|".join(parts)


class MemoryJournal:
    """纯内存 JournalPort 适配器实现."""

    def __init__(self, account_id: str) -> None:
        require_text(account_id, "account_id")
        self.account_id = account_id

        self._seq: int = 0
        self._transactions: list[JournalTransaction] = []
        self._committed_events: list[tuple[int, CanonicalEvent]] = []
        self._trade_keys: dict[str, TradeKey] = {}

        # transaction_id -> committed_seq (用于同事务幂等)
        self._tx_id_to_seq: dict[str, int] = {}
        self._current_cursor: int = 0
        self._control_record: ControlRecord | None = None
        self._latest_state: dict[str, Any] = {}
        self._snapshots: dict[int, JournalSnapshot] = {}

    def append(self, transaction: JournalTransaction) -> int:
        """原子提交事务 (FR-DATA-07, FR-REC-01)."""
        # 1. 幂等性检查
        tx_id = transaction.transaction_id
        if tx_id in self._tx_id_to_seq:
            return self._tx_id_to_seq[tx_id]

        # 2. 游标一致性检查
        if transaction.cursor_before != self._current_cursor:
            raise JournalConflictError(
                f"cursor conflict: expected {self._current_cursor}, got {transaction.cursor_before}"
            )

        # 3. 事实去重检查 (同一成交键不可重复入账)
        for tk in transaction.deduplication_keys:
            s_key = _key_str(tk)
            if s_key in self._trade_keys:
                raise DuplicateFactError(f"duplicate trade fact: {s_key}")

        # 4. 执行原子提交
        self._seq += 1
        committed_seq = self._seq

        for tk in transaction.deduplication_keys:
            self._trade_keys[_key_str(tk)] = tk

        for event in transaction.events:
            self._committed_events.append((committed_seq, event))

        if transaction.control_record is not None:
            self._control_record = transaction.control_record

        if transaction.state_updates:
            self._latest_state.update(transaction.state_updates)

        self._current_cursor = transaction.cursor_after

        self._transactions.append(transaction)
        self._tx_id_to_seq[tx_id] = committed_seq

        # 自动生成当前一致性检查点
        self.snapshot(committed_seq)
        return committed_seq

    def snapshot(self, seq: int) -> None:
        """记录指定序号的一致性快照."""
        snap = JournalSnapshot(
            self.account_id,
            seq,
            self._current_cursor,
            dict(self._latest_state),
            frozenset(self._trade_keys.values()),
            self._control_record,
        )
        self._snapshots[seq] = snap

    def replay_from(self, seq: int) -> Iterator[CanonicalEvent]:
        """回放指定序号之后的所有事件 (不包含该序号本身)."""
        if seq > self._seq:
            raise JournalConflictError("replay cursor is beyond committed journal history")
        for j_seq, event in self._committed_events:
            if j_seq > seq and j_seq <= self._seq:
                yield event

    def load_control_record(self) -> ControlRecord | None:
        return self._control_record

    def load_checkpoint(self) -> JournalSnapshot:
        """加载最新的检查点快照."""
        if not self._snapshots:
            return JournalSnapshot(
                self.account_id,
                0,
                0,
                {},
                frozenset(),
                self._control_record,
            )
        latest_seq = max(self._snapshots.keys())
        return self._snapshots[latest_seq]

    def load_snapshot(self, seq: int | None = None) -> JournalSnapshot | None:
        if seq is None:
            return self.load_checkpoint()
        return self._snapshots.get(seq)

    def contains_trade(self, key: TradeKey) -> bool:
        return _key_str(key) in self._trade_keys
