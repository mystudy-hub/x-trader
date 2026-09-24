"""S5-01 CTP 网关：握手、代次复核、未核验能力禁用、回调入队与结果保守化."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from qh_trader.core.constants import EventKind, Exchange, Offset, OrderStatus, OrderType, SendState, Side
from qh_trader.core.objects import (
    Capability,
    CapabilityProfile,
    ControlEpoch,
    InstrumentId,
    OrderIdentity,
    OrderIntent,
)
from qh_trader.gateway.ctp_gateway import (
    CODE_FENCED,
    CODE_NOT_READY,
    CODE_PLAN_REJECTED,
    CODE_UNSUPPORTED_CAPABILITY,
    CtpHandshakeError,
    CtpOffsetMapping,
    CtpOrderRefBook,
    CtpSettings,
    CtpTraderGateway,
    parse_order_ref_evidence,
    restore_order_refs,
)
from qh_trader.gateway.feedback_normalizer import build_normalizer
from tests.unit.fake_ctp import FakeCtpBinding

ACCOUNT = "simnow-account"
RB = InstrumentId(Exchange.SHFE, "rb2601")
DAY = date(2026, 9, 24)
EPOCH = ControlEpoch("execution-service", 1)


class Sink:
    """执行服务的回调出口等价物：只记录，不改状态."""

    def __init__(self) -> None:
        self.events: list = []
        self.errors: list[str] = []

    def enqueue(self, event) -> bool:
        self.events.append(event)
        return True

    def enqueue_callback_error(self, source_id: str, error: Exception) -> None:
        self.errors.append(f"{source_id}:{type(error).__name__}")


def make_settings(**overrides) -> CtpSettings:
    values = {
        "front_trade": "tcp://127.0.0.1:10201",
        "broker_id": "9999",
        "investor_id": "231495",
        "user_id": "231495",
        "password": "not-a-real-secret",
        "app_id": "simnow_client_test",
        "auth_code": "0000000000000000",
        "flow_dir": "runs/pytest/ctp_flow",
        "connect_timeout_s": 2.0,
        "login_timeout_s": 2.0,
        "query_timeout_s": 2.0,
    }
    values.update(overrides)
    return CtpSettings(**values)


def make_profile(*, market_orders: bool = False) -> CapabilityProfile:
    values = {"order_types.market_order": Capability(True, True, "test:market")}
    if not market_orders:
        values = {"order_types.market_order": Capability(None, False)}
    return CapabilityProfile("simnow_v6", None, values)


def make_gateway(
    *,
    binding: FakeCtpBinding | None = None,
    sink: Sink | None = None,
    authority=None,
    offsets=(),
    settings: CtpSettings | None = None,
    tick: Decimal = Decimal("1"),
    market_orders: bool = False,
    book: CtpOrderRefBook | None = None,
) -> tuple[CtpTraderGateway, Sink, CtpOrderRefBook]:
    resolved = binding if binding is not None else FakeCtpBinding()
    recorder = sink if sink is not None else Sink()
    refs = book if book is not None else CtpOrderRefBook()
    gateway = CtpTraderGateway(
        settings=settings or make_settings(),
        account_id=ACCOUNT,
        events=recorder,
        normalizer=build_normalizer(ACCOUNT, refs),
        price_tick=lambda instrument: tick,
        capability_profile=make_profile(market_orders=market_orders),
        capability_version="registered:simnow_v6",
        authority=authority or (lambda: EPOCH),
        offset_mappings=offsets,
        binding=resolved,
        ref_book=refs,
        wall_time=lambda: datetime(2026, 9, 24, 9, 30, tzinfo=timezone.utc),
    )
    return gateway, recorder, refs


def open_intent(**overrides) -> OrderIntent:
    values = {
        "client_order_id": "cid-1",
        "account_id": ACCOUNT,
        "strategy_id": "test-strategy",
        "instrument": RB,
        "side": Side.BUY,
        "offset": Offset.OPEN,
        "quantity": 1,
        "order_type": OrderType.LIMIT,
        "created_at": datetime(2026, 9, 24, 9, 30, tzinfo=timezone.utc),
        "limit_price_ticks": 3000,
    }
    values.update(overrides)
    return OrderIntent(**values)


def verified_offsets() -> tuple[CtpOffsetMapping, ...]:
    return (
        CtpOffsetMapping(
            Exchange.SHFE,
            {Offset.OPEN: "0", Offset.CLOSE: "1", Offset.CLOSE_TODAY: "3", Offset.CLOSE_YESTERDAY: "4"},
            True,
            "test:offset-mapping",
        ),
    )


# --------------------------------------------------------------------------------------- 握手


def test_handshake_authenticates_logs_in_and_confirms_settlement():
    binding = FakeCtpBinding()
    gateway, _, _ = make_gateway(binding=binding)
    report = gateway.connect()
    names = [name for name, _ in binding.api.calls]
    assert names[:4] == ["RegisterSpi", "SubscribePrivateTopic", "SubscribePublicTopic", "RegisterFront"]
    assert names[4] == "Init"
    assert names[5:8] == ["ReqAuthenticate", "ReqUserLogin", "ReqSettlementInfoConfirm"]
    assert (report.front_id, report.session_id, report.trading_day) == (12, 345678, DAY)
    assert report.api_version == "fake-api-6.7.13"
    assert report.dll_hashes and report.terminal_authentication is True
    assert report.max_order_ref == "0"
    assert gateway.trading_day == DAY
    # 会话建立不等于可以发单：先要对账放行
    assert gateway.ready_to_send is False
    assert gateway.mark_reconciled() is True
    assert gateway.ready_to_send is True


def test_terminal_authentication_is_optional_but_recorded():
    binding = FakeCtpBinding()
    gateway, _, _ = make_gateway(binding=binding, settings=make_settings(app_id=None, auth_code=None))
    report = gateway.connect()
    names = [name for name, _ in binding.api.calls]
    assert "ReqAuthenticate" not in names
    assert report.terminal_authentication is False
    assert any("not configured" in note for note in report.notes)


def test_rejected_login_keeps_the_gate_closed_and_never_sends():
    binding = FakeCtpBinding(login_code=3)
    gateway, _, _ = make_gateway(binding=binding)
    with pytest.raises(CtpHandshakeError):
        gateway.connect()
    assert gateway.fault == "login_error_3"
    assert gateway.ready_to_send is False
    result = gateway.submit(open_intent(), EPOCH)
    assert result.state == SendState.NOT_SENT and result.local_code == CODE_NOT_READY
    assert [name for name, _ in binding.api.calls].count("ReqOrderInsert") == 0


def test_silent_login_times_out_without_pretending_to_be_connected():
    binding = FakeCtpBinding(login_silent=True)
    gateway, _, _ = make_gateway(binding=binding, settings=make_settings(login_timeout_s=0.05))
    with pytest.raises(CtpHandshakeError):
        gateway.connect()
    assert gateway.fault == "login"


def test_api_version_and_dll_hashes_are_recorded_for_the_gap_evidence():
    binding = FakeCtpBinding(dll_files={"thosttraderapi_se-x.dll": "ab" * 32})
    gateway, _, _ = make_gateway(binding=binding)
    report = gateway.connect()
    assert report.dll_hashes == {"thosttraderapi_se-x.dll": "ab" * 32}


# --------------------------------------------------------------------------------------- 门禁


def test_disconnect_closes_the_send_gate_until_reconciliation():
    binding = FakeCtpBinding()
    gateway, _, _ = make_gateway(binding=binding)
    gateway.connect()
    assert gateway.mark_reconciled() is True
    binding.api.front_disconnected()
    assert gateway.ready_to_send is False
    assert gateway.needs_reconciliation is True
    result = gateway.submit(open_intent(), EPOCH)
    assert result.state == SendState.NOT_SENT and result.local_code == CODE_NOT_READY
    assert binding.api.insert_fields == []
    assert gateway.counts["front_disconnected"] == 1


def test_reconnect_requires_a_new_login_and_reconciliation():
    binding = FakeCtpBinding()
    gateway, _, _ = make_gateway(binding=binding)
    gateway.connect()
    gateway.mark_reconciled()
    binding.api.front_disconnected()
    binding.api.front_connected()
    assert gateway.ready_to_send is False
    assert gateway.maintain() is False  # 重新登录成功也不自动放行
    assert gateway.fault is None
    assert gateway.mark_reconciled() is True
    assert gateway.maintain() is True


def test_heartbeat_warning_is_counted_without_changing_the_gate():
    binding = FakeCtpBinding()
    gateway, _, _ = make_gateway(binding=binding)
    gateway.connect()
    gateway.mark_reconciled()
    binding.api.heartbeat_warning(120)
    assert gateway.counts["heartbeat_warnings"] == 1
    assert gateway.ready_to_send is True


# --------------------------------------------------------------------------------------- 代次


def test_control_epoch_is_fenced_at_the_api_call():
    binding = FakeCtpBinding()
    gateway, _, _ = make_gateway(binding=binding, authority=lambda: ControlEpoch("other", 2))
    gateway.connect()
    result = gateway.submit(open_intent(), EPOCH)
    assert result.state == SendState.NOT_SENT
    assert result.local_code == CODE_FENCED
    assert result.remote_identity is None
    assert [name for name, _ in binding.api.calls].count("ReqOrderInsert") == 0
    assert gateway.counts["fenced_calls"] == 1


def test_missing_control_authority_fences_the_call():
    gateway, _, _ = make_gateway(authority=lambda: None)
    gateway.connect()
    result = gateway.cancel(
        OrderIdentity(account_id=ACCOUNT, exchange=Exchange.SHFE, front_id=12, session_id=345678, order_ref="1"),
        EPOCH,
    )
    assert result.state == SendState.NOT_SENT and result.local_code == CODE_FENCED


# --------------------------------------------------------------------------------------- 能力


def test_unverified_offset_registration_disables_closes_but_keeps_opens():
    binding = FakeCtpBinding()
    unverified = (CtpOffsetMapping(Exchange.SHFE, {Offset.OPEN: "0", Offset.CLOSE_TODAY: "3"}, False),)
    gateway, _, _ = make_gateway(binding=binding, offsets=unverified)
    gateway.connect()
    gateway.mark_reconciled()
    close = gateway.submit(open_intent(client_order_id="cid-close", offset=Offset.CLOSE_TODAY), EPOCH)
    assert close.state == SendState.NOT_SENT
    assert close.local_code == CODE_UNSUPPORTED_CAPABILITY
    assert "not verified" in close.evidence
    opened = gateway.submit(open_intent(), EPOCH)
    assert opened.state == SendState.SENT_UNKNOWN
    assert len(binding.api.insert_fields) == 1
    assert binding.api.insert_fields[0].CombOffsetFlag == "0"


def test_verified_offset_mapping_is_used_for_each_close_bucket():
    binding = FakeCtpBinding()
    gateway, _, _ = make_gateway(binding=binding, offsets=verified_offsets())
    gateway.connect()
    gateway.mark_reconciled()
    gateway.submit(open_intent(client_order_id="cid-today", offset=Offset.CLOSE_TODAY), EPOCH)
    gateway.submit(open_intent(client_order_id="cid-yesterday", offset=Offset.CLOSE_YESTERDAY), EPOCH)
    flags = [field.CombOffsetFlag for field in binding.api.insert_fields]
    assert flags == ["3", "4"]


def test_market_orders_stay_disabled_until_the_capability_is_verified():
    binding = FakeCtpBinding()
    gateway, _, _ = make_gateway(binding=binding)
    gateway.connect()
    gateway.mark_reconciled()
    rejected = gateway.submit(
        open_intent(client_order_id="cid-market", order_type=OrderType.MARKET, limit_price_ticks=None), EPOCH
    )
    assert rejected.state == SendState.NOT_SENT and rejected.local_code == CODE_UNSUPPORTED_CAPABILITY
    gateway_market, _, _ = make_gateway(binding=FakeCtpBinding(), market_orders=True, offsets=verified_offsets())
    gateway_market.connect()
    gateway_market.mark_reconciled()
    accepted = gateway_market.submit(
        open_intent(client_order_id="cid-market", order_type=OrderType.MARKET, limit_price_ticks=None), EPOCH
    )
    assert accepted.state == SendState.SENT_UNKNOWN
    assert accepted.remote_identity is not None


def test_unknown_price_tick_refuses_the_send_locally_instead_of_guessing():
    gateway, _, _ = make_gateway(authority=lambda: EPOCH, offsets=verified_offsets())

    def missing_tick(instrument):  # noqa: ANN001, ANN202
        raise LookupError("no registered tick")

    gateway._price_tick = missing_tick
    gateway.connect()
    gateway.mark_reconciled()
    result = gateway.submit(open_intent(), EPOCH)
    # 规划阶段的本地失败可证明"没有发送"，因此返回 NOT_SENT 而不是 SENT_UNKNOWN
    assert result.state == SendState.NOT_SENT and result.local_code == CODE_PLAN_REJECTED
    assert "LookupError" in result.evidence


# --------------------------------------------------------------------------------------- 发送结果


def test_accepted_local_call_is_recorded_as_unknown_not_confirmed():
    binding = FakeCtpBinding()
    gateway, sink, book = make_gateway(binding=binding, offsets=verified_offsets())
    gateway.connect()
    gateway.mark_reconciled()
    result = gateway.submit(open_intent(), EPOCH)
    assert result.state == SendState.SENT_UNKNOWN
    assert result.local_code == 0
    assert result.remote_identity is not None
    assert result.remote_identity.front_id == 12 and result.remote_identity.session_id == 345678
    assert parse_order_ref_evidence(result.evidence) == (12, 345678, result.remote_identity.order_ref)
    assert book.resolve(12, 345678, result.remote_identity.order_ref) == "cid-1"
    # 柜台受理后的回报已归一化入队，且不带本地单号（归属由内核按三元组完成）
    assert [event.kind for event in sink.events] == [EventKind.ORDER_REPORT]
    assert sink.events[0].payload.identity.client_order_id is None


def test_order_ref_allocation_continues_from_the_counter_max_order_ref():
    binding = FakeCtpBinding(max_order_ref="0000000007")
    gateway, _, book = make_gateway(binding=binding, offsets=verified_offsets())
    gateway.connect()
    gateway.mark_reconciled()
    first = gateway.submit(open_intent(client_order_id="cid-1"), EPOCH)
    second = gateway.submit(open_intent(client_order_id="cid-2"), EPOCH)
    refs = [first.remote_identity.order_ref, second.remote_identity.order_ref]
    assert refs == ["8", "9"]
    assert book.allocated == 2


def test_unverified_nonzero_return_stays_unknown_and_keeps_reservations():
    binding = FakeCtpBinding(insert_code=-2)
    gateway, _, _ = make_gateway(binding=binding, offsets=verified_offsets())
    gateway.connect()
    gateway.mark_reconciled()
    result = gateway.submit(open_intent(), EPOCH)
    assert result.state == SendState.SENT_UNKNOWN and result.local_code == -2
    assert "not verified" in result.evidence
    assert gateway.counts["send_unknown"] == 1


def test_verified_local_rejection_codes_become_not_sent():
    binding = FakeCtpBinding(insert_code=-2)
    gateway, _, _ = make_gateway(
        binding=binding, offsets=verified_offsets(), settings=make_settings(local_reject_codes=frozenset({-2, -3}))
    )
    gateway.connect()
    gateway.mark_reconciled()
    result = gateway.submit(open_intent(), EPOCH)
    assert result.state == SendState.NOT_SENT and result.local_code == -2
    assert gateway.counts["submit_refused_local"] == 1


def test_submit_before_connection_is_refused_locally():
    gateway, _, _ = make_gateway()
    result = gateway.submit(open_intent(), EPOCH)
    assert result.state == SendState.NOT_SENT and result.local_code == CODE_NOT_READY


# --------------------------------------------------------------------------------------- 撤单


def test_cancel_uses_the_original_session_triple():
    binding = FakeCtpBinding()
    gateway, _, _ = make_gateway(binding=binding, offsets=verified_offsets())
    gateway.connect()
    gateway.mark_reconciled()
    identity = OrderIdentity(
        account_id=ACCOUNT,
        exchange=Exchange.SHFE,
        client_order_id="cid-1",
        exchange_order_id="sys-1",
        front_id=12,
        session_id=345678,
        order_ref="1",
    )
    result = gateway.cancel(identity, EPOCH)
    assert result.state == SendState.SENT_UNKNOWN and result.local_code == 0
    action = binding.api.action_fields[-1]
    assert (action.OrderRef, action.FrontID, action.SessionID, action.ActionFlag) == ("1", 12, 345678, "0")
    assert action.OrderSysID == "sys-1" and action.ExchangeID == "SHFE"


def test_cancel_without_the_session_triple_is_refused_locally():
    binding = FakeCtpBinding()
    gateway, _, _ = make_gateway(binding=binding, offsets=verified_offsets())
    gateway.connect()
    gateway.mark_reconciled()
    identity = OrderIdentity(
        account_id=ACCOUNT, exchange=Exchange.SHFE, client_order_id="cid-1", exchange_order_id="sys-1"
    )
    result = gateway.cancel(identity, EPOCH)
    assert result.state == SendState.NOT_SENT
    assert result.local_code == -6
    assert binding.api.action_fields == []


# --------------------------------------------------------------------------------------- 回调


def test_callbacks_are_normalized_and_enqueued_without_touching_gateway_state():
    binding = FakeCtpBinding()
    gateway, sink, _ = make_gateway(binding=binding, offsets=verified_offsets())
    gateway.connect()
    gateway.mark_reconciled()
    gateway.submit(open_intent(), EPOCH)
    binding.api.push_order(order_ref="1", status="1", traded=1)
    binding.api.push_trade(order_ref="1", trade_id="t-9")
    kinds = [event.kind for event in sink.events]
    assert kinds == [EventKind.ORDER_REPORT, EventKind.ORDER_REPORT, EventKind.TRADE_REPORT]
    assert gateway.ready_to_send is True
    assert gateway.counts["front_disconnected"] == 0


def test_conversion_failure_becomes_a_dead_letter_and_a_fault():
    binding = FakeCtpBinding()
    gateway, sink, _ = make_gateway(binding=binding, offsets=verified_offsets())
    gateway.connect()
    gateway.mark_reconciled()
    # 强平标志无法用当前 Offset 枚举表达：明确失败而不是当成普通平仓
    binding.api.push_order(order_ref="1", status="3")
    binding.api.spi.OnRtnTrade(
        type(
            "T",
            (),
            {
                "InstrumentID": "rb2601",
                "ExchangeID": "SHFE",
                "TradeID": "t1",
                "Direction": "0",
                "OffsetFlag": "2",
                "Price": 3000.0,
                "Volume": 1,
                "TradeDate": "20260924",
                "TradeTime": "09:31:00",
                "TradingDay": "20260924",
                "OrderRef": "1",
                "OrderSysID": "sys-1",
            },
        )()
    )
    assert sink.errors == ["ctp:CtpNormalizationError"]
    assert gateway.router.faulted == "trade_report_conversion"
    assert gateway.router.counts["trade_reports"] == 0


def test_order_ref_evidence_round_trip_and_restore():
    evidence = "ctp-order-ref front=12 session=345678 order_ref=42; ReqOrderInsert accepted locally"
    assert parse_order_ref_evidence(evidence) == (12, 345678, "42")
    assert parse_order_ref_evidence("no marker here") is None
    restored = restore_order_refs(
        (
            {"kind": "send_result", "client_order_id": "cid-9", "result": type("R", (), {"evidence": evidence})()},
            {"kind": "send_result", "client_order_id": "cid-8", "result": type("R", (), {"evidence": "nothing"})()},
        ),
        {},
    )
    assert restored == [(12, 345678, "42", "cid-9")]
    book = CtpOrderRefBook(restored=restored)
    assert book.resolve(12, 345678, "42") == "cid-9"
    assert book.restored == 1


def test_night_session_reports_keep_the_counter_trading_day_and_local_receive_time():
    binding = FakeCtpBinding(insert_date="20260924", insert_time="21:30:00", trading_day="20260925")
    gateway, sink, _ = make_gateway(binding=binding, offsets=verified_offsets())
    gateway.connect()
    gateway.mark_reconciled()
    gateway.submit(open_intent(), EPOCH)
    payload = sink.events[-1].payload
    assert payload.event_time == datetime(2026, 9, 24, 9, 30, tzinfo=timezone.utc)
    assert payload.event_time <= datetime.now(timezone.utc) + timedelta(seconds=1)


def test_status_and_capabilities_are_redacted_and_serializable():
    binding = FakeCtpBinding()
    gateway, _, _ = make_gateway(binding=binding, offsets=verified_offsets())
    gateway.connect()
    status = gateway.status()
    assert "not-a-real-secret" not in str(status)
    assert status["broker_id"] == "9999"
    caps = gateway.capabilities()
    assert caps.source_id == "broker_profile:simnow_v6"
    assert caps.value.values["order_types.market_order"].verified is False
    assert OrderStatus.ACCEPTED.value == "ACCEPTED"
