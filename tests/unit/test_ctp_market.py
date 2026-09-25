"""S5-01 行情面：MdApi 登录、订阅、逐笔快照归一化与哨兵值处理（绑定假件驱动）."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from qh_trader.core.constants import EventKind, Exchange, MarketPhase
from qh_trader.core.objects import InstrumentId
from qh_trader.gateway.ctp_market import (
    CTP_SENTINEL,
    CtpMarketDataGateway,
    CtpMarketSettings,
    _count,
    _optional_count,
    _price,
    exchange_instant,
)
from tests.unit.fake_ctp import FakeMdBinding

RB = InstrumentId(Exchange.SHFE, "rb2610")
RU = InstrumentId(Exchange.SHFE, "ru2611")


class Sink:
    def __init__(self) -> None:
        self.events: list = []
        self.errors: list[str] = []

    def enqueue(self, event) -> bool:
        self.events.append(event)
        return True

    def enqueue_callback_error(self, source_id: str, error: Exception) -> None:
        self.errors.append(f"{source_id}:{type(error).__name__}")


def make_settings(**overrides) -> CtpMarketSettings:
    values = {
        "front_market": "tcp://127.0.0.1:40011",
        "broker_id": "9999",
        "user_id": "231495",
        "password": "not-a-real-secret",
        "flow_dir": "runs/pytest/ctp_md_flow",
        "connect_timeout_s": 2.0,
        "login_timeout_s": 2.0,
        "subscribe_timeout_s": 0.5,
    }
    values.update(overrides)
    return CtpMarketSettings(**values)


def make_gateway(binding: FakeMdBinding | None = None, sink: Sink | None = None):
    resolved = binding if binding is not None else FakeMdBinding()
    recorder = sink if sink is not None else Sink()
    gateway = CtpMarketDataGateway(
        settings=make_settings(),
        events=recorder,
        binding=resolved,
        wall_time=lambda: datetime(2026, 9, 25, 2, 30, tzinfo=timezone.utc),
    )
    return gateway, recorder, resolved


# --------------------------------------------------------------------------------------- 握手


def test_market_login_and_subscription_handshake():
    gateway, _, binding = make_gateway()
    report = gateway.connect()
    names = [name for name, _ in binding.api.calls]
    assert names[:4] == ["RegisterSpi", "RegisterFront", "Init", "ReqUserLogin"]
    assert report["logged_in"] is True and report["binding_version"] == "6.7.11-fake"
    assert gateway.ready is True
    assert gateway.subscribe([RB, RU]) == ("rb2610", "ru2611")
    assert binding.api.subscribed == ["rb2610", "ru2611"]
    # SWIG 包装的订阅签名要求 list[bytes] + 数量
    payload, count = binding.api.calls[-1][1]
    assert payload == [b"rb2610", b"ru2611"] and count == 2
    gateway.close()


def test_rejected_login_keeps_the_gateway_unready():
    gateway, _, _ = make_gateway(FakeMdBinding(login_code=3))
    with pytest.raises(Exception, match="market data login failed"):
        gateway.connect()
    assert gateway.ready is False
    assert gateway.fault == "market_login_error_3"


def test_subscription_rejection_leaves_the_symbol_unsubscribed():
    gateway, _, _ = make_gateway(FakeMdBinding(rejected_symbols=("ru2611",)))
    gateway.connect()
    assert gateway.subscribe([RB, RU]) == ("rb2610",)
    assert gateway.subscribed == ("rb2610",)
    assert gateway.counts["subscribe_rejections"] == 1


def test_front_disconnect_closes_readiness():
    gateway, _, binding = make_gateway()
    gateway.connect()
    gateway.subscribe([RB])
    binding.api.front_disconnected()
    assert gateway.ready is False
    assert gateway.counts["front_disconnected"] == 1


# --------------------------------------------------------------------------------------- 快照映射


def test_snapshot_is_normalized_into_a_tick_event():
    gateway, sink, binding = make_gateway()
    gateway.connect()
    gateway.subscribe([RB])
    binding.api.push_tick()
    assert len(sink.events) == 1
    event = sink.events[0]
    tick = event.payload
    assert event.kind == EventKind.MARKET_DATA and event.source_id == "ctp-md"
    assert tick.instrument == RB
    assert tick.meta.trading_day.isoformat() == "2026-09-23"
    # 事件时刻由自然日 ActionDay + 柜台时间 + 毫秒还原，不用交易日拼时刻
    assert event.event_time == datetime(2026, 9, 23, 2, 26, 8, 500000, tzinfo=timezone.utc)
    assert event.available_at == datetime(2026, 9, 25, 2, 30, tzinfo=timezone.utc)
    assert (tick.bid_price, tick.ask_price) == (Decimal("3052.0"), Decimal("3054.0"))
    assert (tick.bid_volume, tick.ask_volume) == (12, 30)
    assert tick.cumulative_volume == 42925  # 柜台以浮点返回，必须精确转成整数
    assert tick.cumulative_turnover == Decimal("1313292470.0")
    assert tick.open_interest == 201290
    assert tick.phase == MarketPhase.UNKNOWN  # 时段判断不在行情层，权限交给会话门禁
    assert (tick.upper_limit_price, tick.lower_limit_price) == (Decimal("3216.0"), Decimal("2909.0"))
    assert gateway.counts["ticks_enqueued"] == 1 and gateway.counts["snapshots"] == 1


def test_sentinel_prices_are_not_read_as_numbers():
    gateway, sink, binding = make_gateway()
    gateway.connect()
    gateway.subscribe([RB])
    # 无有效值：CTP 用 DBL_MAX 表示，不能当价格
    binding.api.push_tick(BidPrice1=CTP_SENTINEL, AskPrice1=CTP_SENTINEL, UpperLimitPrice=CTP_SENTINEL)
    tick = sink.events[-1].payload
    assert tick.bid_price is None and tick.ask_price is None and tick.upper_limit_price is None
    # LastPrice 命中哨兵说明这一笔没有成交价：整笔快照不产生事件，也不读成 0
    before = len(sink.events)
    binding.api.push_tick(LastPrice=CTP_SENTINEL)
    assert len(sink.events) == before
    assert gateway.counts["empty_snapshots"] == 1


def test_missing_counts_fail_instead_of_becoming_zero():
    gateway, sink, binding = make_gateway()
    gateway.connect()
    gateway.subscribe([RB])
    binding.api.push_tick(Volume=None)
    assert sink.events == []
    assert gateway.counts["conversion_failures"] == 1
    assert "market_data_failure" in gateway.evidence[-1]


def test_snapshot_without_exchange_uses_the_subscribed_registration():
    # SimNow 40011 实测的行情快照不带 ExchangeID：交易所只能来自订阅时的合约标识
    binding = FakeMdBinding()
    gateway = CtpMarketDataGateway(
        settings=make_settings(),
        events=Sink(),
        binding=binding,
        wall_time=lambda: datetime(2026, 9, 25, 2, 30, tzinfo=timezone.utc),
    )
    gateway.connect()
    gateway.subscribe([RB])
    binding.api.push_tick(ExchangeID="")
    assert gateway.counts["ticks_enqueued"] == 1
    assert gateway.counts["conversion_failures"] == 0


def test_unsubscribed_instrument_is_rejected_with_evidence():
    gateway, sink, binding = make_gateway()
    gateway.connect()
    gateway.subscribe([RB])
    binding.api.push_tick(ExchangeID="", InstrumentID="ag2612")
    assert sink.events == []
    assert gateway.counts["conversion_failures"] == 1
    assert "not subscribed" in str(gateway.evidence[-1]["reason"])


def test_exchange_mismatch_is_refused():
    gateway, sink, binding = make_gateway()
    gateway.connect()
    gateway.subscribe([RB])
    binding.api.push_tick(ExchangeID="DCE")
    assert sink.events == []
    assert gateway.counts["conversion_failures"] == 1


def test_market_data_failures_do_not_touch_the_trading_gate():
    # 行情转换失败只记数据质量缺口：不能通过 enqueue_callback_error 关闭交易门禁
    gateway, sink, binding = make_gateway()
    gateway.connect()
    gateway.subscribe([RB])
    binding.api.push_tick(Volume=42.5)
    assert sink.errors == []
    assert gateway.counts["conversion_failures"] == 1


def test_status_is_redacted_and_reports_subscriptions():
    gateway, _, _ = make_gateway()
    gateway.connect()
    gateway.subscribe([RB])
    status = gateway.status()
    assert "not-a-real-secret" not in str(status)
    assert status["subscribed"] == ["rb2610"]
    assert status["counts"]["logins"] == 1


# --------------------------------------------------------------------------------------- 工具


def test_price_and_count_helpers_reject_sentinels_and_fractions():
    assert _price({"LastPrice": 3053.5}, "LastPrice") == Decimal("3053.5")
    assert _price({"LastPrice": CTP_SENTINEL}, "LastPrice") is None
    assert _price({"LastPrice": 0}, "LastPrice") is None
    assert _price({"LastPrice": -1}, "LastPrice") is None
    assert _count({"Volume": 12.0}, "Volume") == 12
    assert _optional_count(None) is None
    with pytest.raises(ValueError, match="integral"):
        _count({"Volume": 1.5}, "Volume")


def test_exchange_instant_clamps_implausible_clock_and_keeps_milliseconds():
    received = datetime(2026, 9, 25, 2, 30, tzinfo=timezone.utc)
    instant, from_counter = exchange_instant("20260925", "10:26:08", received, update_millisec=250)
    assert from_counter is True and instant == datetime(2026, 9, 25, 2, 26, 8, 250000, tzinfo=timezone.utc)
    clamped, from_counter = exchange_instant("20260926", "10:26:08", received)
    assert from_counter is False and clamped == received
    assert exchange_instant(None, None, received) == (received, False)
    # 少数柜台返回带小数秒的时刻
    instant, from_counter = exchange_instant("20260925", "10:26:08.700", received)
    assert from_counter is True and instant.microsecond == 0


def test_market_settings_validate_front_and_field_lengths():
    with pytest.raises(ValueError, match="tcp://"):
        make_settings(front_market="182.254.243.31:40011")
    with pytest.raises(ValueError, match="user_id"):
        make_settings(user_id="x" * 16)
    with pytest.raises(ValueError, match="password"):
        make_settings(password="")


def test_market_settings_can_be_derived_from_the_trading_settings():
    from qh_trader.gateway.ctp_gateway import CtpSettings

    trading = CtpSettings(
        front_trade="tcp://127.0.0.1:40001",
        broker_id="9999",
        investor_id="231495",
        user_id="231495",
        password="not-a-real-secret",
        flow_dir="runs/live/ctp_flow",
    )
    derived = CtpMarketSettings.from_settings(trading, front_market="tcp://127.0.0.1:40011")
    assert derived.broker_id == "9999" and derived.user_id == "231495"
    assert derived.flow_dir.endswith("-md")
    assert derived.password == trading.password
    assert isinstance(derived.connect_timeout_s, float)
    assert timedelta(seconds=derived.login_timeout_s) > timedelta(0)
