"""策略独立游标存储的持久化、独占与破损隔离测试。"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from qh_trader.core.constants import JournalCorruptionError
from qh_trader.core.execution import ExecutionOwnershipError
from qh_trader.infrastructure.strategy_checkpoint import SQLiteStrategyCheckpointStore


def payload(processing=True):
    return {
        "stream_id": "configured-strategy",
        "bar_end": datetime(2026, 9, 28, tzinfo=timezone.utc),
        "processing": processing,
        "pending_command_ids": ("order-1",),
        "tick_time": None,
        "tick_keys": (),
    }


def test_checkpoint_survives_close_and_retains_incomplete_status(tmp_path):
    path = tmp_path / "strategy.db"
    with SQLiteStrategyCheckpointStore(path) as store:
        assert store.load("configured-strategy") is None
        store.save("configured-strategy", payload())
    with SQLiteStrategyCheckpointStore(path) as reopened:
        assert reopened.load("configured-strategy") == payload()
        reopened.save("configured-strategy", payload(False))
    with SQLiteStrategyCheckpointStore(path) as final:
        assert final.load("configured-strategy")["processing"] is False


def test_checkpoint_exclusive_lock_released_on_close(tmp_path):
    path = tmp_path / "strategy.db"
    with SQLiteStrategyCheckpointStore(path):
        with pytest.raises(ExecutionOwnershipError, match="another producer"):
            SQLiteStrategyCheckpointStore(path)
    with SQLiteStrategyCheckpointStore(path) as reopened:
        reopened.save("configured-strategy", payload())


def test_store_refuses_trading_database_without_mutating_it(tmp_path):
    path = tmp_path / "account.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE journal_meta(account_id TEXT)")
    with pytest.raises(ValueError, match="unrelated"):
        SQLiteStrategyCheckpointStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("journal_meta",)]
    with pytest.raises(ValueError, match="separate"):
        SQLiteStrategyCheckpointStore(tmp_path / "trading.db")


def test_corrupted_payload_fails_closed(tmp_path):
    path = tmp_path / "strategy.db"
    with SQLiteStrategyCheckpointStore(path) as store:
        store.save("configured-strategy", payload())
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE strategy_checkpoints SET payload='corrupted'")
    with SQLiteStrategyCheckpointStore(path) as store:
        with pytest.raises(JournalCorruptionError, match="checksum"):
            store.load("configured-strategy")


def test_invalid_save_leaves_last_committed_checkpoint(tmp_path):
    with SQLiteStrategyCheckpointStore(tmp_path / "strategy.db") as store:
        store.save("configured-strategy", payload())
        with pytest.raises(ValueError, match="stream"):
            store.save("other", payload(False))
        with pytest.raises(TypeError):
            store.save("configured-strategy", {**payload(False), "unsupported": object()})
        assert store.load("configured-strategy") == payload()


def test_cross_thread_access_and_closed_store_are_rejected(tmp_path):
    store = SQLiteStrategyCheckpointStore(tmp_path / "strategy.db")
    with ThreadPoolExecutor() as pool:
        with pytest.raises(ExecutionOwnershipError, match="owning thread"):
            pool.submit(store.load, "configured-strategy").result()
    store.close()
    with pytest.raises(ExecutionOwnershipError):
        store.load("configured-strategy")
