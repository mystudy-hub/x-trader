"""合并回归：持久费率经过整库备份、检查点重放和离线恢复后仍保持相同口径。"""

from dataclasses import replace
from decimal import Decimal

import pytest

from qh_trader.core.constants import Offset, Side
from qh_trader.engine.live_account_model import LiveAccountModel
from scripts.backup_state import backup_state, restore_state, verify_backup
from scripts.live_assembly import AssemblyError, ExecutionSpec, assemble, open_model_read_only
from scripts.replay_events import replay_events
from tests.unit import test_live_account_model as fixture
from tests.unit.test_commission_schedule import economics


@pytest.mark.parametrize("compacted", [False, True])
def test_counter_economics_survive_backup_and_guarded_restore(tmp_path, compacted):
    eco = economics()
    model = LiveAccountModel(fixture.ACCOUNT, fixture.OPENING, {fixture.RB: eco}, now=lambda: fixture.NOW)
    original = tmp_path / "original.db"
    backup_directory = tmp_path / "backup"
    with fixture.Harness(original, model=model) as account:
        account.make_ready()
        account.submit(fixture.command("open", fixture.intent("open")))
        account.fact(fixture.trade_event("fill", order="open"))
        if compacted:
            account.fact(fixture.settlement_event(fixture.D1, "105"))
            account.fact(fixture.advance_event(fixture.D1, fixture.D2))
        assert account.model.ledger.total_commission == Decimal("0.2")
        balance = account.model.ledger.balance
        account.journal.snapshot(account.journal.head_seq)
        saved = account.journal.load_checkpoint()
        manifest = backup_state(original, backup_directory)
        assert verify_backup(backup_directory) == manifest
        assert account.journal.load_checkpoint() == saved

    restore_directory = tmp_path / "offline"
    receipt = restore_state(backup_directory, restore_directory)
    database = restore_directory / "trading.db"
    assert receipt["offline_only"] and not receipt["trading_authorized"]
    replay = replay_events(database, fixture.ACCOUNT)
    assert replay["consistent"] and replay["offline_restore"]
    assert replay["head_seq"] == saved.journal_seq
    journal, restored = open_model_read_only(database, fixture.ACCOUNT, tmp_path / "missing-catalog.json")
    try:
        assert restored.ledger.balance == balance
        assert restored.ledger.total_commission == Decimal("0.2")
        assert restored._economics_for(fixture.RB) == eco
        close = fixture.intent(
            "close", side=Side.SELL, offset=Offset.CLOSE_YESTERDAY if compacted else Offset.CLOSE_TODAY
        )
        plan = restored.stage_command(fixture.command("close", close))
        assert plan.approved, plan.reason
        checkpoint = journal.load_checkpoint()
        restored.publish(replace(checkpoint, state=dict(checkpoint.state) | dict(plan.state_updates)))
        assert restored.ledger.get_funds_reservation("close").fee == (Decimal("0.4") if compacted else Decimal("0.6"))
    finally:
        journal.close()
    spec = ExecutionSpec(
        mode="paper",
        account_id=fixture.ACCOUNT,
        journal_path=database,
        catalog_path=tmp_path / "missing-catalog.json",
        symbols=(str(fixture.RB),),
        initial_capital=fixture.OPENING.initial_capital,
        trading_day=fixture.D2 if compacted else fixture.D1,
        controller_id="unverified-restored-controller",
        heartbeat_path=tmp_path / "heartbeat.json",
    )
    with pytest.raises(AssemblyError, match="offline restore"):
        assemble(spec, economics_override={fixture.RB: eco})
