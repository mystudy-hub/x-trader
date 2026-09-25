"""S5-04 / R11 日终检查点：内核导出恢复逐字段一致、已结算历史退役、事实键增量存储、旧版布局迁移与存储缓存."""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path

import pytest

from qh_trader.core.constants import EventKind, Exchange, Offset, OrderStatus, SendState, Side
from qh_trader.core.event import CanonicalEvent, JournalTransaction
from qh_trader.core.execution import CommandKind, CommandStatus
from qh_trader.core.objects import LocalSendResult, OrderIdentity, OrderUpdate, freeze_payload
from qh_trader.engine.live_account_model import (
    CHECKPOINT_KEY,
    FACT_KEY_PREFIX,
    LEGACY_FACTS_KEY,
    AccountModelCorruptionError,
    LiveAccountModel,
    account_facts,
    fact_key,
    order_facts,
)
from scripts.live_assembly import AssemblyError, local_day_figures, migrate_account_facts, open_model_read_only
from tests.unit.test_live_account_model import (
    ACCOUNT,
    D1,
    D2,
    ECONOMICS,
    NOW,
    OPENING,
    RB,
    Harness,
    advance_event,
    command,
    intent,
    order_report,
    settlement_event,
    trade_event,
)

ROOT = Path(__file__).resolve().parents[2]
D3 = D2 + timedelta(days=1)
D4 = D3 + timedelta(days=1)

# 墙钟审计时刻：重放时本来就取当前时间，不进入检查点 (见 engine/account_checkpoint.py)
WALL_CLOCK = {
    ("Order", "updated_at"),
    ("SendAttempt", "attempted_at"),
    ("UnlinkedTrade", "received_at"),
    ("ExternalOrderRecord", "first_seen_at"),
    # 内核簿记：已应用事实计数与"由检查点恢复"标记描述重建路径，不是账户状态
    ("_Kernel", "applied"),
    ("_Kernel", "restored"),
}


def graph(value, depth: int = 0):
    """逐字段展开领域对象图；领域类新增字段而检查点未覆盖时，恢复前后的展开结果不再相等."""
    assert depth < 40, "object graph is unexpectedly deep"
    if value is None or isinstance(value, (str, int, float, bool, Decimal, date, datetime, Enum)):
        return value
    parameters = getattr(type(value), "__dataclass_params__", None)
    if parameters is not None and parameters.frozen:
        return value  # Core 值对象按值比较
    if isinstance(value, dict):
        return {key: graph(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [graph(item, depth + 1) for item in value]
    if isinstance(value, (set, frozenset)):
        return frozenset(value)
    if callable(value) and not hasattr(value, "__dict__"):
        return value
    names = list(vars(value)) if hasattr(value, "__dict__") else []
    if is_dataclass(value):
        names = [member.name for member in fields(value)]
    kind = type(value).__name__
    return {
        "__type__": kind,
        **{name: graph(getattr(value, name), depth + 1) for name in names if (kind, name) not in WALL_CLOCK},
    }


def cancel(identifier: str, order: str):
    identity = OrderIdentity(account_id=ACCOUNT, exchange=Exchange.SHFE, client_order_id=order)
    return command(identifier, identity, kind=CommandKind.CANCEL)


def control(identifier: str, kind: CommandKind, **payload):
    return command(identifier, {"reason": "fixture " + kind.value.lower(), **payload}, kind=kind)


def external_report() -> CanonicalEvent:
    update = OrderUpdate(
        identity=OrderIdentity(account_id=ACCOUNT, exchange=Exchange.SHFE, exchange_order_id="EX-UNKNOWN"),
        instrument=RB,
        side=Side.BUY,
        offset=Offset.OPEN,
        status=OrderStatus.ACCEPTED,
        quantity=1,
        filled_quantity=0,
        event_time=NOW,
        available_at=NOW,
    )
    return CanonicalEvent(
        event_id="report:external",
        kind=EventKind.ORDER_REPORT,
        event_time=NOW,
        available_at=NOW,
        sequence=0,
        source_id="fixture-broker",
        payload=update,
    )


def rich_day_one(harness: Harness) -> None:
    """覆盖成交、部分成交后撤单、明确未发送、结果未知、平仓冻结、撤单在途、外部委托、待关联成交与熔断事件."""
    harness.make_ready()
    harness.submit(command("c1", intent("o1")))
    harness.fact(order_report("o1", OrderStatus.ACCEPTED))
    harness.fact(trade_event("t1"))
    harness.fact(order_report("o1", OrderStatus.FILLED, filled=1))
    harness.submit(command("c2", intent("o2", quantity=2)))
    harness.fact(order_report("o2", OrderStatus.ACCEPTED, quantity=2))
    harness.fact(trade_event("t2", order="o2"))
    harness.fact(order_report("o2", OrderStatus.CANCELLED, filled=1, quantity=2))
    harness.gateway.result = LocalSendResult(SendState.NOT_SENT, -1, "rejected locally by gateway")
    harness.submit(command("c3", intent("o3")))
    harness.gateway.result = LocalSendResult(SendState.SENT_UNKNOWN, 0, "accepted locally; remote result unknown")
    harness.submit(command("c4", intent("o4", price=90)))
    harness.submit(command("c5", intent("o5", side=Side.SELL, offset=Offset.CLOSE_TODAY, price=130)))
    harness.fact(order_report("o5", OrderStatus.ACCEPTED, side=Side.SELL, offset=Offset.CLOSE_TODAY))
    harness.submit(cancel("c6", "o5"))
    harness.fact(external_report())
    harness.fact(trade_event("t-unlinked", order="nobody"))
    harness.submit(control("c7", CommandKind.PAUSE))
    harness.submit(control("c8", CommandKind.RESUME, cause_cleared=True, account_consistent=True))


@pytest.fixture
def rich(tmp_path):
    with Harness(tmp_path / "trading.db") as harness:
        rich_day_one(harness)
        yield harness


# ---------------------------------------------------------------------- 导出 / 恢复逐字段一致


def test_restored_kernel_matches_every_field_of_the_replayed_kernel(rich):
    model = rich.model
    kernel = model.replica()
    assert kernel.orders.external_orders and kernel.orders.pending_unlinked_trades()
    assert kernel.orders.get_order("o5").cancel_pending and kernel.risk.risk_events
    restored = model._restore_kernel(freeze_payload(model._dump_kernel(kernel)))  # noqa: SLF001
    assert graph(restored) == graph(kernel)


def test_pending_advance_and_settlement_prices_survive_the_round_trip(rich):
    rich.fact(advance_event(D1, D2))  # 缺结算价：SETTLEMENT_PENDING，不前进、不写检查点
    model = rich.model
    assert model.settlement_pending and model.checkpoint_through == 0
    kernel = model.replica()
    restored = model._restore_kernel(freeze_payload(model._dump_kernel(kernel)))  # noqa: SLF001
    assert restored.pending_advance == kernel.pending_advance
    assert graph(restored) == graph(kernel)


def test_restored_kernel_keeps_matching_after_the_same_later_facts(rich):
    model = rich.model
    kernel = model.replica()
    restored = model._restore_kernel(freeze_payload(model._dump_kernel(kernel)))  # noqa: SLF001
    later = (
        {"kind": "trade", "trade": trade_event("t4", order="o4", price="90").payload},
        {"kind": "order_report", "update": order_report("o4", OrderStatus.FILLED, filled=1).payload},
        {
            "kind": "order_report",
            "update": order_report("o5", OrderStatus.CANCELLED, side=Side.SELL, offset=Offset.CLOSE_TODAY).payload,
        },
    )
    model._apply_facts(kernel, [freeze_payload(fact) for fact in later])  # noqa: SLF001
    model._apply_facts(restored, [freeze_payload(fact) for fact in later])  # noqa: SLF001
    assert graph(restored) == graph(kernel)


# ---------------------------------------------------------------------- 日终压缩


def settle(harness: Harness, day: date, new_day: date, price: str = "100") -> None:
    harness.fact(settlement_event(day, price))
    harness.fact(advance_event(day, new_day))


def test_completed_settlement_replaces_the_fact_prefix_with_a_checkpoint(rich):
    before = rich.model.fact_count
    balance_before = rich.model.ledger.balance
    settle(rich, D1, D2, "110")
    model = rich.model
    state = rich.journal.load_state()
    assert model.trading_day == D2 and model.fact_count == 0
    assert model.checkpoint_through == before + 2 and model.checkpoints_written == 1
    assert CHECKPOINT_KEY in state and not [key for key in state if key.startswith(FACT_KEY_PREFIX)]
    assert state[CHECKPOINT_KEY]["trading_day"] == D2
    assert model.history_start == D1
    # 3 手多头按 110 结算 (各自基准 100)：+300
    assert model.ledger.balance == balance_before + Decimal("300")
    rich.submit(command("c9", intent("o9", price=105)))
    assert [fact["kind"] for fact in account_facts(rich.journal.load_state())] == ["intent", "send_result"]
    assert rich.journal.load_state()[fact_key(before + 3)]["kind"] == "intent"


def test_restart_after_checkpoint_rebuilds_an_identical_kernel(tmp_path):
    path = tmp_path / "trading.db"
    with Harness(path) as first:
        rich_day_one(first)
        settle(first, D1, D2, "110")
        first.submit(command("c9", intent("o9", price=105)))
        expected = graph(first.model.replica())
        expected_view = first.model.funds_state()
    with Harness(path, model=LiveAccountModel(ACCOUNT, OPENING, ECONOMICS, now=lambda: NOW)) as second:
        assert second.model.checkpoint_through > 0 and second.model.fact_count == 2
        assert graph(second.model.replica()) == expected
        assert second.model.funds_state() == expected_view


def test_terminal_orders_retire_at_the_second_boundary_and_their_ids_stay_reserved(rich):
    settle(rich, D1, D2)
    assert rich.model.orders.get_order("o1") is not None  # 第一个边界只登记为候选
    settle(rich, D2, D3)
    model = rich.model
    assert model.orders.get_order("o1") is None and model.orders.get_order("o3") is None
    for active in ("o4", "o5"):
        assert model.orders.get_order(active) is not None  # 在途委托与预占不退役
    reused = rich.submit(command("c-reuse", intent("o1")))
    assert reused.status == CommandStatus.REJECTED
    assert model.history_start == D2
    assert all(entry.trading_day >= D2 for entry in model.ledger.entries)


def test_late_report_for_a_retired_order_is_not_guessed(rich):
    settle(rich, D1, D2)
    settle(rich, D2, D3)
    rich.fact(trade_event("t1-late", order="o1"))
    # 已退役委托的迟到成交不按品种方向猜配：进入待关联队列，交由对账处置
    assert [item.trade.trade_id for item in rich.model.orders.pending_unlinked_trades()][-1] == "t1-late"


def test_local_day_figures_refuse_days_before_the_retained_history(rich):
    settle(rich, D1, D2)
    settle(rich, D2, D3)
    model = rich.model
    figures = local_day_figures(model.ledger, D2, history_start=model.history_start)
    assert figures.mtm_pnl == Decimal("0")  # D1、D2 结算价都是 100：D2 无盯市盈亏
    with pytest.raises(AssemblyError, match="retired by the account checkpoint"):
        local_day_figures(model.ledger, D1, history_start=model.history_start)


def trade_one_round_trip(harness: Harness, day: date, index: int, *, report_first: bool = True) -> None:
    for name, side, offset in (
        (f"open-{index}", Side.BUY, Offset.OPEN),
        (f"close-{index}", Side.SELL, Offset.CLOSE_TODAY),
    ):
        harness.submit(command("c-" + name, intent(name, side=side, offset=offset)))
        if report_first:  # CTP 通常先回 OnRtnOrder 再回 OnRtnTrade
            harness.fact(order_report(name, OrderStatus.ACCEPTED, side=side, offset=offset))
        harness.fact(trade_event("t-" + name, day=day, side=side, offset=offset, order=name))
        harness.fact(order_report(name, OrderStatus.FILLED, filled=1, side=side, offset=offset))


def test_fact_window_and_checkpoint_stay_bounded_over_many_days(tmp_path):
    with Harness(tmp_path / "trading.db") as harness:
        harness.make_ready()
        day, sizes = D1, []
        for index in range(6):
            trade_one_round_trip(harness, day, index)
            next_day = day + timedelta(days=1)
            settle(harness, day, next_day)
            day = next_day
            kernel = harness.journal.load_state()[CHECKPOINT_KEY]["kernel"]
            sizes.append((len(kernel["orders"]["orders"]), len(kernel["ledger"]["entries"])))
            assert harness.model.fact_count == 0
        # 每天两笔委托、一条平仓盯市条目：检查点只保留当天成为终态的委托 (下一边界退役) 与已结算日的条目
        assert sizes == [(2, 1)] * 6
        assert len(harness.model.replica().retired_order_ids) == 10


def test_orders_still_flagged_for_reconciliation_are_never_retired(tmp_path):
    with Harness(tmp_path / "trading.db") as harness:
        harness.make_ready()
        # 成交先于任何委托回报到达：领域层保留"发送结果未知"的对账标记，检查点不能替运维清除它
        trade_one_round_trip(harness, D1, 0, report_first=False)
        settle(harness, D1, D2)
        settle(harness, D2, D3)
        kernel = harness.model.replica()
        assert {order.client_order_id for order in kernel.orders.orders()} == {"open-0", "close-0"}
        assert all(order.reconciliation_required for order in kernel.orders.orders())


# ---------------------------------------------------------------------- 失败与迁移


def test_unverifiable_checkpoint_is_refused_and_all_facts_are_kept(rich, monkeypatch):
    model = rich.model
    original = model._dump_kernel  # noqa: SLF001
    calls = {"count": 0}

    def unstable(kernel):
        calls["count"] += 1
        data = original(kernel)
        return data | {"history_start": D4} if calls["count"] > 1 else data

    monkeypatch.setattr(model, "_dump_kernel", unstable)
    facts_before = model.fact_count
    settle(rich, D1, D2)
    assert model.checkpoints_written == 0 and model.checkpoint_through == 0
    assert "differs" in model.checkpoint_refusals[0]
    assert model.fact_count == facts_before + 2 and model.trading_day == D2


def test_malformed_checkpoint_poisons_the_model_instead_of_guessing(rich):
    settle(rich, D1, D2)
    snapshot = rich.store.checkpoint()
    stored = dict(snapshot.state[CHECKPOINT_KEY])
    stored["kernel"] = {key: value for key, value in stored["kernel"].items() if key != "orders"}
    forged = replace(snapshot, state=dict(snapshot.state) | {CHECKPOINT_KEY: stored})
    fresh = LiveAccountModel(ACCOUNT, OPENING, ECONOMICS, now=lambda: NOW)
    with pytest.raises(AccountModelCorruptionError, match="cannot be restored"):
        fresh.publish(forged)
    with pytest.raises(AccountModelCorruptionError):
        _ = fresh.ledger


def test_legacy_fact_table_needs_the_explicit_migration(tmp_path):
    path = tmp_path / "trading.db"
    with Harness(path) as first:
        first.make_ready()
        first.submit(command("c1", intent("o1")))
        first.fact(trade_event("t1"))
        expected = graph(first.model.replica())
        state = first.journal.load_state()
        legacy = tuple(state[key] for key in sorted(state) if key.startswith(FACT_KEY_PREFIX))
        checkpoint = first.store.checkpoint()
        seed = CanonicalEvent(
            event_id="legacy-layout",
            kind=EventKind.CONTROL,
            event_time=NOW,
            available_at=NOW,
            sequence=first.store.next_ingress_sequence(),
            source_id="fixture",
            payload={"fixture": "legacy"},
        )
        updates = {key: None for key in state if key.startswith(FACT_KEY_PREFIX)} | {LEGACY_FACTS_KEY: legacy}
        first.store.commit(
            JournalTransaction(
                transaction_id="legacy-layout",
                events=(seed,),
                cursor_before=checkpoint.cursor,
                cursor_after=checkpoint.cursor + 1,
                state_updates=updates,
            ),
            expected_control=checkpoint.control_record.epoch,
        )
        with pytest.raises(AccountModelCorruptionError, match="legacy"):
            first.model.publish(first.store.checkpoint())
        # 只读脚本 (结算单比对) 不写库，在内存里按同一迁移规则展开旧版布局
        journal, read_only = open_model_read_only(path, ACCOUNT, ROOT / "config" / "contract_catalog_s4_2024v1.json")
        journal.close()
        assert read_only.fact_count == len(legacy) and LEGACY_FACTS_KEY in first.journal.load_state()
        assert migrate_account_facts(first.store) == len(legacy)
        assert migrate_account_facts(first.store) == 0
        migrated = LiveAccountModel(ACCOUNT, OPENING, ECONOMICS, now=lambda: NOW)
        migrated.publish(first.store.checkpoint())
        assert graph(migrated.replica()) == expected


def test_order_facts_carry_retained_checkpoint_orders_for_the_ref_book(rich):
    settle(rich, D1, D2)
    facts = order_facts(rich.journal.load_state())
    intents = {fact["intent"].client_order_id for fact in facts if fact["kind"] == "intent"}
    assert {"o1", "o4", "o5"} <= intents
    sent = [fact for fact in facts if fact["kind"] == "send_result" and fact["client_order_id"] == "o4"]
    assert sent and sent[0]["result"].state == SendState.SENT_UNKNOWN


# ---------------------------------------------------------------------- Journal 删除与存储缓存


def test_store_cache_matches_a_fresh_journal_load_through_deletions(rich):
    settle(rich, D1, D2)
    rich.submit(command("c9", intent("o9", price=105)))
    cached = rich.store.checkpoint()
    fresh = rich.journal.load_checkpoint()
    assert cached.journal_seq == fresh.journal_seq and cached.cursor == fresh.cursor
    assert cached.state == fresh.state
    assert cached.deduplication_keys == fresh.deduplication_keys
    assert cached.control_record == fresh.control_record
    # 历史快照同样按删除语义重放事务
    rich.journal.snapshot(fresh.journal_seq)
    assert rich.journal.load_snapshot().state == fresh.state


def test_out_of_band_journal_write_invalidates_the_store_cache(rich):
    cached = rich.store.checkpoint()
    event = CanonicalEvent(
        event_id="out-of-band",
        kind=EventKind.CONTROL,
        event_time=NOW,
        available_at=NOW,
        sequence=rich.store.next_ingress_sequence(),
        source_id="fixture",
        payload={"fixture": "out-of-band"},
    )
    rich.journal.append(
        JournalTransaction(
            transaction_id="out-of-band",
            events=(event,),
            cursor_before=cached.cursor,
            cursor_after=cached.cursor + 1,
            state_updates={"fixture_note": {"written": True}},
        )
    )
    reloaded = rich.store.checkpoint()
    assert reloaded.journal_seq == cached.journal_seq + 1 and reloaded.state["fixture_note"]["written"] is True


def test_snapshot_advance_rejects_a_transaction_that_does_not_follow(rich):
    snapshot = rich.store.checkpoint()
    transaction = JournalTransaction(
        transaction_id="gap", events=(), cursor_before=snapshot.cursor, cursor_after=snapshot.cursor
    )
    with pytest.raises(ValueError, match="does not directly follow"):
        snapshot.advance(snapshot.journal_seq + 2, transaction)
    with pytest.raises(ValueError, match="does not directly follow"):
        snapshot.advance(
            snapshot.journal_seq + 1,
            replace(transaction, cursor_before=snapshot.cursor + 1, cursor_after=snapshot.cursor + 1),
        )


def test_freezing_an_already_frozen_value_returns_it_unchanged(rich):
    trade = trade_event("t-frozen").payload
    assert freeze_payload(trade) is trade
    facts = account_facts(rich.journal.load_state())
    assert freeze_payload(facts[1]["intent"]) is facts[1]["intent"]
