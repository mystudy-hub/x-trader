"""只读预检只选择柜台实际合约，并保留失败、脱敏和无交易副作用证据。"""

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import yaml

from qh_trader.core.constants import EventKind, Exchange, MarketPhase
from qh_trader.core.objects import InstrumentId, RecordMeta, Tick
from qh_trader.data.contracts import ContractResolver
from scripts import simnow_preflight as preflight
from tests.unit.fake_ctp import FakeCtpBinding, FakeField, account_record

DAY = date(2026, 9, 29)
NOW = datetime(2026, 9, 28, 13, 35, tzinfo=timezone.utc)
SECRET = "test-preflight-secret-not-for-output"


def contract(symbol="rb2610", **overrides):
    values = {
        "InstrumentID": symbol,
        "ExchangeID": "SHFE",
        "ProductID": "rb",
        "ProductClass": "1",
        "VolumeMultiple": 10,
        "PriceTick": 1.0,
        "OpenDate": "20251016",
        "ExpireDate": "20261015",
        "IsTrading": 1,
        "InstLifePhase": "1",
        "DeliveryYear": 2026,
        "DeliveryMonth": 10,
    }
    values.update(overrides)
    return values


def make_tick(symbol="rb2610", *, oi=100, volume=20, day=DAY, at=NOW):
    return Tick(
        instrument=InstrumentId(Exchange.SHFE, symbol),
        meta=RecordMeta(
            event_time=at,
            available_at=at,
            ingested_at=at,
            trading_day=day,
            source_id="test",
            source_version="1",
            ingest_seq=1,
        ),
        last_price=Decimal("3000"),
        bid_price=Decimal("2999"),
        ask_price=Decimal("3001"),
        bid_volume=1,
        ask_volume=1,
        cumulative_volume=volume,
        cumulative_turnover=Decimal("600000"),
        open_interest=oi,
        pre_settlement_price=None,
        upper_limit_price=None,
        lower_limit_price=None,
        phase=MarketPhase.UNKNOWN,
    )


def rate_record(kind="commission", symbol="rb2610", **overrides):
    values = {name: 0.0001 for name in preflight.RATE_FIELDS[kind] if "Ratio" in name}
    values.update(
        {"InstrumentID": symbol, "ExchangeID": "SHFE", "HedgeFlag": "1", "InvestorID": "private-id", "Password": SECRET}
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_fronts_reject_non_simnow_or_unregistered_or_mismatched_pair():
    profile = preflight.ctp_setup.load_broker_profile("simnow_v6")
    assert preflight.registered_front_pair(profile, "tcp://182.254.243.31:30001", None) == (
        "tcp://182.254.243.31:30001",
        "tcp://182.254.243.31:30011",
    )
    for trade, market in [
        ("tcp://127.0.0.1:30001", None),
        ("tcp://180.168.146.187:10201", None),
        ("tcp://182.254.243.31:30001", "tcp://182.254.243.31:40011"),
    ]:
        with pytest.raises(ValueError):
            preflight.registered_front_pair(profile, trade, market)
    with pytest.raises(ValueError, match="simnow_v6"):
        preflight.registered_front_pair({**profile, "profile_name": "live"}, None, None)


def test_catalog_does_not_guess_continuous_symbols_expired_or_option_contracts():
    records = [
        contract(),
        contract("rb0000"),
        contract("rb2610P3000", ProductClass="2"),
        contract("rb2609", ExpireDate="20260915"),
        contract("rb2611", IsTrading=0),
        contract("rb2612", PriceTick=0),
        contract("rb2701", ProductID="wrong"),
    ]
    # 0000 还需要真实柜台交割月份与代码一致，不能接受人为构造的连续代号。
    records[1]["DeliveryYear"], records[1]["DeliveryMonth"] = 0, 0
    assert [row["InstrumentID"] for row in preflight.rb_candidates(records, DAY)] == ["rb2610"]


def test_selection_uses_fresh_matching_trading_day_and_oi_then_volume():
    candidates = [contract("rb2610"), contract("rb2611"), contract("rb2612"), contract("rb2701")]
    ticks = [
        make_tick("rb2610", oi=100, volume=500),
        make_tick("rb2611", oi=101, volume=1),
        make_tick("rb2612", oi=9999, at=NOW - timedelta(minutes=1)),
        make_tick("rb2701", oi=9999, day=DAY - timedelta(days=1)),
    ]
    selected, ranking = preflight.select_active(candidates, ticks, DAY, NOW)
    assert selected == "SHFE.rb2611"
    assert len(ranking) == 2
    ticks.append(make_tick("rb2610", oi=101, volume=10, at=NOW + timedelta(seconds=1)))
    assert preflight.select_active(candidates, ticks, DAY, NOW + timedelta(seconds=1))[0] == "SHFE.rb2610"
    assert preflight.select_active(candidates, [], DAY, NOW) == (None, [])


def test_generated_catalog_is_usable_by_existing_resolver(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(preflight.build_catalog([contract()], NOW)), encoding="utf-8")
    spec = ContractResolver.from_file(path).get_spec("SHFE.rb2610", as_of=DAY)
    assert spec.multiplier == Decimal("10") and spec.price_tick == Decimal("1")
    assert spec.last_trading_day == date(2026, 10, 15)


@pytest.mark.parametrize("kind", ["commission", "margin"])
def test_rate_callback_queries_are_scoped_and_redacted(kind):
    rates = preflight.RawRateQueries()

    class Channel:
        def next_request_id(self):
            return 7

        def new_field(self, field_name):
            return SimpleNamespace()

        def send_request(self, name, field, request_id):
            assert name.startswith("ReqQry")
            rates.on_response(kind, rate_record(kind), None, request_id + 1, True)
            rates.on_response(kind, rate_record(kind), None, request_id, True)
            return 0

    settings = SimpleNamespace(broker_id="9999", investor_id="private-id")
    report = rates.query(Channel(), settings, kind, "rb2610", timeout=0.01, interval_ms=0)
    assert report["complete"] and len(report["records"]) == 1
    encoded = json.dumps(report)
    assert SECRET not in encoded and "private-id" not in encoded


@pytest.mark.parametrize("kind", ["commission", "margin"])
@pytest.mark.parametrize(
    "symbol,error,complete", [("rb", None, True), ("cu2610", None, False), ("rb2610", (3, "counter rejected"), False)]
)
def test_shared_rate_query_preserves_scope_errors_and_redaction(kind, symbol, error, complete):
    class Queries:
        def query_batch(self, query_kind):
            return query_kind

        def _collect(self, query_kind, batch, extra):
            assert query_kind == ("commission_rate" if kind == "commission" else "margin_rate")
            assert extra["InstrumentID"] == "rb2610"
            if kind == "margin":
                assert extra["HedgeFlag"] == "1"
            return [{"InstrumentID": symbol, "InvestorID": SECRET}], error

    report = preflight.query_counter_rate(Queries(), kind, "rb2610")
    assert report["complete"] is complete
    assert SECRET not in json.dumps(report)


@pytest.mark.parametrize("mode", ["timeout", "wrong_symbol", "counter_error", "local_error"])
def test_incomplete_or_mismatched_rate_query_never_passes(mode):
    rates = preflight.RawRateQueries()

    class Channel:
        def next_request_id(self):
            return 1

        def new_field(self, name):
            return SimpleNamespace()

        def send_request(self, name, field, request_id):
            if mode == "local_error":
                return -3
            record = rate_record(symbol="cu2610" if mode == "wrong_symbol" else "rb2610")
            rates.on_response(
                "commission",
                record,
                SimpleNamespace(ErrorID=3 if mode == "counter_error" else 0),
                request_id,
                mode != "timeout",
            )
            return 0

    report = rates.query(
        Channel(),
        SimpleNamespace(broker_id="9999", investor_id="id"),
        "commission",
        "rb2610",
        timeout=0.001,
        interval_ms=0,
    )
    assert not report["complete"]


def test_missing_history_is_reported_without_manufacturing_warmup(tmp_path):
    result = preflight.history_capability(tmp_path, "SHFE.rb2610", NOW)
    assert result["has_200_bars"] is False
    assert result["suitability"] == "unavailable"


def test_whole_preflight_is_read_only_closes_connections_and_writes_no_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "ROOT", tmp_path)
    profile = preflight.ctp_setup.load_broker_profile("simnow_v6")
    monkeypatch.setattr(preflight.ctp_setup, "load_broker_profile", lambda _: profile)
    monkeypatch.setenv("QH_CTP_PASSWORD", SECRET)
    config = {"broker": {"profile": "simnow_v6", "user_id": "private-id", "investor_id": "private-id"}}
    (tmp_path / "settings.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    binding = FakeCtpBinding(
        trading_day=DAY.strftime("%Y%m%d"),
        query_records={
            "account": (account_record("100000"),),
            "instrument": (FakeField(**contract()),),
        },
    )
    monkeypatch.setattr(preflight, "load_ctp_binding", lambda: binding)
    original_create = binding.create_trader_api

    def create(flow_dir):
        api = original_create(flow_dir)

        def commission(field, request_id):
            api.spi.OnRspQryInstrumentCommissionRate(rate_record(), None, request_id, True)
            return 0

        def margin(field, request_id):
            api.spi.OnRspQryInstrumentMarginRate(rate_record("margin"), None, request_id, True)
            return 0

        api.ReqQryInstrumentCommissionRate = commission
        api.ReqQryInstrumentMarginRate = margin
        return api

    monkeypatch.setattr(binding, "create_trader_api", create)
    made = []

    class Market:
        def __init__(self, **kwargs):
            self.sink = kwargs["events"]
            self.counts = {"ticks_enqueued": 1}
            self.closed = False
            made.append(self)

        def connect(self):
            return {}

        def subscribe(self, instruments, timeout_s):
            self.sink.events.append(
                SimpleNamespace(kind=EventKind.MARKET_DATA, payload=make_tick(at=datetime.now(timezone.utc)))
            )
            return [item.symbol for item in instruments]

        def close(self):
            self.closed = True

    monkeypatch.setattr(preflight, "CtpMarketDataGateway", Market)
    code = preflight.main(
        [
            "--config",
            "settings.yaml",
            "--expected-trading-day",
            DAY.isoformat(),
            "--query-interval-ms",
            "0",
            "--market-seconds",
            "0.001",
            "--out",
            "runs/test",
        ]
    )
    report_text = (tmp_path / "runs/test/preflight.json").read_text(encoding="utf-8")
    report = json.loads(report_text)
    assert code == 0, report
    assert report["rates"]["commission"]["complete"] and report["rates"]["margin"]["complete"]
    assert report["query_diagnostics"]["counts"]["unmatched_responses"] == 0
    assert report["read_only_checks_passed"] is True
    assert report["ready_for_trading"] is False
    assert report["runtime_inputs"]["symbol"] == "SHFE.rb2610"
    assert SECRET not in report_text and "private-id" not in report_text
    assert binding.api.insert_fields == [] and binding.api.action_fields == []
    assert made[0].closed
    assert (tmp_path / "runs/test/contract_catalog.json").is_file()


def test_no_output_outside_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="within runs"):
        preflight.main(["--out", "outside"])


@pytest.mark.parametrize("error", [(preflight.QUERY_TIMEOUT_CODE, SECRET), (90, SECRET)])
def test_catalog_failure_retains_fixed_diagnostics_without_counter_message(error):
    class Queries:
        _timeout_s = 15

        def query_batch(self, kind):
            return kind

        def _collect(self, kind, batch, fields):
            assert fields == {"ExchangeID": "SHFE", "ProductID": "rb"}
            assert self._timeout_s == 45
            return [contract()], error

    queries = Queries()
    rows, diagnostics = preflight.query_rb_catalog(queries, timeout=45)
    assert rows == ()
    assert diagnostics["complete"] is False
    assert diagnostics["requests"][0]["error_code"] == error[0]
    assert diagnostics["requests"][0]["received_records"] == 1
    assert SECRET not in json.dumps(diagnostics)
    assert queries._timeout_s == 15


def test_explicit_candidate_query_does_not_accept_unrelated_response():
    class Queries:
        _timeout_s = 15

        def query_batch(self, kind):
            return kind

        def _collect(self, kind, batch, fields):
            assert fields["InstrumentID"] == "rb2701"
            return [contract("rb2610")], None

    rows, diagnostics = preflight.query_rb_catalog(Queries(), timeout=45, symbols=["SHFE.rb2701"])
    assert rows == ()
    assert diagnostics["requests"][0]["reason"] == "candidate_response_mismatch"
