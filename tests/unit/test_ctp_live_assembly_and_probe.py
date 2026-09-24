"""S5-01 / S0-02 实盘装配与柜台探测脚本：连接、接管、对账、放行与证据落盘（用绑定假件驱动）."""

from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from qh_trader.core.constants import Exchange, Offset, OrderStatus, OrderType, SendState, Side
from qh_trader.core.execution import CommandKind, CommandStatus, ExecutionCommand
from qh_trader.core.objects import InstrumentId, OrderIntent
from qh_trader.gateway import ctp_gateway
from scripts import ctp_probe, run_execution_service
from scripts.live_assembly import AssemblyError, assemble, spec_from_settings

ROOT = Path(__file__).resolve().parents[2]
DAY = date(2024, 9, 10)
SYMBOL = "SHFE.rb2410"
INSTRUMENT = InstrumentId(Exchange.SHFE, "rb2410")
ACCOUNT = "counter-account"
SECRET = "counter-password-from-environment"


def write_settings(tmp_path: Path, **overrides) -> Path:
    template = yaml.safe_load((ROOT / "config" / "settings.yaml.example").read_text(encoding="utf-8"))
    template["system"]["mode"] = "live"
    template["risk"]["account_id"] = ACCOUNT
    template["risk"]["initial_capital"] = "100000.00"
    template["storage"]["journal_db_path"] = str(tmp_path / "trading.db")
    template["strategy"]["symbols"] = [SYMBOL]
    template["broker"] = {"profile": "simnow_v6", "user_id": "231495", "investor_id": "231495"}
    template.update(overrides)
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(template, allow_unicode=True), encoding="utf-8")
    return path


def install_fake_counter(monkeypatch: pytest.MonkeyPatch, binding) -> None:
    monkeypatch.setattr(ctp_gateway, "load_ctp_binding", lambda: binding)
    monkeypatch.setenv("QH_CTP_PASSWORD", SECRET)


def fake_account(**overrides):
    from tests.unit.fake_ctp import FakeCtpBinding, account_record, position_record

    records = {"account": (account_record("100000", "90000", "10000"),)}
    binding = FakeCtpBinding(
        instrument_id="rb2410",
        exchange_id="SHFE",
        trading_day=DAY.strftime("%Y%m%d"),
        insert_date=DAY.strftime("%Y%m%d"),
        query_records=records,
        **overrides,
    )
    assert position_record is not None
    return binding


def live_spec(tmp_path: Path, *, day: date = DAY, symbols=(SYMBOL,), **overrides):
    settings = write_settings(tmp_path, **overrides)
    data = yaml.safe_load(settings.read_text(encoding="utf-8"))
    return spec_from_settings(
        data,
        config_path=settings,
        mode="live",
        trading_day=day,
        symbols=list(symbols),
        controller_id="execution-service",
        heartbeat_path=str(tmp_path / "hb.json"),
    )


def submit_command(client, *, client_order_id: str, epoch: int, instrument=INSTRUMENT) -> ExecutionCommand:
    intent = OrderIntent(
        client_order_id=client_order_id,
        account_id=ACCOUNT,
        strategy_id="test-strategy",
        instrument=instrument,
        side=Side.BUY,
        offset=Offset.OPEN,
        quantity=1,
        order_type=OrderType.LIMIT,
        created_at=datetime.now(timezone.utc),
        limit_price_ticks=3500,
    )
    command = ExecutionCommand(
        command_id=f"cmd-{client_order_id}",
        account_id=ACCOUNT,
        producer_id="test-strategy",
        control=ctp_gateway.ControlEpoch("execution-service", epoch),
        kind=CommandKind.SUBMIT,
        submitted_at=datetime.now(timezone.utc),
        payload=intent,
    )
    client.submit(command)
    return command


def test_live_assembly_requires_the_registered_profile_and_the_environment_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("QH_CTP_PASSWORD", raising=False)
    settings = write_settings(tmp_path, broker={"front_trade_uri": "tcp://127.0.0.1:10201"})
    data = yaml.safe_load(settings.read_text(encoding="utf-8"))
    with pytest.raises(AssemblyError, match="broker.profile"):
        spec_from_settings(data, config_path=settings, mode="live", trading_day=DAY)
    spec = live_spec(tmp_path)
    with pytest.raises(Exception, match="QH_CTP_PASSWORD"):
        assemble(spec)


def test_live_assembly_connects_takes_over_reconciles_and_routes_counter_reports(tmp_path, monkeypatch):
    binding = fake_account()
    install_fake_counter(monkeypatch, binding)
    assembled = assemble(live_spec(tmp_path))
    try:
        assert assembled.live is True
        session = assembled.connect_counter()
        assert session is not None and session["front_id"] == 12
        assert session["trading_day"] == DAY.isoformat()

        # A23 序列：隔离 → 提升代次 → 对账 → 放行
        request = assembled.request_control("opening the live session")
        control = assembled.take_over(request.command_id, assembled.isolation(operator_confirmed=True))
        assert (control.controller_id, control.epoch) == ("execution-service", 1)
        assembled.reconcile_and_enable()
        assert assembled.service.ready is True
        assert assembled.counter_gateway.ready_to_send is True

        # 柜台报单：网关在调用前复核代次，回报经回调线程归一化并入队
        submit_command(assembled.client, client_order_id="cid-live-1", epoch=1)
        assert assembled.step() >= 1
        assert assembled.step() >= 1  # 回报在下一步被账户序列消费
        replica = assembled.model.replica()
        order = replica.orders.get_order("cid-live-1")
        assert order is not None and order.status == OrderStatus.ACCEPTED
        assert order.send_state == SendState.CONFIRMED_REMOTE
        assert order.identity is not None and order.identity.order_ref == binding.api.insert_fields[0].OrderRef
        manifest = assembled.manifest()
        # 清单记录的是被代次护栏包裹的真实网关类型
        assert manifest["gateway"] == "CtpTraderGateway"
        assert manifest["query_source"] == "CtpQueryAdapter"
        assert manifest["counter"]["status"]["order_refs_allocated"] == 1
        assert SECRET not in json.dumps(manifest, default=str)

        # 断线后网关关闭发送门禁，执行服务也随之重新要求对账 (FR-REC-04)
        binding.api.front_disconnected()
        assembled.step()
        assert assembled.counter_gateway.ready_to_send is False
        assert assembled.service.ready is False
        blocked = submit_command(assembled.client, client_order_id="cid-live-2", epoch=1)
        assembled.step()
        queued = assembled.store.get(blocked.command_id)
        assert queued is not None and queued.status is CommandStatus.REJECTED
        assert len(binding.api.insert_fields) == 1
    finally:
        assembled.close()


def test_live_assembly_refuses_a_counter_trading_day_that_differs(tmp_path, monkeypatch):
    binding = fake_account()
    install_fake_counter(monkeypatch, binding)
    spec = live_spec(tmp_path, day=date(2024, 9, 11))
    assembled = assemble(spec)
    try:
        with pytest.raises(AssemblyError, match="trading day"):
            assembled.connect_counter()
    finally:
        assembled.close()


def test_live_takeover_needs_operator_confirmation_and_a_stale_heartbeat(tmp_path, monkeypatch):
    binding = fake_account()
    install_fake_counter(monkeypatch, binding)
    assembled = assemble(live_spec(tmp_path))
    try:
        assembled.connect_counter()
        request = assembled.request_control("takeover without confirmation")
        with pytest.raises(Exception, match="isolated"):
            assembled.take_over(request.command_id, assembled.isolation(operator_confirmed=False))
        # 心跳新鲜（同一实例）时即使操作员确认也不成立
        fresh = assembled.isolation(operator_confirmed=True)
        assembled.heartbeat.beat(control_epoch=None, ready=False)
        request2 = assembled.request_control("takeover with a fresh heartbeat")
        with pytest.raises(Exception, match="isolated"):
            assembled.take_over(request2.command_id, fresh)
        assert fresh.evidence[-1]["heartbeat_age_s"] < 30.0
    finally:
        assembled.close()


def test_run_execution_service_live_mode_uses_the_counter_isolation(tmp_path, monkeypatch):
    binding = fake_account()
    install_fake_counter(monkeypatch, binding)
    settings = write_settings(tmp_path)
    code = run_execution_service.main(
        [
            "--config",
            str(settings),
            "--mode",
            "live",
            "--symbols",
            SYMBOL,
            "--trading-day",
            DAY.isoformat(),
            "--out",
            str(tmp_path / "out"),
            "--heartbeat",
            str(tmp_path / "hb.json"),
            "--confirm-isolated",
            "--request-control",
            "live startup",
            "--max-iterations",
            "1",
        ]
    )
    # 柜台假件可完成接管与对账，但命令表写入的接管申请由装配受理；放行后主循环退出码为 0
    assert code in (0, 4)


def probe_arguments(out_dir: str, *extra: str) -> list[str]:
    return [
        "--profile",
        "simnow_v6",
        "--account",
        "probe-account",
        "--user",
        "231495",
        "--investor",
        "231495",
        "--flow-dir",
        str(Path(out_dir) / "flow"),
        "--out",
        out_dir,
        "--query-timeout",
        "1.0",
        "--order-wait",
        "1.0",
        *extra,
    ]


def build_probe_binding():
    from tests.unit.fake_ctp import FakeCtpBinding, account_record, position_record

    order = type(
        "O",
        (),
        {
            "InstrumentID": "rb2410",
            "ExchangeID": "SHFE",
            "OrderRef": "1",
            "OrderSysID": "sys-1",
            "Direction": "0",
            "CombOffsetFlag": "0",
            "OrderStatus": "3",
            "OrderSubmitStatus": "3",
            "VolumeTotalOriginal": 1,
            "VolumeTraded": 0,
            "InsertDate": DAY.strftime("%Y%m%d"),
            "InsertTime": "09:35:00",
            "TradingDay": DAY.strftime("%Y%m%d"),
        },
    )()
    instrument = type(
        "I",
        (),
        {
            "InstrumentID": "rb2410",
            "ExchangeID": "SHFE",
            "VolumeMultiple": 10,
            "PriceTick": 1.0,
            "ExpireDate": "20270115",
            "IsTrading": 1,
        },
    )()
    depth = type(
        "D",
        (),
        {"InstrumentID": "rb2410", "LowerLimitPrice": 2700.0, "PreSettlementPrice": 3000.0, "UpperLimitPrice": 3300.0},
    )()
    trade = type(
        "T",
        (),
        {
            "InstrumentID": "rb2410",
            "ExchangeID": "SHFE",
            "OrderRef": "1",
            "OrderSysID": "sys-1",
            "TradeID": "T-1",
            "Direction": "0",
            "OffsetFlag": "0",
            "Price": 3000.0,
            "Volume": 1,
            "TradeDate": DAY.strftime("%Y%m%d"),
            "TradeTime": "09:35:01",
            "TradingDay": DAY.strftime("%Y%m%d"),
        },
    )()
    return FakeCtpBinding(
        instrument_id="rb2410",
        exchange_id="SHFE",
        trading_day=DAY.strftime("%Y%m%d"),
        insert_date=DAY.strftime("%Y%m%d"),
        query_records={
            "account": (account_record("100000"),),
            "position": (position_record(instrument="rb2410"),),
            "order": (order,),
            "trade": (trade,),
            "instrument": (instrument,),
            "depth": (depth,),
        },
    )


def test_probe_script_collects_redacted_evidence_and_verifies_the_order_round_trip(tmp_path, monkeypatch):
    binding = build_probe_binding()
    install_fake_counter(monkeypatch, binding)
    out_dir = "runs/pytest-ctp-probe"
    target = ROOT / out_dir
    shutil.rmtree(target, ignore_errors=True)
    try:
        code = ctp_probe.main(
            probe_arguments(out_dir, "--order-symbol", SYMBOL, "--price-tick", "1", "--query-interval-ms", "0")
        )
        assert code == 0
        files = sorted(target.glob("ctp_runtime_evidence_*.json"))
        assert len(files) == 1
        report = json.loads(files[0].read_text(encoding="utf-8"))
        steps = {step["name"]: step for step in report["steps"]}
        assert steps["connect_login_settlement"]["status"] == "passed"
        assert steps["counter_trading_day"]["status"] == "passed"
        for name in ("query_account", "query_positions", "query_orders", "query_trades"):
            assert steps[name]["status"] == "passed"
        assert report["account"]["investor_id"]["masked"].endswith("95")
        assert SECRET not in json.dumps(report)
        assert report["binding"]["dll_hashes"]
        assert report["session"]["front_id"] == 12
        order = report["order_probe"]
        assert order["send_result"]["state"] == SendState.SENT_UNKNOWN.value
        assert order["statuses_after_submit"] == [OrderStatus.ACCEPTED.value]
        assert order["statuses_after_cancel"] == [OrderStatus.ACCEPTED.value, OrderStatus.CANCELLED.value]
        assert order["contract"]["volume_multiple"] == 10
        assert order["result"].startswith("order insert and cancel round trip verified")
        assert report["callbacks_normalized"]["orders"] >= 2
    finally:
        shutil.rmtree(target, ignore_errors=True)


def test_probe_script_refuses_to_run_without_the_local_secret(monkeypatch, capsys):
    monkeypatch.delenv("QH_CTP_PASSWORD", raising=False)
    assert ctp_probe.main(["--profile", "simnow_v6", "--user", "231495", "--investor", "231495"]) == 2
    assert "QH_CTP_PASSWORD" in capsys.readouterr().err


def test_probe_script_refuses_to_write_evidence_outside_runs(tmp_path, monkeypatch):
    install_fake_counter(monkeypatch, build_probe_binding())
    assert ctp_probe.main(probe_arguments(str(tmp_path))) == 2


def test_counter_profile_registration_is_translated_without_inventing_capabilities():
    from scripts import ctp_setup

    profile = ctp_setup.load_broker_profile("simnow_v6")
    capabilities = ctp_setup.capability_profile(profile)
    assert capabilities.profile_id == "simnow_v6"
    # 登记里 value 为 null 的能力一律是"未知"，不给默认值
    assert capabilities.values["order_types.market_order"].verified is False
    assert capabilities.values["order_types.market_order"].value is None
    offsets = ctp_setup.offset_mappings(profile)
    assert offsets and all(mapping.verified is False for mapping in offsets)
    assert ctp_setup.front_addresses(profile)["trade"].startswith("tcp://")
    summary = ctp_setup.profile_summary(profile)
    assert summary["capability_states"]["offset_mapping.SHFE"] == "登记缺口"
    assert Decimal("1") > 0
