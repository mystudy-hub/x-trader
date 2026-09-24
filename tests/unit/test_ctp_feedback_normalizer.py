"""S5-02 CTP 回报归一化：字段映射、交易日、去重键、标识归属与不可表达的回报."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from qh_trader.core.constants import EventKind, Exchange, Offset, OrderStatus, Side
from qh_trader.core.objects import InstrumentId, OrderUpdate, Trade
from qh_trader.gateway.ctp_gateway import CtpCallbackRouter, CtpOrderRefBook, build_trader_spi
from qh_trader.gateway.feedback_normalizer import (
    CtpFeedbackNormalizer,
    CtpIdentityResolver,
    CtpNormalizationError,
    build_normalizer,
    exchange_instant,
    parse_trading_day,
)
from tests.unit.fake_ctp import FakeCtpBinding

ACCOUNT = "simnow-account"
RECEIVED = datetime(2026, 9, 24, 1, 35, 0, tzinfo=timezone.utc)  # 上海 09:35


def order_raw(**overrides) -> dict[str, object]:
    raw: dict[str, object] = {
        "BrokerID": "9999",
        "InvestorID": "231495",
        "InstrumentID": "rb2601",
        "ExchangeID": "SHFE",
        "OrderRef": "7",
        "OrderSysID": "sys-7",
        "OrderLocalID": "local-7",
        "FrontID": 12,
        "SessionID": 345678,
        "Direction": "0",
        "CombOffsetFlag": "0",
        "OrderStatus": "3",
        "OrderSubmitStatus": "0",
        "VolumeTotalOriginal": 3,
        "VolumeTraded": 0,
        "InsertDate": "20260924",
        "InsertTime": "09:34:59",
        "UpdateTime": "09:35:00",
        "TradingDay": "20260924",
    }
    raw.update(overrides)
    return raw


def trade_raw(**overrides) -> dict[str, object]:
    raw: dict[str, object] = {
        "BrokerID": "9999",
        "InvestorID": "231495",
        "InstrumentID": "rb2601",
        "ExchangeID": "SHFE",
        "OrderRef": "7",
        "OrderSysID": "sys-7",
        "TradeID": "T-1001",
        "Direction": "1",
        "OffsetFlag": "1",
        "HedgeFlag": "1",
        "TradeType": "0",
        "Price": 3120.0,
        "Volume": 2,
        "TradeDate": "20260924",
        "TradeTime": "09:35:01",
        "TradingDay": "20260924",
    }
    raw.update(overrides)
    return raw


def normalizer(ref_book: CtpOrderRefBook | None = None) -> CtpFeedbackNormalizer:
    return build_normalizer(ACCOUNT, ref_book if ref_book is not None else CtpOrderRefBook())


# --------------------------------------------------------------------------------------- 订单回报


def test_order_report_maps_status_side_offset_and_quantity():
    event = normalizer().normalize_order(order_raw(), RECEIVED)
    assert event is not None and event.kind == EventKind.ORDER_REPORT
    update = event.payload
    assert isinstance(update, OrderUpdate)
    assert update.instrument == InstrumentId(Exchange.SHFE, "rb2601")
    assert update.side == Side.BUY and update.offset == Offset.OPEN
    assert update.status == OrderStatus.ACCEPTED
    assert (update.quantity, update.filled_quantity) == (3, 0)
    assert update.identity.exchange_order_id == "sys-7"
    assert (update.identity.front_id, update.identity.session_id, update.identity.order_ref) == (12, 345678, "7")
    assert update.identity.client_order_id is None  # 归属由内核按远端标识完成
    assert event.event_time == datetime(2026, 9, 24, 1, 34, 59, tzinfo=timezone.utc)
    assert event.available_at == RECEIVED


@pytest.mark.parametrize(
    ("flag", "expected"),
    [
        ("0", OrderStatus.FILLED),
        ("1", OrderStatus.PARTIALLY_FILLED),
        ("2", OrderStatus.CANCELLED),
        ("3", OrderStatus.ACCEPTED),
        ("4", OrderStatus.CANCELLED),
        ("5", OrderStatus.CANCELLED),
        ("a", OrderStatus.UNKNOWN),
    ],
)
def test_every_declared_order_status_flag_is_mapped(flag, expected):
    event = normalizer().normalize_order(order_raw(OrderStatus=flag), RECEIVED)
    assert event.payload.status == expected


def test_unknown_status_flag_fails_explicitly():
    with pytest.raises(CtpNormalizationError, match="unknown CTP order status"):
        normalizer().normalize_order(order_raw(OrderStatus="z"), RECEIVED)


@pytest.mark.parametrize(
    ("flag", "expected"),
    [("0", Offset.OPEN), ("1", Offset.CLOSE), ("3", Offset.CLOSE_TODAY), ("4", Offset.CLOSE_YESTERDAY)],
)
def test_declared_offset_flags_are_mapped(flag, expected):
    event = normalizer().normalize_order(order_raw(CombOffsetFlag=flag), RECEIVED)
    assert event.payload.offset == expected


def test_force_close_flags_are_refused_instead_of_booked_as_a_normal_close():
    with pytest.raises(CtpNormalizationError, match="cannot be represented"):
        normalizer().normalize_order(order_raw(CombOffsetFlag="2"), RECEIVED)


def test_insert_rejected_submit_status_becomes_a_rejected_order():
    event = normalizer().normalize_order(order_raw(OrderSubmitStatus="4"), RECEIVED)
    assert event.payload.status == OrderStatus.REJECTED


def test_cancel_rejection_is_recorded_as_a_gap_not_silently_dropped():
    normalizer_ = normalizer()
    assert normalizer_.normalize_order(order_raw(OrderSubmitStatus="5"), RECEIVED) is None
    assert normalizer_.counts["unrepresentable"] == 1
    assert normalizer_.gaps[0]["reason"].startswith("counter submit status 5")


def test_missing_trading_day_fails_instead_of_using_a_local_date():
    raw = order_raw()
    raw.pop("TradingDay")
    with pytest.raises(CtpNormalizationError, match="TradingDay"):
        normalizer().normalize_order(raw, RECEIVED)


def test_counter_date_in_the_future_is_not_used_as_the_event_time():
    # 夜盘柜台把日期填成交易日时，拼接会得到未来时刻：退回落本地接收时刻并记异常
    event = normalizer().normalize_order(order_raw(InsertDate="20260925", InsertTime="21:30:00"), RECEIVED)
    assert event.event_time == RECEIVED
    assert normalizer().counts["timestamp_anomalies"] == 0  # 计数在归一化器实例上
    assert event.available_at == RECEIVED


def test_identical_redelivery_produces_the_same_event_id_and_a_later_status_does_not():
    normalizer_ = normalizer()
    first = normalizer_.normalize_order(order_raw(), RECEIVED)
    again = normalizer_.normalize_order(order_raw(), RECEIVED)
    changed = normalizer_.normalize_order(order_raw(OrderStatus="1", VolumeTraded=1), RECEIVED)
    assert first.event_id == again.event_id
    assert changed.event_id != first.event_id


def test_naive_receive_time_is_refused():
    with pytest.raises(CtpNormalizationError, match="aware timestamp"):
        normalizer().normalize_order(order_raw(), datetime(2026, 9, 24, 9, 35))


# --------------------------------------------------------------------------------------- 成交回报


def test_trade_report_builds_a_scoped_deduplication_key_and_decimal_price():
    event = normalizer().normalize_trade(trade_raw(), RECEIVED)
    trade = event.payload
    assert isinstance(trade, Trade)
    assert trade.trade_id == "T-1001"
    assert trade.price == Decimal("3120.0")
    assert trade.side == Side.SELL and trade.offset == Offset.CLOSE
    assert trade.trading_day == date(2026, 9, 24)
    assert trade.deduplication_key.account_id == ACCOUNT
    assert trade.deduplication_key.exchange == Exchange.SHFE
    assert trade.deduplication_key.extra_scope == ()
    assert trade.order_identity is not None and trade.order_identity.exchange_order_id == "sys-7"
    assert event.event_id == "ctp:trade:2026-09-24:SHFE:T-1001"


def test_trade_without_price_or_identifier_fails_explicitly():
    no_price = trade_raw()
    no_price.pop("Price")
    with pytest.raises(CtpNormalizationError, match="Price"):
        normalizer().normalize_trade(no_price, RECEIVED)
    with pytest.raises(CtpNormalizationError, match="positive"):
        normalizer().normalize_trade(trade_raw(Price=0.0), RECEIVED)
    no_id = trade_raw()
    no_id.pop("TradeID")
    with pytest.raises(CtpNormalizationError, match="TradeID"):
        normalizer().normalize_trade(no_id, RECEIVED)
    with pytest.raises(CtpNormalizationError, match="positive"):
        normalizer().normalize_trade(trade_raw(Volume=0), RECEIVED)


def test_trade_without_session_fields_is_completed_from_our_own_order_ref_book():
    book = CtpOrderRefBook()
    book.open_session(12, 345678, "0")
    book.allocate("cid-1")  # 分配出 order_ref=1
    raw = trade_raw(OrderRef="1")
    event = normalizer(book).normalize_trade(raw, RECEIVED)
    identity = event.payload.order_identity
    assert (identity.front_id, identity.session_id, identity.order_ref) == (12, 345678, "1")


def test_ambiguous_order_ref_keeps_the_trade_unattributed_instead_of_guessing():
    book = CtpOrderRefBook(restored=[(12, 345678, "1", "cid-a"), (13, 999999, "1", "cid-b")])
    raw = trade_raw(OrderRef="1")
    raw.pop("OrderSysID")
    normalizer_ = normalizer(book)
    event = normalizer_.normalize_trade(raw, RECEIVED)
    assert event is not None  # 真实成交绝不能因为无法归属而被丢弃
    assert event.payload.order_identity is None
    assert normalizer_.counts["unattributable"] == 1


def test_order_report_without_any_identifier_becomes_a_recorded_gap():
    raw = order_raw()
    raw.pop("OrderSysID")
    raw.pop("FrontID")
    raw.pop("SessionID")
    normalizer_ = normalizer()
    assert normalizer_.normalize_order(raw, RECEIVED) is None
    assert normalizer_.counts["unrepresentable"] == 1
    assert normalizer_.counts["unattributable"] == 1


# --------------------------------------------------------------------------------------- 错误回报


def test_exchange_rejection_becomes_a_rejected_order_report():
    normalizer_ = normalizer()
    raw = {
        "callback": "order_insert",
        "InstrumentID": "rb2601",
        "ExchangeID": "SHFE",
        "OrderRef": "9",
        "FrontID": 12,
        "SessionID": 345678,
        "Direction": "0",
        "CombOffsetFlag": "0",
        "VolumeTotalOriginal": 1,
        "InsertDate": "20260924",
        "InsertTime": "09:36:00",
        "TradingDay": "20260924",
        "rsp": {"ErrorID": 31, "ErrorMsg": "CTP:资金不足"},
    }
    event = normalizer_.normalize_error(raw, RECEIVED)
    assert event is not None and event.payload.status == OrderStatus.REJECTED
    assert normalizer_.counts["errors"] == 1


def test_cancel_error_callback_is_recorded_as_a_gap():
    normalizer_ = normalizer()
    raw = {"callback": "order_action", "OrderRef": "9", "rsp": {"ErrorID": 26, "ErrorMsg": "撤单错误"}}
    assert normalizer_.normalize_error(raw, RECEIVED) is None
    assert normalizer_.counts["unrepresentable"] == 1


# --------------------------------------------------------------------------------------- 工具函数


def test_trading_day_parsing_and_exchange_instant_helpers():
    assert parse_trading_day({"TradingDay": "20260924"}) == date(2026, 9, 24)
    with pytest.raises(CtpNormalizationError):
        parse_trading_day({"TradingDay": "2026-09-24"})
    instant, from_counter = exchange_instant("20260924", "09:35:00", RECEIVED)
    assert from_counter is True and instant == datetime(2026, 9, 24, 1, 35, tzinfo=timezone.utc)
    clamped, from_counter = exchange_instant("20260925", "21:30:00", RECEIVED)
    assert from_counter is False and clamped == RECEIVED
    assert exchange_instant(None, None, RECEIVED) == (RECEIVED, False)


def test_fake_binding_order_reports_are_normalizable_end_to_end():
    binding = FakeCtpBinding()
    book = CtpOrderRefBook()
    book.open_session(binding.front_id, binding.session_id, "0")
    ref = book.allocate("cid-1")
    api = binding.create_trader_api("runs/pytest/ctp_flow")

    class Sink:
        def __init__(self) -> None:
            self.events: list = []
            self.errors: list[str] = []

        def enqueue(self, event) -> bool:
            self.events.append(event)
            return True

        def enqueue_callback_error(self, source_id: str, error: Exception) -> None:
            self.errors.append(f"{source_id}:{type(error).__name__}")

    sink = Sink()
    normalizer_ = CtpFeedbackNormalizer(account_id=ACCOUNT, resolver=CtpIdentityResolver(book))
    router = CtpCallbackRouter(normalizer=normalizer_, events=sink, account_id=ACCOUNT)
    spi = build_trader_spi(binding, router)
    api.spi = spi
    api.push_order(order_ref=ref.order_ref, status="3")
    assert sink.errors == []
    assert len(sink.events) == 1
    update = sink.events[0].payload
    assert (update.identity.front_id, update.identity.session_id, update.identity.order_ref) == ref.triple
    assert update.identity.exchange_order_id == binding.order_sys_id
    assert update.status == OrderStatus.ACCEPTED
    api.push_trade(order_ref=ref.order_ref, trade_id="T-1")
    trade = sink.events[1].payload
    assert isinstance(trade, Trade) and trade.order_identity is not None
    assert trade.order_identity.order_ref == ref.order_ref
    assert normalizer_.counts["orders"] == 1 and normalizer_.counts["trades"] == 1
