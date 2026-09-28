"""S5-01 / S0-02 联调工具：柜台口径比对、费率查询、实盘合约目录与持仓探测.

这些用例用假件与本地临时目录覆盖纯逻辑：真实柜台的结论属于 `runs/` 下的联调证据（不入库）。
"""

from __future__ import annotations

import types

import pytest

from qh_trader.core.constants import EventKind, Exchange, Offset, OrderStatus, SendState
from qh_trader.core.objects import (
    Capability,
    CapabilityProfile,
    ControlEpoch,
    InstrumentId,
    LocalSendResult,
    OrderIdentity,
    OrderIntent,
)
from qh_trader.gateway.ctp_gateway import CtpOrderRef
from scripts import build_live_contract_catalog as live_catalog
from scripts import ctp_position_probe as position_probe
from scripts import ctp_setup

RB = InstrumentId(Exchange.SHFE, "rb2610")
EPOCH = ControlEpoch("unit-test", 1)


# --------------------------------------------------------------------------------------- 口径比对


def test_a_counter_product_that_reports_zero_is_not_a_mismatch():
    """实测：openctp TTS 的 ReqQryProduct 把乘数与最小变动恒返 0；这不能算“登记不一致”."""
    comparison = ctp_setup.compare_products(
        [{"ProductID": "RB", "ExchangeID": "SHFE", "VolumeMultiple": 0, "PriceTick": 0.0}]
    )
    item = next(entry for entry in comparison["compared"] if entry["product"] == "SHFE.RB")
    assert item["value_state"] == "柜台未给出该口径" and item["matches"] is False
    assert comparison["mismatches"] == []
    assert len(comparison["counter_absent"]) == 1
    assert "ReqQryInstrument" in comparison["result"]


def test_a_counter_product_with_a_different_non_zero_value_is_a_mismatch():
    comparison = ctp_setup.compare_products(
        [{"ProductID": "RB", "ExchangeID": "SHFE", "VolumeMultiple": 5, "PriceTick": 1.0}]
    )
    assert [entry["product"] for entry in comparison["mismatches"]] == ["SHFE.RB"]
    assert comparison["result"].startswith("柜台品种级口径与本地登记不一致")


def test_instrument_level_comparison_uses_listed_contracts_only():
    instruments = [
        {
            "InstrumentID": "rb2610",
            "ProductID": "RB",
            "ExchangeID": "SHFE",
            "VolumeMultiple": 10,
            "PriceTick": 1.0,
            "IsTrading": "1",
        },
        {
            "InstrumentID": "rb2609",
            "ProductID": "RB",
            "ExchangeID": "SHFE",
            "VolumeMultiple": 5,
            "PriceTick": 1.0,
            "IsTrading": "0",
        },
    ]
    comparison = ctp_setup.compare_instruments(instruments)
    item = next(entry for entry in comparison["compared"] if entry["product"] == "SHFE.RB")
    assert item["is_trading_filter_applied"] is True
    assert item["counter_contracts"] == 1 and item["value_state"] == "一致"
    assert comparison["mismatches"] == []


def test_instrument_level_comparison_reports_a_listed_contract_that_disagrees():
    instruments = [
        {
            "InstrumentID": "rb2610",
            "ProductID": "RB",
            "ExchangeID": "SHFE",
            "VolumeMultiple": 10,
            "PriceTick": 1.0,
            "IsTrading": "1",
        },
        {
            "InstrumentID": "rb2701",
            "ProductID": "RB",
            "ExchangeID": "SHFE",
            "VolumeMultiple": 10,
            "PriceTick": 2.0,
            "IsTrading": "1",
        },
    ]
    comparison = ctp_setup.compare_instruments(instruments)
    item = next(entry for entry in comparison["compared"] if entry["product"] == "SHFE.RB")
    assert item["value_state"] == "不一致"
    assert item["counter_contracts_disagreeing"] == ["rb2701"]


def test_rates_compare_per_lot_commission_and_margin_and_keep_states_separate():
    per_lot = ctp_setup.compare_contract_rates(
        [
            {
                "instrument": "SHFE.rb2610",
                "product": "rb",
                "commission": {"OpenRatioByVolume": 1.5},
                "margin": {"LongMarginRatioByMoney": 0.10, "ShortMarginRatioByMoney": 0.10},
            }
        ]
    )["compared"][0]
    assert per_lot["value_state"] == "一致" and per_lot["margin_direction_conflict"] is False

    by_money = ctp_setup.compare_contract_rates(
        [
            {
                "instrument": "SHFE.rb2610",
                "product": "rb",
                "commission": {"OpenRatioByMoney": 0.000101},
                "margin": {"LongMarginRatioByMoney": 0.10, "ShortMarginRatioByMoney": 0.10},
            }
        ]
    )
    item = by_money["compared"][0]
    # 柜台按金额收费：与“每手固定费用”的登记不可比，但也不能降级成“柜台未给出”
    assert item["value_state"] == "口径不同" and item["commission_state"].startswith("按金额收费")
    assert by_money["mismatches"] == [] and by_money["state_counts"] == {"口径不同": 1}


def test_rates_report_a_margin_difference_and_an_empty_counter_answer():
    mismatch = ctp_setup.compare_contract_rates(
        [
            {
                "instrument": "SHFE.au2610",
                "product": "au",
                "commission": {"OpenRatioByVolume": 10},
                "margin": {"LongMarginRatioByMoney": 0.16, "ShortMarginRatioByMoney": 0.16},
            }
        ]
    )
    item = mismatch["compared"][0]
    assert item["value_state"] == "不一致" and mismatch["state_counts"] == {"不一致": 1}

    empty = ctp_setup.compare_contract_rates(
        [{"instrument": "SHFE.rb2610", "product": "rb", "commission": None, "margin": None}]
    )
    assert empty["mismatches"] == [] and len(empty["counter_absent"]) == 1


# --------------------------------------------------------------------------------------- 实盘合约目录


def test_live_catalog_entry_comes_from_the_counter_instrument_record():
    entry = live_catalog.entry_from_instrument(
        "SHFE.rb2610",
        {
            "InstrumentID": "rb2610",
            "ExchangeID": "SHFE",
            "ProductID": "rb",
            "VolumeMultiple": 10,
            "PriceTick": 1.0,
            "OpenDate": "20251016",
            "ExpireDate": "20261015",
            "IsTrading": "1",
        },
        available_at="2026-09-28T06:46:10+00:00",
    )
    assert entry["delivery_year"] == 2026 and entry["delivery_month"] == 10
    assert entry["listed_on"] == "2025-10-16" and entry["last_trading_day"] == "2026-10-15"
    assert entry["source_id"] == live_catalog.SOURCE_ID and entry["multiplier"] == "10"


def test_live_catalog_fails_instead_of_guessing_missing_counter_fields():
    with pytest.raises(ctp_setup.BrokerProfileError, match="no usable multiplier"):
        live_catalog.entry_from_instrument(
            "SHFE.rb2610",
            {
                "InstrumentID": "rb2610",
                "ExchangeID": "SHFE",
                "ProductID": "rb",
                "OpenDate": "20251016",
                "ExpireDate": "20261015",
            },
            available_at="2026-09-28T06:46:10+00:00",
        )
    with pytest.raises(ctp_setup.BrokerProfileError, match="ExpireDate"):
        live_catalog.entry_from_instrument(
            "SHFE.rb2610",
            {
                "InstrumentID": "rb2610",
                "ExchangeID": "SHFE",
                "ProductID": "rb",
                "VolumeMultiple": 10,
                "PriceTick": 1.0,
                "OpenDate": "20251016",
            },
            available_at="2026-09-28T06:46:10+00:00",
        )
    with pytest.raises(ctp_setup.BrokerProfileError, match="different instrument"):
        live_catalog.entry_from_instrument(
            "SHFE.rb2610",
            {
                "InstrumentID": "rb2701",
                "ExchangeID": "SHFE",
                "ProductID": "rb",
                "VolumeMultiple": 10,
                "PriceTick": 1.0,
                "OpenDate": "20251016",
                "ExpireDate": "20261015",
            },
            available_at="2026-09-28T06:46:10+00:00",
        )


def test_live_catalog_delivery_comes_from_the_counter_expiry_not_from_the_code():
    """3 位代码（郑商所 AP610）只有一位年数字，交割年月只认柜台到期日，并用代码月份交叉核对."""
    assert live_catalog._delivery("AP610", "2026-10-15") == (2026, 10)
    assert live_catalog._delivery("rb2610", "2026-10-15") == (2026, 10)
    assert live_catalog._delivery("TTS.TEST", "2026-10-15") == (2026, 10)
    with pytest.raises(ctp_setup.BrokerProfileError, match="disagree on the delivery month"):
        live_catalog._delivery("rb2610", "2026-11-16")


# --------------------------------------------------------------------------------------- 持仓 / 开平探测


def test_probe_declares_its_probe_only_capability_and_offset_candidates():
    base = CapabilityProfile("openctp_tts", "6.7.11", {"order_types.limit_order": Capability(True, True, "reg")})
    probed = position_probe.probe_capability_profile(base)
    assert probed.values["order_types.market_order"].verified is True
    assert probed.values["order_types.market_order"].evidence_ref == position_probe.PROBE_CAPABILITY_EVIDENCE
    # 原档案不被就地修改
    assert "order_types.market_order" not in base.values
    mapping = position_probe.probe_offset_mapping(Exchange.SHFE)
    assert mapping.verified is True and mapping.evidence_ref == position_probe.PROBE_CAPABILITY_EVIDENCE
    assert {str(offset) for offset in mapping.flags} == {"OPEN", "CLOSE_TODAY", "CLOSE", "CLOSE_YESTERDAY"}
    assert [flag for offset, flag, _ in position_probe.CANDIDATE_OFFSETS] == ["3", "1", "4"]


def test_open_position_and_filled_volume_read_the_kernel_objects():
    rows = [{"pos_yd": 1, "pos_td": 2}, {"pos_yd": 0, "pos_td": 0}]
    assert position_probe._open_position(rows) == 3

    sink = types.SimpleNamespace(
        events=[
            types.SimpleNamespace(kind=EventKind.MARKET_DATA, payload=None),
            types.SimpleNamespace(
                kind=EventKind.TRADE_REPORT, payload=types.SimpleNamespace(instrument=str(RB), quantity=1)
            ),
            types.SimpleNamespace(
                kind=EventKind.TRADE_REPORT,
                payload=types.SimpleNamespace(instrument="SHFE.cu2610", quantity=5),
            ),
        ]
    )
    assert position_probe._filled_volume(sink, RB, 0) == 1


def test_close_candidates_are_tried_in_order_until_the_position_is_flat():
    instrument = RB
    positions = {"open": 1}
    sink = _FakeSink()
    gateway = _FakeGateway(sink, positions, rejected_offsets={Offset.CLOSE})
    queries = _FakeQueries(positions)

    # 先试“平仓”（本假件会拒），再试“平今”（本假件会成交）：验证探测按给定顺序逐个试并停在已平处
    attempts, confirm, rows, flat = position_probe._close_until_flat(
        instrument=instrument,
        quantity=1,
        account_id="unit-test",
        controller_id="unit-test",
        epoch=EPOCH,
        gateway=gateway,  # type: ignore[arg-type]
        queries=queries,  # type: ignore[arg-type]
        sink=sink,
        sell_ticks=3044,
        wait_s=1.0,
        order=[position_probe.CANDIDATE_OFFSETS[1], position_probe.CANDIDATE_OFFSETS[0]],
        label="unit",
    )
    assert [attempt["offset"] for attempt in attempts] == ["CLOSE", "CLOSE_TODAY"]
    assert attempts[0]["order"]["statuses"][-1] == str(OrderStatus.REJECTED)
    assert confirm is not None and confirm["flag"] == "3"
    assert flat is True and position_probe._open_position(rows) == 0


def test_a_close_that_gets_no_counter_report_is_not_treated_as_flat():
    instrument = RB
    positions = {"open": 1}
    sink = _FakeSink()
    gateway = _FakeGateway(sink, positions, silent_offsets={Offset.CLOSE, Offset.CLOSE_TODAY, Offset.CLOSE_YESTERDAY})
    queries = _FakeQueries(positions)

    attempts, confirm, _, flat = position_probe._close_until_flat(
        instrument=instrument,
        quantity=1,
        account_id="unit-test",
        controller_id="unit-test",
        epoch=EPOCH,
        gateway=gateway,  # type: ignore[arg-type]
        queries=queries,  # type: ignore[arg-type]
        sink=sink,
        sell_ticks=3044,
        wait_s=0.6,
        order=position_probe.CANDIDATE_OFFSETS,
        label="unit-silent",
    )
    assert confirm is None and flat is False
    assert all(attempt["order"]["result"] == "no terminal status" for attempt in attempts)
    assert len(attempts) == 3


class _FakeSink:
    """按 OrderRef 存放状态序列：等价于探测里的 RecordingSink."""

    def __init__(self) -> None:
        self.events: list[object] = []
        self.statuses: dict[str, list[str]] = {}

    def order_updates(self, ref: CtpOrderRef) -> list[str]:
        return list(self.statuses.get(ref.order_ref, []))


class _FakeGateway:
    """按开平类型决定柜台结果：被拒的返回 REJECTED，沉默的什么都不回，其余成交并减少仓位."""

    def __init__(
        self,
        sink: _FakeSink,
        positions: dict[str, int],
        *,
        rejected_offsets: set[Offset] | None = None,
        silent_offsets: set[Offset] | None = None,
    ) -> None:
        self.sink = sink
        self.positions = positions
        self.rejected = rejected_offsets or set()
        self.silent = silent_offsets or set()
        self.counter_rejections: list[dict[str, object]] = []
        self.submitted: list[OrderIntent] = []
        self._next = 1

    def submit(self, intent: OrderIntent, epoch: ControlEpoch) -> LocalSendResult:
        self.submitted.append(intent)
        ref = CtpOrderRef(1, 1, str(self._next))
        self._next += 1
        if intent.offset in self.silent:
            self.sink.statuses[ref.order_ref] = []
        elif intent.offset in self.rejected:
            self.sink.statuses[ref.order_ref] = ["UNKNOWN", str(OrderStatus.REJECTED)]
            self.counter_rejections.append({"kind": "order_insert", "error_code": 1009, "order_ref": ref.order_ref})
        else:
            self.sink.statuses[ref.order_ref] = ["UNKNOWN", str(OrderStatus.ACCEPTED), str(OrderStatus.FILLED)]
            if intent.offset == Offset.OPEN:
                self.positions["open"] += intent.quantity
            else:
                self.positions["open"] -= intent.quantity
        identity = OrderIdentity(
            account_id=intent.account_id,
            exchange=intent.instrument.exchange,
            client_order_id=intent.client_order_id,
            front_id=1,
            session_id=1,
            order_ref=ref.order_ref,
        )
        return LocalSendResult(SendState.SENT_UNKNOWN, 0, "fake send", identity)


class _FakeQueries:
    """只提供探测需要的那两个查询入口；持仓由假网关按成交增减."""

    def __init__(self, positions: dict[str, int]) -> None:
        self.positions = positions

    def query_batch(self, kind: str) -> object:
        return types.SimpleNamespace(kind=kind)

    def query_positions(self, batch: object) -> object:
        rows = []
        if self.positions.get("open"):
            rows.append(
                types.SimpleNamespace(
                    instrument=RB,
                    side=types.SimpleNamespace(__str__=lambda self: "LONG"),
                    pos_yd=0,
                    pos_td=self.positions["open"],
                    frozen_yd=0,
                    frozen_td=0,
                )
            )
        return types.SimpleNamespace(records=rows, complete=True)
