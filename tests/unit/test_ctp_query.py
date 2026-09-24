"""S5-01 CTP 查询适配器：完成标志、错误码、持仓口径与查询流控."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from qh_trader.core.constants import Exchange, OrderStatus, PositionSide
from qh_trader.core.objects import QueryBatch
from qh_trader.gateway.ctp_gateway import CtpOrderRefBook, CtpSettings, CtpTraderGateway
from qh_trader.gateway.ctp_query import (
    QUERY_LOCAL_REJECT_CODE,
    QUERY_TIMEOUT_CODE,
    QUERY_UNSUPPORTED_CODE,
    CtpQueryAdapter,
)
from qh_trader.gateway.feedback_normalizer import build_normalizer
from tests.unit.fake_ctp import FakeCtpBinding, account_record, position_record

ACCOUNT = "simnow-account"
DAY = date(2026, 9, 24)


class Sink:
    def __init__(self) -> None:
        self.events: list = []
        self.errors: list[str] = []

    def enqueue(self, event) -> bool:
        self.events.append(event)
        return True

    def enqueue_callback_error(self, source_id: str, error: Exception) -> None:
        self.errors.append(f"{source_id}:{type(error).__name__}")


def build(binding: FakeCtpBinding, *, interval_ms: int = 0, timeout_s: float = 2.0, sleeps=None):
    book = CtpOrderRefBook()
    sink = Sink()
    gateway = CtpTraderGateway(
        settings=CtpSettings(
            front_trade="tcp://127.0.0.1:10201",
            broker_id="9999",
            investor_id="231495",
            user_id="231495",
            password="not-a-real-secret",
            flow_dir="runs/pytest/ctp_flow",
            connect_timeout_s=2.0,
            login_timeout_s=2.0,
        ),
        account_id=ACCOUNT,
        events=sink,
        normalizer=build_normalizer(ACCOUNT, book),
        price_tick=lambda instrument: Decimal("1"),
        capability_profile=build_capability(),
        capability_version="test",
        authority=lambda: None,
        binding=binding,
        ref_book=book,
    )
    gateway.connect()
    recorded: list[float] = []

    def sleep(seconds: float) -> None:
        recorded.append(seconds)

    adapter = CtpQueryAdapter(
        account_id=ACCOUNT,
        channel=gateway,
        normalizer=gateway.router.normalizer,
        investor_id="231495",
        broker_id="9999",
        trading_day=lambda: gateway.trading_day,
        interval_ms=interval_ms,
        timeout_s=timeout_s,
        wall_time=lambda: datetime(2026, 9, 24, 1, 35, tzinfo=timezone.utc),
        monotonic=lambda: 100.0,
        sleep=sleep,
    )
    gateway.router.queries = adapter
    return gateway, adapter, recorded


def build_capability():  # noqa: ANN201 - 简化：空能力登记
    from qh_trader.core.objects import CapabilityProfile

    return CapabilityProfile("simnow_v6", None, {})


def batch(adapter: CtpQueryAdapter, kind: str = "account") -> QueryBatch:
    return adapter.query_batch(kind)


def test_account_query_maps_only_reported_fields():
    binding = FakeCtpBinding(query_records={"account": (account_record("123456.78", "90000.5", "10000.25"),)})
    _, adapter, _ = build(binding)
    result = adapter.query_account(batch(adapter))
    assert result.complete is True and result.error_code is None
    funds = result.records[0]
    assert funds.balance == Decimal("123456.78")
    assert funds.available_for_new_trades == Decimal("90000.5")
    assert funds.margin == Decimal("10000.25")
    assert funds.equity is None  # 柜台未给出可直接使用的权益口径：留空而不是自行推导
    assert result.source_id == "ctp"


def test_account_query_without_balance_is_incomplete_rather_than_zero():
    binding = FakeCtpBinding(query_records={"account": (account_record("100000"),)})
    binding.query_records["account"][0].Balance = ""
    _, adapter, _ = build(binding)
    result = adapter.query_account(batch(adapter))
    assert result.complete is False
    assert result.error_code == QUERY_UNSUPPORTED_CODE
    assert result.records == ()
    assert adapter.counts["conversion_failures"] == 1


def test_position_query_aggregates_today_and_yesterday_buckets():
    records = (
        position_record(position_date="1", position=2, frozen=1),
        position_record(position_date="2", position=3),
    )
    binding = FakeCtpBinding(query_records={"position": records})
    _, adapter, _ = build(binding)
    result = adapter.query_positions(batch(adapter, "position"))
    assert result.complete is True
    position = result.records[0]
    assert position.instrument.exchange == Exchange.SHFE
    assert position.side == PositionSide.LONG
    assert (position.pos_td, position.pos_yd) == (2, 3)
    assert position.frozen_td == 1 and position.frozen_yd == 0
    assert position.hedge_flag == "1"


def test_hedged_or_net_positions_make_the_query_incomplete_instead_of_guessing():
    binding = FakeCtpBinding(query_records={"position": (position_record(hedge="3"),)})
    _, adapter, _ = build(binding)
    result = adapter.query_positions(batch(adapter, "position"))
    assert result.complete is False and result.error_code == QUERY_UNSUPPORTED_CODE
    net = FakeCtpBinding(query_records={"position": (position_record(direction="1"),)})
    _, adapter, _ = build(net)
    assert adapter.query_positions(batch(adapter, "position")).complete is False


def test_position_without_position_date_is_incomplete():
    record = position_record()
    record.PositionDate = ""
    binding = FakeCtpBinding(query_records={"position": (record,)})
    _, adapter, _ = build(binding)
    result = adapter.query_positions(batch(adapter, "position"))
    assert result.complete is False and result.error_code == QUERY_UNSUPPORTED_CODE


def test_order_query_returns_only_active_orders_and_trade_query_deduplicates():
    binding = FakeCtpBinding()
    _, adapter, _ = build(binding)
    from tests.unit.fake_ctp import FakeField

    def order(order_ref: str, status: str) -> FakeField:
        return FakeField(
            "CThostFtdcOrderField",
            InstrumentID="rb2601",
            ExchangeID="SHFE",
            OrderRef=order_ref,
            OrderSysID=f"sys-{order_ref}",
            Direction="0",
            CombOffsetFlag="0",
            OrderStatus=status,
            OrderSubmitStatus="3",
            VolumeTotalOriginal=2,
            VolumeTraded=0,
            InsertDate="20260924",
            InsertTime="09:35:00",
            TradingDay="20260924",
        )

    binding.query_records["order"] = (order("1", "3"), order("2", "5"), order("3", "1"))
    result = adapter.query_orders(batch(adapter, "order"))
    assert result.complete is True
    # 查询回报没有会话号，只能用交易所单号归属；没有标识的订单不会被编造本地单号
    assert [(item.identity.exchange_order_id, item.status) for item in result.records] == [
        ("sys-1", OrderStatus.ACCEPTED),
        ("sys-3", OrderStatus.PARTIALLY_FILLED),
    ]

    def trade(trade_id: str, order_ref: str) -> FakeField:
        return FakeField(
            "CThostFtdcTradeField",
            InstrumentID="rb2601",
            ExchangeID="SHFE",
            OrderRef=order_ref,
            OrderSysID=f"sys-{order_ref}",
            TradeID=trade_id,
            Direction="0",
            OffsetFlag="0",
            Price=3000.0,
            Volume=1,
            TradeDate="20260924",
            TradeTime="09:35:01",
            TradingDay="20260924",
        )

    binding.query_records["trade"] = (trade("T-1", "1"), trade("T-1", "1"), trade("T-2", "2"))
    trades = adapter.query_trades(batch(adapter, "trade"))
    assert trades.complete is True
    assert [item.trade_id for item in trades.records] == ["T-1", "T-2"]


def test_query_timeout_and_local_rejection_are_reported_not_raised():
    silent = FakeCtpBinding(query_silent={"account": True})
    _, adapter, _ = build(silent, timeout_s=0.05)
    result = adapter.query_account(batch(adapter))
    assert result.complete is False and result.error_code == QUERY_TIMEOUT_CODE
    assert adapter.counts["timeouts"] == 1

    rejected = FakeCtpBinding(query_codes={"account": -1})
    _, adapter, _ = build(rejected)
    result = adapter.query_account(batch(adapter))
    # 柜台应答里的错误码原样进入结果，不替换成本地码
    assert result.complete is False and result.error_code == -1

    local = FakeCtpBinding(query_return_codes={"account": -1})
    _, adapter, _ = build(local)
    result = adapter.query_account(batch(adapter))
    assert result.complete is False and result.error_code == QUERY_LOCAL_REJECT_CODE
    assert adapter.counts["local_rejections"] == 1


def test_counter_query_error_code_propagates_to_the_result():
    binding = FakeCtpBinding(query_codes={"trade": 42})
    _, adapter, _ = build(binding)
    result = adapter.query_trades(batch(adapter, "trade"))
    assert result.complete is False and result.error_code == 42


def test_query_interval_is_enforced_between_requests():
    binding = FakeCtpBinding()
    _, adapter, sleeps = build(binding, interval_ms=1000)
    adapter.query_account(batch(adapter))
    adapter.query_account(batch(adapter))
    assert adapter.rate_limit().interval_ms == 1000
    assert sleeps and sleeps[0] == pytest.approx(1.0)


def test_instrument_and_depth_queries_expose_the_counter_parameters():
    binding = FakeCtpBinding(
        query_records={
            "account": (account_record(),),
            "instrument": (
                type(
                    "I",
                    (),
                    {
                        "InstrumentID": "rb2601",
                        "ExchangeID": "SHFE",
                        "VolumeMultiple": 10,
                        "PriceTick": 1.0,
                        "ExpireDate": "20270115",
                        "IsTrading": 1,
                    },
                )(),
            ),
            "depth": (type("D", (), {"InstrumentID": "rb2601", "LowerLimitPrice": 2700.0})(),),
        }
    )
    _, adapter, _ = build(binding)
    contract = adapter.query_instrument("rb2601")
    assert contract["VolumeMultiple"] == 10 and contract["ExpireDate"] == "20270115"
    depth = adapter.query_depth("rb2601")
    assert depth["LowerLimitPrice"] == 2700.0
    with pytest.raises(ValueError):
        adapter.query_instrument("")


def test_unmatched_query_responses_are_counted_not_applied():
    binding = FakeCtpBinding()
    _, adapter, _ = build(binding)
    adapter.on_query_response(
        kind="account", request_id=999, records=(account_record(),), is_last=True, error_code=None, error_message=None
    )
    adapter.on_query_error(request_id=998, error_code=1, error_message="late error")
    assert adapter.counts["unmatched_responses"] == 2
