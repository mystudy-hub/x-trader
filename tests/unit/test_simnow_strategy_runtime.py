"""SimNow 运行入口默认只观察，缺少核验资料的 trade 在连接前阻断。"""

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import yaml

from qh_trader.core.constants import EventKind, Exchange, Offset, OrderType, PositionSide, Side
from qh_trader.core.event import CanonicalEvent
from qh_trader.core.execution import CommandStatus
from qh_trader.core.objects import Bar, InstrumentId, OrderIntent, RecordMeta
from qh_trader.domain.orders import OrderManager
from qh_trader.domain.positions import PositionDetail
from qh_trader.engine.live_engine import StrategyCheckpoint
from qh_trader.infrastructure import journal_codec
from qh_trader.infrastructure.strategy_checkpoint import SQLiteStrategyCheckpointStore
from scripts import run_simnow_strategy as runtime
from scripts.simnow_preflight import build_catalog
from tests.unit.fake_ctp import FakeCtpBinding, FakeField, account_record
from tests.unit.test_live_bars import START as SESSION_START
from tests.unit.test_live_bars import session
from tests.unit.test_simnow_preflight import contract, make_tick

INST = InstrumentId(Exchange.SHFE, "rb2610")
DAY = date(2026, 9, 29)


def runtime_files(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    config = yaml.safe_load((runtime.ROOT / "config/strategy_validation.yaml").read_text(encoding="utf-8"))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    catalog = build_catalog([contract()], now)
    (tmp_path / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    (tmp_path / "settings.yaml").write_text(
        yaml.safe_dump(
            {
                "broker": {"profile": "simnow_v6", "user_id": "231495", "investor_id": "231495"},
                "risk": {"account_id": "test-runtime"},
            }
        ),
        encoding="utf-8",
    )
    preflight = {
        "kind": "simnow_read_only_preflight",
        "profile": "simnow_v6",
        "generated_at": now.isoformat(),
        "counter_trading_day": str(DAY),
        "selection": {"instrument": str(INST)},
        "front_trade": "tcp://182.254.243.31:30001",
        "front_market": "tcp://182.254.243.31:30011",
        "runtime_inputs": {
            "symbol": str(INST),
            "trading_day": str(DAY),
            "catalog_path": "catalog.json",
            "front_trade": "tcp://182.254.243.31:30001",
            "front_market": "tcp://182.254.243.31:30011",
        },
        "funds": [{"balance": "100000", "margin": "0", "available_for_new_trades": "100000"}],
        "positions": [],
        "active_orders": [],
        "read_only_checks_passed": True,
        "user": runtime.mask_identifier("231495"),
        "investor": runtime.mask_identifier("231495"),
        "rates": {
            "commission": {
                "complete": True,
                "records": [
                    {
                        "InstrumentID": "rb2610",
                        "ExchangeID": "SHFE",
                        "OpenRatioByMoney": "0.0001",
                        "CloseRatioByMoney": "0.0001",
                        "CloseTodayRatioByMoney": "0.0001",
                        "OpenRatioByVolume": "0",
                        "CloseRatioByVolume": "0",
                        "CloseTodayRatioByVolume": "0",
                    }
                ],
            },
            "margin": {"complete": False, "records": []},
        },
    }
    (tmp_path / "preflight.json").write_text(json.dumps(preflight), encoding="utf-8")
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    return [
        "--preflight",
        "preflight.json",
        "--config",
        "config.yaml",
        "--settings",
        "settings.yaml",
        "--seconds",
        "0.02",
        "--out",
        "runs/test",
    ]


class FakeMarket:
    made = []
    anomaly = False

    def __init__(self, *, settings, events, **kwargs):
        self.events = events
        self.closed = False
        self.connected = False
        self.made.append(self)

    def connect(self):
        self.connected = True
        return {}

    def subscribe(self, instruments):
        self.events.enqueue(SimpleNamespace(payload=make_tick(at=datetime.now(timezone.utc))))
        return [instrument.symbol for instrument in instruments]

    def status(self):
        return {
            "connected": self.connected,
            "logged_in": self.connected,
            "fault": None,
            "counts": {"timestamp_anomalies": int(self.anomaly)},
        }

    def close(self):
        self.closed = True


def test_default_observe_records_normalized_ticks_without_calendar_or_execution_service(tmp_path, monkeypatch):
    args = runtime_files(tmp_path, monkeypatch)
    monkeypatch.setenv("QH_CTP_PASSWORD", "test-runtime-secret")
    monkeypatch.setattr(runtime, "CtpMarketDataGateway", FakeMarket)
    monkeypatch.setattr(runtime.live_assembly, "assemble", lambda *_: pytest.fail("observe must not assemble trading"))
    assert runtime.main(args) == 0
    report = json.loads((tmp_path / "runs/test/runtime.json").read_text(encoding="utf-8"))
    assert report["mode"] == "observe" and report["status"] == "completed_observation"
    assert report["ticks_recorded"] == 1 and report["bars_recorded"] == 0 and report["commands_enqueued"] == 0
    assert "calendar_required" in report["bar_blocker"]
    rows = (tmp_path / "runs/test/ticks.jsonl").read_text(encoding="utf-8").splitlines()
    assert journal_codec.loads(rows[0]).instrument == INST
    assert not (tmp_path / "runs/test/trading.db").exists()
    assert FakeMarket.made[-1].closed
    assert "test-runtime-secret" not in (tmp_path / "runs/test/runtime.json").read_text(encoding="utf-8")


def test_trade_missing_warmup_calendar_and_margin_blocks_before_any_connection(tmp_path, monkeypatch):
    args = runtime_files(tmp_path, monkeypatch)
    monkeypatch.delenv("QH_CTP_PASSWORD", raising=False)
    monkeypatch.setattr(runtime, "CtpMarketDataGateway", lambda **_: pytest.fail("must block before connecting"))
    monkeypatch.setattr(runtime.live_assembly, "assemble", lambda *_: pytest.fail("must block before assembling"))
    assert runtime.main([*args, "--mode", "trade"]) == 2
    report = json.loads((tmp_path / "runs/test/runtime.json").read_text(encoding="utf-8"))
    assert report["status"] == "blocked_before_connection"
    assert "trusted_current_30m_warmup_required" in report["blockers"]
    assert "explicit_current_session_calendar_required" in report["blockers"]
    assert "complete_unambiguous_counter_margin_required" in report["blockers"]
    assert "explicit_confirm_isolated_required" in report["blockers"]
    assert not (tmp_path / "runs/test/trading.db").exists()


def test_observe_timestamp_anomaly_is_recorded_as_pause_and_never_a_trading_signal(tmp_path, monkeypatch):
    args = runtime_files(tmp_path, monkeypatch)
    monkeypatch.setenv("QH_CTP_PASSWORD", "test-runtime-secret")
    monkeypatch.setattr(FakeMarket, "anomaly", True)
    monkeypatch.setattr(runtime, "CtpMarketDataGateway", FakeMarket)
    assert runtime.main(args) == 2
    report = json.loads((tmp_path / "runs/test/runtime.json").read_text(encoding="utf-8"))
    assert report["status"] == "no_verified_current_market_data"
    assert report["commands_enqueued"] == 0 and report["ticks_recorded"] == 1
    assert "market_gateway_quality_gap" in report["quality_faults"]


def test_checkpoint_mapping_round_trips_tick_and_bar_cursors(tmp_path):
    now = datetime.now(timezone.utc)
    checkpoint = StrategyCheckpoint(
        stream_id="stream",
        bar_end=now,
        processing=False,
        tick_time=now,
        tick_keys=("tick-key",),
        pending_command_ids=("command-id",),
    )
    with SQLiteStrategyCheckpointStore(tmp_path / "strategy.db") as store:
        wrapped = runtime.CheckpointMappingStore(store)
        wrapped.save(checkpoint)
        assert wrapped.load("stream") == checkpoint


def account_view(*positions):
    positions = tuple(positions)
    manager = SimpleNamespace(
        all_positions=lambda: positions, get_position=lambda *_: pytest.fail("read-only view must not create positions")
    )
    model = SimpleNamespace(
        positions=manager, orders=OrderManager(), funds_state=lambda: SimpleNamespace(total_equity=Decimal("100000"))
    )
    store = SimpleNamespace(
        checkpoint=lambda: SimpleNamespace(state={"account_view": {"total_equity": Decimal("100000")}})
    )
    return runtime.CommittedAccountView(model, store)


def test_committed_account_view_reads_existing_buckets_without_mutating_model():
    view = account_view(PositionDetail(INST, PositionSide.LONG, pos_yd=2, pos_td=1, frozen_yd=1))
    assert view.get_position(INST) == 3
    assert view.quantities(INST, PositionSide.LONG) == (1, 1)
    assert view.get_position(InstrumentId(Exchange.SHFE, "rb2701")) == 0
    assert view.equity() == Decimal("100000")
    assert view.has_active_orders(INST) is False


def order_intent(*, side=Side.BUY, offset=Offset.OPEN, quantity=1):
    return OrderIntent(
        client_order_id="signal",
        account_id="test",
        strategy_id="ema",
        instrument=INST,
        side=side,
        offset=offset,
        quantity=quantity,
        order_type=OrderType.MARKET,
        created_at=datetime.now(timezone.utc),
    )


def translator(account, *, tick=None, mapping=True):
    now = datetime.now(timezone.utc)
    quote = tick or replace(make_tick(at=now), lower_limit_price=Decimal("2800"), upper_limit_price=Decimal("3200"))
    close_mapping = (
        SimpleNamespace(
            verified=True, evidence_ref="test:verified", flags={Offset.CLOSE_TODAY: "3", Offset.CLOSE_YESTERDAY: "4"}
        )
        if mapping
        else None
    )
    return runtime.LimitOrderTranslator(
        instrument=INST,
        price_tick=Decimal("1"),
        account=account,
        quote_provider=lambda: quote,
        clock=SimpleNamespace(now=lambda: now),
        slippage_ticks=1,
        close_mapping=close_mapping,
    )


def test_order_translation_uses_opposite_price_plus_slippage_as_integer_limit_ticks():
    (translated,) = translator(account_view())(order_intent())
    assert translated.order_type == OrderType.LIMIT and translated.limit_price_ticks == 3002
    assert translated.offset == Offset.OPEN


def test_close_translation_uses_only_existing_unfrozen_verified_bucket():
    view = account_view(PositionDetail(INST, PositionSide.LONG, pos_yd=1, frozen_yd=1, pos_td=1))
    (translated,) = translator(view)(order_intent(side=Side.SELL, offset=Offset.CLOSE))
    assert translated.offset == Offset.CLOSE_TODAY and translated.limit_price_ticks == 2998
    with pytest.raises(runtime.RuntimeBlocked, match="existing_unfrozen"):
        translator(view)(order_intent(side=Side.SELL, offset=Offset.CLOSE_YESTERDAY))
    with pytest.raises(runtime.RuntimeBlocked, match="verified_close"):
        translator(view, mapping=False)(order_intent(side=Side.SELL, offset=Offset.CLOSE))


def test_translator_refuses_extra_lots_stale_quotes_and_limit_violations():
    with pytest.raises(runtime.RuntimeBlocked, match="one_lot"):
        translator(account_view())(order_intent(quantity=2))
    with pytest.raises(runtime.RuntimeBlocked, match="stale"):
        translator(account_view(), tick=make_tick(at=datetime.now(timezone.utc) - timedelta(seconds=6)))(order_intent())
    quote = replace(
        make_tick(at=datetime.now(timezone.utc)), lower_limit_price=Decimal("2800"), upper_limit_price=Decimal("3001")
    )
    with pytest.raises(runtime.RuntimeBlocked, match="exceeds_counter_limits"):
        translator(account_view(), tick=quote)(order_intent())


def test_fees_preserve_distinct_open_close_yesterday_today_amount_and_lot_components():
    report = {
        "rates": {
            "commission": {
                "complete": True,
                "records": [
                    {
                        "OpenRatioByMoney": 0,
                        "CloseRatioByMoney": 0,
                        "CloseTodayRatioByMoney": 0,
                        "OpenRatioByVolume": 5,
                        "CloseRatioByVolume": 5,
                        "CloseTodayRatioByVolume": 5,
                    }
                ],
            },
            "margin": {
                "complete": True,
                "records": [
                    {
                        "LongMarginRatioByMoney": "0.1",
                        "ShortMarginRatioByMoney": "0.1",
                        "LongMarginRatioByVolume": 0,
                        "ShortMarginRatioByVolume": 0,
                        "HedgeFlag": "1",
                        "IsRelative": 0,
                    }
                ],
            },
        }
    }
    for result in report["rates"].values():
        result["records"][0].update(InstrumentID="rb2610", ExchangeID="SHFE")
    spec = SimpleNamespace(instrument=INST, multiplier=Decimal(10), price_tick=Decimal(1))
    fees = report["rates"]["commission"]["records"][0]
    fees["OpenRatioByMoney"] = "0.0001"
    economics = runtime.economics_from_preflight(report, spec, source="test:counter")
    assert economics.commission(Decimal(3000), 1, Offset.OPEN) == Decimal(8)
    assert economics.commission(Decimal(3000), 1, Offset.CLOSE_YESTERDAY) == Decimal(5)
    report["rates"]["commission"]["records"][0]["CloseTodayRatioByVolume"] = 10
    updated = runtime.economics_from_preflight(report, spec, source="test:counter")
    assert updated.commission(Decimal(3000), 1, Offset.CLOSE_TODAY) == Decimal(10)
    fees["InstrumentID"] = "rb"
    with pytest.raises(runtime.RuntimeBlocked, match="exactly_match_actual_contract"):
        runtime.economics_from_preflight(report, spec, source="test:counter")


def test_foreign_preflight_is_refused_before_market_construction(tmp_path, monkeypatch):
    args = runtime_files(tmp_path, monkeypatch)
    preflight = json.loads((tmp_path / "preflight.json").read_text())
    preflight["profile"] = "real-broker"
    (tmp_path / "preflight.json").write_text(json.dumps(preflight))
    monkeypatch.setattr(runtime, "CtpMarketDataGateway", lambda **_: pytest.fail("foreign profile must not connect"))
    assert runtime.main(args) == 2
    report = json.loads((tmp_path / "runs/test/runtime.json").read_text())
    assert "registered_simnow_preflight_required" in report["blockers"]


def test_enqueued_unprocessed_command_is_exposure_at_runtime_deadline():
    engine = SimpleNamespace(checkpoint=SimpleNamespace(pending_command_ids=("queued",)))
    client = SimpleNamespace(get=lambda _: SimpleNamespace(status=CommandStatus.PENDING))
    assert runtime.pending_strategy_commands(engine, client, account_view()) == ("queued",)
    unknown = SimpleNamespace(status=CommandStatus.SENT_UNKNOWN, command=SimpleNamespace(payload=order_intent()))
    client.get = lambda _: unknown
    assert runtime.pending_strategy_commands(engine, client, account_view()) == ("queued",)


def test_invalid_slippage_does_not_get_silently_coerced_and_connect(tmp_path, monkeypatch):
    args = runtime_files(tmp_path, monkeypatch)
    config = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    config["research"]["slippage_ticks"] = 1.5
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setattr(runtime, "CtpMarketDataGateway", lambda **_: pytest.fail("invalid slippage must not connect"))
    assert runtime.main(args) == 2
    report = json.loads((tmp_path / "runs/test/runtime.json").read_text())
    assert "explicit_nonnegative_slippage_ticks_required" in report["blockers"]


@pytest.mark.parametrize("mode", ["none", "historical", "7x24"])
def test_observe_does_not_report_success_without_verified_current_ticks(tmp_path, monkeypatch, mode):
    args = runtime_files(tmp_path, monkeypatch)
    monkeypatch.setenv("QH_CTP_PASSWORD", "test-runtime-secret")
    if mode == "7x24":
        path = tmp_path / "preflight.json"
        preflight = json.loads(path.read_text())
        for target in (preflight, preflight["runtime_inputs"]):
            target.update(front_trade="tcp://182.254.243.31:40001", front_market="tcp://182.254.243.31:40011")
        path.write_text(json.dumps(preflight))

    class QuietMarket(FakeMarket):
        def subscribe(self, instruments):
            if mode != "none":
                at = datetime.now(timezone.utc) - (timedelta(days=1) if mode == "historical" else timedelta(0))
                self.events.enqueue(SimpleNamespace(payload=make_tick(at=at)))
            return [instrument.symbol for instrument in instruments]

    monkeypatch.setattr(runtime, "CtpMarketDataGateway", QuietMarket)
    assert runtime.main(args) == 2
    report = json.loads((tmp_path / "runs/test/runtime.json").read_text())
    assert report["fresh_ticks"] == 0
    assert report["status"] in ("no_market_data", "no_verified_current_market_data")


def prepare_trade_run(tmp_path, monkeypatch):
    args = runtime_files(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    preflight_path = tmp_path / "preflight.json"
    preflight = json.loads(preflight_path.read_text())
    preflight["rates"]["margin"] = {
        "complete": True,
        "records": [
            {
                "InstrumentID": "rb2610",
                "ExchangeID": "SHFE",
                "HedgeFlag": "1",
                "IsRelative": 0,
                "LongMarginRatioByMoney": "0.12",
                "ShortMarginRatioByMoney": "0.12",
                "LongMarginRatioByVolume": 0,
                "ShortMarginRatioByVolume": 0,
            }
        ],
    }
    preflight_path.write_text(json.dumps(preflight))
    profile = runtime.ctp_setup.load_broker_profile("simnow_v6")
    profile["capabilities"]["offset_mapping"]["SHFE"] = {
        "value": {"CLOSE_TODAY": "3", "CLOSE_YESTERDAY": "4"},
        "verification_status": "verified",
        "evidence": "test:broker-offsets",
    }
    monkeypatch.setattr(runtime.ctp_setup, "load_broker_profile", lambda *_: profile)
    calendar = {
        "schema_version": 1,
        "version": "test-calendar",
        "source_id": "test-source",
        "available_at": (now - timedelta(days=1)).isoformat(),
        "coverage_start": str(DAY),
        "coverage_end": str(DAY),
        "trading_days": [str(DAY)],
        "sessions": [
            {
                "exchange": "SHFE",
                "symbol": "rb2610",
                "session_id": "test-session",
                "trading_day": str(DAY),
                "start": (now - timedelta(minutes=1)).isoformat(),
                "end": (now + timedelta(hours=1)).isoformat(),
                "phase": "CONTINUOUS",
                "permissions": {"submit": True, "cancel": True, "match": True},
                "available_at": (now - timedelta(days=1)).isoformat(),
            }
        ],
    }
    (tmp_path / "calendar.json").write_text(json.dumps(calendar))
    history = []
    for index in range(210):
        end = now - timedelta(minutes=30 * (210 - index))
        start = end - timedelta(minutes=30)
        history.append(
            Bar(
                instrument=INST,
                meta=RecordMeta(
                    event_time=end,
                    available_at=end,
                    ingested_at=end,
                    trading_day=end.date(),
                    source_id="test-warmup",
                    source_version="1",
                    ingest_seq=index,
                ),
                bar_start=start,
                bar_end=end,
                interval="30m",
                open=Decimal(3000),
                high=Decimal(3001),
                low=Decimal(2999),
                close=Decimal(3000),
                volume=100,
                turnover=Decimal(3000000),
                open_interest=1000,
                open_time=start,
                includes_auction=False,
            )
        )
    monkeypatch.setattr(runtime, "load_warmup_bars", lambda *args, **kwargs: tuple(history))
    return [*args, "--mode", "trade", "--confirm-isolated", "--calendar", "calendar.json"]


def test_trade_real_assembly_fake_ctp_startup_and_fixed_account_state_across_output_directories(tmp_path, monkeypatch):
    from qh_trader.gateway import ctp_gateway

    args = prepare_trade_run(tmp_path, monkeypatch)
    monkeypatch.setenv("QH_CTP_PASSWORD", "test-runtime-secret")
    binding = FakeCtpBinding(
        user_id="231495",
        investor_id="231495",
        trading_day=DAY.strftime("%Y%m%d"),
        query_records={
            "account": (account_record("100000", "100000", "0"),),
            "investor": (FakeField(BrokerID="9999", InvestorID="231495"),),
            "user_session": (FakeField(BrokerID="9999", UserID="231495", FrontID=12, SessionID=345678),),
        },
    )
    monkeypatch.setattr(ctp_gateway, "load_ctp_binding", lambda: binding)

    class CanonicalMarket(FakeMarket):
        def subscribe(self, instruments):
            tick = make_tick(at=datetime.now(timezone.utc))
            self.events.enqueue(
                CanonicalEvent(
                    event_id="runtime-test-tick",
                    kind=EventKind.MARKET_DATA,
                    event_time=tick.meta.event_time,
                    available_at=tick.meta.available_at,
                    sequence=1,
                    source_id="test",
                    payload=tick,
                )
            )
            return [instrument.symbol for instrument in instruments]

    monkeypatch.setattr(runtime.live_assembly, "CtpMarketDataGateway", CanonicalMarket)
    original_assemble = runtime.live_assembly.assemble
    observed = []
    checked_quotes = []
    dispatch_quotes = []
    original_timeout = runtime.quote_timed_out

    def check_timeout(**kwargs):
        checked_quotes.append(kwargs["quote"])
        return original_timeout(**kwargs)

    monkeypatch.setattr(runtime, "quote_timed_out", check_timeout)

    def assemble(spec, **kwargs):
        result = original_assemble(spec, **kwargs)
        before = result.model.positions.all_positions()
        # 首次 opened 没有 account_view；资金接口仍可读取且不改变本启动账户的持仓集合。
        assert runtime.CommittedAccountView(result.model, result.store).equity() == Decimal(100000)
        assert result.model.positions.all_positions() == before
        observed.append((spec, result.economics[INST]))
        process_command = result.service.process_next_command

        def process_after_market_check():
            dispatch_quotes.append(checked_quotes[-1] if checked_quotes else None)
            return process_command()

        result.service.process_next_command = process_after_market_check
        return result

    monkeypatch.setattr(runtime.live_assembly, "assemble", assemble)
    code = runtime.main(args)
    report = json.loads((tmp_path / "runs/test/runtime.json").read_text())
    assert code == 0, (report.get("blockers"), report.get("error_type"))
    assert report["status"] == "completed_simulation_window" and report["counter_identity_verified"]
    assert report["fresh_ticks"] == 1 and report["strategy_ready"]
    assert checked_quotes and all(quote is not None for quote in checked_quotes)
    assert dispatch_quotes and all(quote is not None for quote in dispatch_quotes)
    assert observed[0][0].poll_interval == 0.05
    assert observed[0][1].commission(Decimal(3000), 1, Offset.OPEN) == Decimal(3)
    assert observed[0][1].margin_ratio == Decimal("0.12")
    state = runtime.account_state_directory(tmp_path, "231495")
    assert observed[0][0].journal_path == state / "trading.db"
    assert observed[0][0].heartbeat_path == state / "heartbeat.json"
    assert not (tmp_path / "runs/test/trading.db").exists()
    assert binding.api.insert_fields == [] and binding.api.action_fields == []
    assert runtime.main([*args, "--entry-mode", "B", "--out", "runs/another-output"]) == 2
    second = json.loads((tmp_path / "runs/another-output/runtime.json").read_text())
    assert second["status"] == "blocked_before_connection"
    assert "existing_account_database_requires_explicit_recovery_workflow" in second["blockers"]
    assert len(observed) == 1


def test_tick_detected_bar_gap_pauses_before_strategy_callback(tmp_path, monkeypatch):
    args = prepare_trade_run(tmp_path, monkeypatch)
    monkeypatch.setenv("QH_CTP_PASSWORD", "test-runtime-secret")
    monkeypatch.setattr(runtime, "CtpMarketDataGateway", FakeMarket)
    calendar_path = tmp_path / "calendar.json"
    calendar = json.loads(calendar_path.read_text())
    current = calendar["sessions"][0]
    previous_end = datetime.fromisoformat(current["start"])
    previous_start = previous_end - timedelta(minutes=30)
    calendar["sessions"].insert(
        0,
        current
        | {"session_id": "previous-session", "start": previous_start.isoformat(), "end": previous_end.isoformat()},
    )
    calendar_path.write_text(json.dumps(calendar))
    original = runtime.LiveBarAggregator

    def stalled_aggregator(**kwargs):
        aggregator = original(**kwargs)
        # 模拟循环停顿：旧桶只有头部行情，恢复后的新会话 Tick 会令 _finish 检出缺口。
        for at in (previous_start, previous_start + timedelta(seconds=5)):
            aggregator.on_tick(make_tick(at=at), now=at)
        return aggregator

    monkeypatch.setattr(runtime, "LiveBarAggregator", stalled_aggregator)
    notified = []
    monkeypatch.setattr(runtime.EmaTrendStrategy, "on_tick", lambda self, tick: notified.append(tick))
    assert runtime.main([*args, "--mode", "observe"]) == 2
    report = json.loads((tmp_path / "runs/test/runtime.json").read_text())
    assert "bar_continuity_gap" in report["quality_faults"]
    assert report["commands_enqueued"] == 0
    assert report["bars_recorded"] == 0
    assert notified == []


@pytest.mark.parametrize(
    "second,quote_second,expected",
    [
        (59, 54, False),
        (59, 53, True),
        (66, 59, False),
        (119, 59, False),
        (125, 59, False),
        (126, 59, True),
        (126, 125, False),
    ],
)
def test_quote_timeout_distinguishes_recess_resume_and_trading_silence(second, quote_second, expected):
    sessions = (
        session(duration=60),
        session(SESSION_START + timedelta(seconds=120), duration=60, name="after-recess"),
    )
    assert (
        runtime.quote_timed_out(
            now=SESSION_START + timedelta(seconds=second),
            quote=make_tick(at=SESSION_START + timedelta(seconds=quote_second)),
            sessions=sessions,
            started_at=SESSION_START,
        )
        is expected
    )


@pytest.mark.parametrize("elapsed,expected", [(5, False), (6, True)])
def test_quote_timeout_also_detects_missing_first_tick_after_startup(elapsed, expected):
    started = SESSION_START + timedelta(seconds=20)
    assert (
        runtime.quote_timed_out(
            now=started + timedelta(seconds=elapsed),
            quote=None,
            sessions=(session(),),
            started_at=started,
        )
        is expected
    )


def test_identity_link_is_mandatory_and_counter_session_must_match():
    expected = {"user": runtime.mask_identifier("login"), "investor": runtime.mask_identifier("investor")}
    with pytest.raises(runtime.RuntimeBlocked, match="identity_link_required"):
        runtime.verify_configured_identity({}, "login", "investor")
    with pytest.raises(runtime.RuntimeBlocked, match="differs_from_preflight"):
        runtime.verify_configured_identity(expected, "other", "investor")
    assembled = SimpleNamespace(
        counter_gateway=SimpleNamespace(
            settings=SimpleNamespace(user_id="login", investor_id="investor", broker_id="9999"),
            status=lambda: {"front_id": 1, "session_id": 2},
        ),
        query=SimpleNamespace(
            query_investor=lambda: {"BrokerID": "9999", "InvestorID": "investor"},
            query_user_sessions=lambda _: [{"BrokerID": "9999", "UserID": "login", "FrontID": 9, "SessionID": 2}],
        ),
    )
    with pytest.raises(runtime.RuntimeBlocked, match="current_user_session_link_required"):
        runtime.verify_counter_identity(expected, assembled)
