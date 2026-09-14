"""SQLite durability/reopening and bitemporal rule lookup without fabricated defaults."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from qh_trader.core.constants import AmbiguousRuleError, Exchange, MissingRuleError, Offset
from qh_trader.core.objects import Capability, CommissionRule, InstrumentId, MarginRule, ProductId
from qh_trader.core.ports import RuleStorePort
from qh_trader.infrastructure.rule_store import RuleStore

D = Decimal
START = datetime(2024, 1, 1, tzinfo=timezone.utc)
BOUNDARY = datetime(2024, 6, 1, tzinfo=timezone.utc)


@pytest.fixture
def store():
    with RuleStore() as value:
        yield value


def register_margin(store, instrument, version="v1", ratio="0.08", **changes):
    values = dict(
        instrument=instrument,
        profile="test-profile",
        rule=MarginRule(D(ratio), D(0)),
        source_id="synthetic",
        version=version,
        effective_from=START,
        available_at=START,
    )
    store.register_margin_rule(**(values | changes))


def test_empty_store_has_no_invented_defaults_or_product_fallback(store, sample_instrument):
    assert isinstance(store, RuleStorePort)
    with pytest.raises(MissingRuleError):
        store.commission_rule(sample_instrument, "default", Offset.OPEN, START, START)
    with pytest.raises(MissingRuleError):
        store.margin_rule(sample_instrument, "default", START, START)
    register_margin(store, InstrumentId(Exchange.SHFE, "rb0"))
    with pytest.raises(MissingRuleError):
        store.margin_rule(sample_instrument, "test-profile", START, START)


def test_file_database_requires_explicit_migration_and_survives_reopen(tmp_path, sample_instrument):
    path = tmp_path / "control.db"
    with RuleStore(path) as store:
        with pytest.raises(RuntimeError, match="migrate"):
            store.margin_rule(sample_instrument, "test-profile", START, START)
        store.migrate()
        store.migrate()
        assert store.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert store.connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert store.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert store.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        register_margin(store, sample_instrument, ratio="0.123456789123456789")
    with RuleStore(path) as reopened:
        result = reopened.margin_rule(sample_instrument, "test-profile", START, START)
        assert result.value.ratio == D("0.123456789123456789")
        assert result.source_id == "synthetic" and result.version == "v1"


def test_effective_intervals_are_left_closed_right_open(store, sample_instrument):
    register_margin(store, sample_instrument, effective_to=BOUNDARY)
    assert store.margin_rule(sample_instrument, "test-profile", START, START).value.ratio == D("0.08")
    with pytest.raises(MissingRuleError):
        store.margin_rule(sample_instrument, "test-profile", START - timedelta(microseconds=1), START)
    with pytest.raises(MissingRuleError):
        store.margin_rule(sample_instrument, "test-profile", BOUNDARY, BOUNDARY)


def test_late_replacement_keeps_old_known_view_and_effective_interval(store, sample_instrument):
    register_margin(store, sample_instrument)
    publication = datetime(2024, 7, 1, tzinfo=timezone.utc)
    register_margin(
        store,
        sample_instrument,
        "v2",
        "0.10",
        effective_from=BOUNDARY,
        available_at=publication,
        replaces=("synthetic", "v1"),
    )
    june = BOUNDARY + timedelta(days=10)
    assert store.margin_rule(sample_instrument, "test-profile", june, june).version == "v1"
    assert store.margin_rule(sample_instrument, "test-profile", june, publication).version == "v2"
    assert store.margin_rule(sample_instrument, "test-profile", START, publication).version == "v1"


def test_conflicts_are_not_silently_resolved_by_latest_version(store, sample_instrument):
    register_margin(store, sample_instrument)
    register_margin(store, sample_instrument, "v2", "0.10")
    with pytest.raises(AmbiguousRuleError):
        store.margin_rule(sample_instrument, "test-profile", START, START)


def test_versions_are_immutable_and_re_registration_is_idempotent(store, sample_instrument):
    register_margin(store, sample_instrument)
    register_margin(store, sample_instrument)
    assert store.connection.execute("SELECT count(*) FROM rule_versions").fetchone()[0] == 1
    with pytest.raises(ValueError, match="immutable"):
        register_margin(store, sample_instrument, ratio="0.15")
    assert store.margin_rule(sample_instrument, "test-profile", START, START).value.ratio == D("0.08")
    with pytest.raises(MissingRuleError, match="replaced"):
        register_margin(store, sample_instrument, "v2", replaces=("synthetic", "missing"))
    assert store.connection.execute("SELECT count(*) FROM rule_versions").fetchone()[0] == 1


def test_profile_and_instrument_scopes_do_not_fall_back(store, sample_instrument):
    register_margin(store, sample_instrument)
    with pytest.raises(MissingRuleError):
        store.margin_rule(sample_instrument, "other-profile", START, START)
    with pytest.raises(MissingRuleError):
        store.margin_rule(InstrumentId(Exchange.SHFE, "rb2501"), "test-profile", START, START)


def test_contract_and_commission_payloads_roundtrip_exactly(store, sample_instrument, sample_catalog):
    spec = sample_catalog.get_spec(str(sample_instrument))
    store.register_contract_rule(sample_instrument, "test-profile", spec, "synthetic", "contract-v1", START, START)
    assert store.contract_rule(sample_instrument, "test-profile", START, START).value == spec
    rule = CommissionRule(D("1.025"), D("0.0001"), D("0.05"), "ROUND_HALF_EVEN")
    store.register_commission_rule(
        sample_instrument, "test-profile", Offset.OPEN, rule, "synthetic", "fee-v1", START, START
    )
    assert store.commission_rule(sample_instrument, "test-profile", Offset.OPEN, START, START).value == rule
    with pytest.raises(MissingRuleError):
        store.commission_rule(sample_instrument, "test-profile", Offset.CLOSE_TODAY, START, START)


def test_capabilities_remain_unknown_without_explicit_scoped_evidence(store):
    product = ProductId(Exchange.SHFE, "rb")
    assert store.capability(Exchange.SHFE, product, "test-profile", "6.7.13", "limit-order", START, START) is None
    value = Capability(False, True, "synthetic-proof")
    store.register_capability(
        Exchange.SHFE, product, "test-profile", "6.7.13", "limit-order", value, "synthetic", "cap-v1", START, START
    )
    result = store.capability(Exchange.SHFE, product, "test-profile", "6.7.13", "limit-order", START, START)
    assert result is not None and result.value.value is False
    assert store.capability(Exchange.SHFE, product, "test-profile", "6.7.12", "limit-order", START, START) is None


def test_sessions_are_registered_sets_with_visibility_and_explicit_closed_dates(
    store, sample_instrument, sample_calendar
):
    day = date(2024, 9, 9)
    sessions = sample_calendar.sessions_for_day(sample_instrument, day)
    with pytest.raises(MissingRuleError):
        store.sessions(sample_instrument, day, START)
    store.register_sessions(sample_instrument, day, sessions, "synthetic-calendar", "synthetic-v1", START, START)
    assert store.sessions(sample_instrument, day, START) == sessions
    with pytest.raises(MissingRuleError):
        store.sessions(sample_instrument, day, START - timedelta(seconds=1))
    closed_day = date(2024, 9, 14)
    store.register_sessions(sample_instrument, closed_day, (), "synthetic", "closed-v1", START, START)
    assert store.sessions(sample_instrument, closed_day, START) == ()
    with pytest.raises(AmbiguousRuleError):
        store.register_sessions(
            sample_instrument,
            day,
            (sessions[0], replace(sessions[0], session_id="duplicate")),
            "synthetic-calendar",
            "synthetic-v1",
            START,
            START,
        )


def test_naive_times_and_missing_sources_are_rejected(store, sample_instrument):
    with pytest.raises(ValueError, match="timezone-aware"):
        register_margin(store, sample_instrument, effective_from=START.replace(tzinfo=None))
    with pytest.raises(ValueError, match="nonempty"):
        register_margin(store, sample_instrument, source_id="")
