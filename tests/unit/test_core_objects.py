"""Core value contracts: precision, provenance, visibility and stable identities."""

from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from qh_trader.core.constants import (
    EventKind,
    Exchange,
    MarketPhase,
    Offset,
    OrderStatus,
    OrderType,
    PositionSide,
    PriceType,
    QualityFlag,
    SendState,
    SeriesKind,
    Side,
)
from qh_trader.core.event import CanonicalEvent
from qh_trader.core.objects import (
    AccountFunds,
    Bar,
    Capability,
    CapabilityProfile,
    CommissionRule,
    ContractSpec,
    ControlEpoch,
    ControlRecord,
    ExecutionReference,
    InstrumentId,
    LocalSendResult,
    MarginRule,
    OrderIdentity,
    OrderIntent,
    OrderUpdate,
    Permissions,
    Position,
    ProductId,
    QueryBatch,
    QueryRateLimit,
    QueryResult,
    RecordMeta,
    SeriesId,
    Session,
    Settlement,
    Tick,
    Trade,
    TradeKey,
    VersionedValue,
    freeze_payload,
)

SHANGHAI = timezone(timedelta(hours=8))
START = datetime(2024, 9, 9, 21, tzinfo=SHANGHAI)
END = START + timedelta(hours=1)
DAY = date(2024, 9, 10)
RB = InstrumentId(Exchange.SHFE, "rb2410")
D = Decimal


def meta(**changes):
    values = dict(
        event_time=END,
        available_at=END,
        ingested_at=END + timedelta(days=365),
        trading_day=DAY,
        source_id="test-only",
        source_version="v1",
        ingest_seq=1,
    )
    return RecordMeta(**(values | changes))


def bar(**changes):
    values = dict(
        instrument=RB,
        meta=meta(),
        bar_start=START,
        bar_end=END,
        interval="1h",
        open=D("100"),
        high=D("102"),
        low=D("99"),
        close=D("101"),
        volume=20,
        turnover=D("20100.00"),
        open_interest=200,
        open_time=START,
        includes_auction=False,
    )
    return Bar(**(values | changes))


def intent(**changes):
    values = dict(
        client_order_id="local-1",
        account_id="account-1",
        strategy_id="strategy-1",
        instrument=RB,
        side=Side.BUY,
        offset=Offset.OPEN,
        quantity=1,
        order_type=OrderType.LIMIT,
        limit_price_ticks=3500,
        created_at=START,
    )
    return OrderIntent(**(values | changes))


def trade(**changes):
    values = dict(
        account_id="account-1",
        instrument=RB,
        trading_day=DAY,
        trade_id="remote-1",
        side=Side.BUY,
        offset=Offset.OPEN,
        quantity=1,
        price=D("3500.2"),
        event_time=START,
        available_at=START + timedelta(milliseconds=100),
        deduplication_key=TradeKey("account-1", Exchange.SHFE, DAY, "remote-1"),
    )
    return Trade(**(values | changes))


def test_night_session_day_is_independent_of_physical_and_import_dates():
    record = meta()
    assert record.event_time == datetime(2024, 9, 9, 14, tzinfo=timezone.utc)
    assert record.event_time.tzinfo is timezone.utc
    assert record.trading_day == DAY
    assert record.trading_day != record.event_time.date()
    assert record.ingested_at.year == 2025
    assert record.receive_time is None
    assert record.visible_at(END)
    assert not record.visible_at(END - timedelta(microseconds=1))
    with pytest.raises(FrozenInstanceError):
        record.trading_day = date(2024, 9, 9)


@pytest.mark.parametrize("field", ["event_time", "available_at", "ingested_at", "receive_time"])
def test_record_rejects_naive_times(field):
    with pytest.raises(ValueError, match="timezone-aware"):
        meta(**{field: END.replace(tzinfo=None)})


@pytest.mark.parametrize(
    "changes",
    [
        {"trading_day": START},
        {"ingest_seq": True},
        {"source_seq": -1},
        {"source_version": " "},
        {"quality_flags": 0},
    ],
)
def test_provenance_cannot_be_implicit_or_weakly_typed(changes):
    with pytest.raises((TypeError, ValueError)):
        meta(**changes)


def test_quality_flags_can_be_combined_without_losing_provenance():
    record = meta(quality_flags=QualityFlag.STALE | QualityFlag.PARTIAL)
    assert record.quality_flags & QualityFlag.STALE
    assert record.source_version == "v1"


def test_final_bar_cannot_be_visible_before_close():
    with pytest.raises(ValueError, match="before its end"):
        bar(meta=meta(available_at=END - timedelta(seconds=1)))
    assert bar().meta.visible_at(END)


def test_bar_preserves_explicit_auction_open_before_continuous_interval():
    opening = START - timedelta(minutes=1)
    with pytest.raises(ValueError, match="auction inclusion"):
        bar(open_time=opening)
    assert bar(open_time=opening, includes_auction=True).open_time == opening


@pytest.mark.parametrize(
    "changes",
    [
        {"bar_end": START},
        {"open_time": END},
        {"low": D("101")},
        {"volume": -1},
        {"volume": True},
        {"turnover": D("-1")},
        {"meta": {}},
        {"includes_auction": "false"},
    ],
)
def test_bar_rejects_inconsistent_or_ambiguous_values(changes):
    with pytest.raises((TypeError, ValueError)):
        bar(**changes)


@pytest.mark.parametrize("bad_price", [100.0, "100", True, D("NaN"), D("Infinity")])
def test_financial_fields_require_finite_decimal(bad_price):
    with pytest.raises((TypeError, ValueError)):
        bar(open=bad_price)
    with pytest.raises((TypeError, ValueError)):
        AccountFunds(bad_price, None, None, None)


@pytest.mark.parametrize("kind", [SeriesKind.SPREAD, SeriesKind.ADJUSTED])
def test_signed_derived_prices_are_not_subject_to_actual_contract_price_assumptions(kind):
    record = bar(instrument=SeriesId("rb-derived", kind), open=D("-10"), high=D("-8"), low=D("-12"), close=D("-9"))
    assert record.close == D("-9")


def test_execution_reference_does_not_invent_available_volume_or_early_visibility():
    reference = ExecutionReference(
        instrument=RB,
        meta=meta(event_time=START, available_at=START),
        session_id="night",
        reference_time=START,
        price_type=PriceType.BAR_OPEN,
        price=D("100"),
        source_record_id="source-row-1",
        resolution="1h",
    )
    assert reference.available_volume is None
    with pytest.raises(ValueError, match="before its reference"):
        replace(reference, meta=meta(available_at=START - timedelta(seconds=1)))


def test_tick_can_express_empty_book_but_not_missing_cumulative_quantity():
    tick = Tick(
        instrument=RB,
        meta=meta(),
        last_price=None,
        bid_price=None,
        ask_price=None,
        bid_volume=None,
        ask_volume=None,
        cumulative_volume=0,
        cumulative_turnover=D(0),
        open_interest=200,
        pre_settlement_price=D("100"),
        upper_limit_price=None,
        lower_limit_price=None,
        phase=MarketPhase.AUCTION_SUBMIT,
    )
    assert tick.bid_price is None
    with pytest.raises(TypeError):
        replace(tick, cumulative_volume=None)


def test_settlement_cannot_precede_its_publication():
    settlement = Settlement(
        instrument=RB,
        meta=meta(),
        settlement_price=D("100.123"),
        pre_settlement_price=None,
        published_at=END,
        is_final=False,
    )
    assert not settlement.is_final
    with pytest.raises(ValueError, match="before publication"):
        replace(settlement, published_at=END + timedelta(seconds=1))


def test_version_effectivity_and_visibility_have_independent_boundaries():
    rule = VersionedValue[CommissionRule](
        value=CommissionRule(D(2), D("0.0001"), D("0.01"), "ROUND_HALF_UP"),
        source_id="test-source",
        version="fee-v2",
        effective_from=START,
        effective_to=END,
        available_at=END + timedelta(days=1),
    )
    assert rule.effective_at(START)
    assert rule.effective_at(END - timedelta(microseconds=1))
    assert not rule.effective_at(END)
    assert not rule.visible_at(START)
    assert rule.visible_at(END + timedelta(days=1))
    with pytest.raises(ValueError, match="nonempty"):
        replace(rule, effective_to=START)


def test_frozen_payload_snapshots_nested_containers_and_dataclass_fields():
    @dataclass(frozen=True)
    class SourceRecord:
        values: object

    source = {"rows": [{"value": D("1.25")}], "tags": {"one"}}
    snapshot = freeze_payload(SourceRecord(source))
    source["rows"][0]["value"] = D("9")
    source["tags"].add("two")
    assert snapshot.values["rows"][0]["value"] == D("1.25")
    assert snapshot.values["tags"] == frozenset({"one"})
    with pytest.raises(TypeError):
        snapshot.values["rows"][0]["value"] = D(0)


def test_mutable_gateway_objects_are_not_canonical_payloads():
    @dataclass
    class GatewayRecord:
        price: float

    with pytest.raises(TypeError, match="mutable gateway"):
        freeze_payload(GatewayRecord(1.5))


def test_sessions_are_left_closed_right_open_and_permissions_are_explicit():
    session = Session(
        instrument=RB,
        session_id="cancel-window",
        trading_day=DAY,
        start=START,
        end=END,
        phase=MarketPhase.CANCEL_ONLY,
        permissions=Permissions(False, True, False),
        rule_version="test-v1",
        source_id="test-only",
        available_at=START - timedelta(days=1),
    )
    assert session.contains(START)
    assert not session.contains(END)
    assert session.permissions.cancel and not session.permissions.submit
    with pytest.raises(ValueError, match="unknown sessions"):
        replace(session, phase=MarketPhase.UNKNOWN)
    unknown = replace(session, phase=MarketPhase.UNKNOWN, permissions=Permissions(False, False, False))
    assert not unknown.permissions.match
    with pytest.raises(TypeError):
        Permissions("false", False, False)


@pytest.mark.parametrize("instrument", [ProductId(Exchange.SHFE, "rb"), SeriesId("rb-main", SeriesKind.ADJUSTED)])
def test_product_or_derived_series_cannot_be_a_final_order(instrument):
    with pytest.raises(TypeError, match="actual contract"):
        intent(instrument=instrument)


@pytest.mark.parametrize(
    "changes",
    [
        {"quantity": True},
        {"quantity": D(1)},
        {"quantity": 0},
        {"limit_price_ticks": 3500.5},
        {"limit_price_ticks": True},
        {"side": "BUY"},
        {"order_type": OrderType.MARKET},
    ],
)
def test_order_quantity_and_price_do_not_silently_coerce(changes):
    with pytest.raises((TypeError, ValueError)):
        intent(**changes)


def test_local_return_code_does_not_confirm_remote_processing():
    accepted = LocalSendResult(SendState.SENT_UNKNOWN, 0, "local API accepted the request")
    assert accepted.state == SendState.SENT_UNKNOWN
    uncertain = LocalSendResult(SendState.SENT_UNKNOWN, -1, "transport outcome uncertain")
    assert uncertain.state != SendState.NOT_SENT
    with pytest.raises(ValueError, match="cannot itself confirm"):
        LocalSendResult(SendState.CONFIRMED_REMOTE, 0, "local return code")


def test_order_identity_can_be_remote_only_but_cannot_use_partial_session_tuple():
    remote = OrderIdentity(account_id="account-1", exchange=Exchange.SHFE, exchange_order_id="remote-1")
    assert remote.client_order_id is None
    original = OrderIdentity(
        account_id="account-1", exchange=Exchange.SHFE, front_id=1, session_id=0, order_ref="reference"
    )
    assert original.session_id == 0
    # 柜台会话号可以为负（SimNow 实测返回负值），符号不是本地可假定的不变量
    negative = OrderIdentity(
        account_id="account-1", exchange=Exchange.SHFE, front_id=1, session_id=-815893143, order_ref="7"
    )
    assert negative.session_id < 0
    with pytest.raises(ValueError, match="original session identity"):
        replace(original, front_id=None)
    with pytest.raises(ValueError, match="nonempty"):
        replace(remote, exchange_order_id=" ")


def test_order_report_rejects_exchange_mismatch_and_impossible_quantity():
    identity = OrderIdentity(account_id="account-1", exchange=Exchange.SHFE, client_order_id="local-1")
    report = OrderUpdate(
        identity=identity,
        instrument=RB,
        side=Side.BUY,
        offset=Offset.OPEN,
        status=OrderStatus.PARTIALLY_FILLED,
        quantity=2,
        filled_quantity=1,
        event_time=START,
        available_at=END,
    )
    with pytest.raises(ValueError, match="exceeds"):
        replace(report, filled_quantity=3)
    with pytest.raises(ValueError, match="exchange"):
        replace(report, identity=replace(identity, exchange=Exchange.DCE))


def test_trade_id_is_scoped_to_account_exchange_day_and_adapter_extensions():
    key = TradeKey("account-1", Exchange.SHFE, DAY, "123")
    keys = {
        key,
        replace(key, account_id="account-2"),
        replace(key, exchange=Exchange.DCE),
        replace(key, trading_day=DAY + timedelta(days=1)),
        replace(key, extra_scope=("session-2",)),
    }
    assert len(keys) == 5
    with pytest.raises(ValueError, match="deduplication scope"):
        trade(deduplication_key=key)
    with pytest.raises(ValueError, match="account and exchange"):
        trade(
            order_identity=OrderIdentity(
                account_id="account-2", exchange=Exchange.SHFE, exchange_order_id="remote-order-1"
            )
        )
    assert trade().order_identity is None  # An unlinked real trade must still be representable.


def test_trade_event_cannot_override_report_visibility():
    report = trade()
    event = CanonicalEvent[Trade](
        event_id="trade-event",
        kind=EventKind.TRADE_REPORT,
        event_time=report.event_time,
        available_at=report.available_at,
        sequence=1,
        source_id="test-only",
        payload=report,
    )
    assert event.payload == report
    with pytest.raises(ValueError, match="timestamps must agree"):
        replace(event, available_at=report.available_at - timedelta(seconds=1))


def test_frozen_positions_cannot_exceed_the_matching_day_bucket():
    position = Position(
        instrument=RB, side=PositionSide.LONG, hedge_flag="speculation", pos_yd=2, pos_td=3, frozen_yd=2, frozen_td=0
    )
    with pytest.raises(ValueError, match="matching position buckets"):
        replace(position, frozen_yd=3)


def test_contract_spec_preserves_full_delivery_year_and_exact_tick():
    spec = ContractSpec(
        instrument=RB,
        product=ProductId(Exchange.SHFE, "rb"),
        delivery_year=2024,
        delivery_month=10,
        multiplier=D(10),
        price_tick=D("0.5"),
        listed_on=date(2023, 10, 16),
        last_trading_day=date(2024, 10, 15),
    )
    assert spec.price_tick == D("0.5")
    with pytest.raises(ValueError):
        replace(spec, delivery_year=24)
    with pytest.raises(ValueError):
        replace(spec, price_tick=D(0))


def test_unknown_capability_cannot_become_an_implicit_true_or_default():
    unknown = Capability(None, False)
    denied = Capability(False, True, "test-evidence")
    for value in (unknown, denied):
        with pytest.raises(TypeError, match="explicitly"):
            bool(value)
    assert denied.verified and denied.value is False
    with pytest.raises(ValueError):
        Capability(True, False)
    with pytest.raises(ValueError):
        Capability(True, True)
    values = {"market_order": unknown}
    profile = CapabilityProfile("test-profile", None, values)
    values["market_order"] = Capability(True, True, "later-evidence")
    assert profile.values["market_order"].value is None


def test_empty_query_remains_incomplete_until_explicit_completion():
    batch = QueryBatch("query-1", "account-1", DAY, START)
    result = QueryResult[AccountFunds](
        batch=batch, records=(), available_at=END, source_id="test-query", source_version="v1"
    )
    assert not result.complete
    assert result.records == ()
    finished = replace(result, complete=True)
    assert finished.complete
    assert finished.batch == batch
    with pytest.raises(TypeError):
        replace(result, complete="true")


def test_funds_keep_unknown_fields_and_rules_keep_precision_without_early_rounding():
    funds = AccountFunds(D("100.123456"), D("110.123456"), None, None)
    assert funds.balance == D("100.123456")
    assert funds.available_for_new_trades is None
    margin = MarginRule(D("0.123456"), D(0))
    assert margin.ratio == D("0.123456")
    record = ControlRecord(ControlEpoch("controller-1", 3), START, 2)
    assert record.epoch.epoch == 3
    with pytest.raises(TypeError):
        ControlEpoch("controller-1", True)
    with pytest.raises(ValueError):
        QueryRateLimit(1000, 0)
