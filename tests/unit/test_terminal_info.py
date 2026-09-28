"""[单元测试] 终端采集与看穿式监管上报测试 (S5-03, FR-LIVE-02, FR-LIVE-03, A27, F17)."""

from __future__ import annotations

import platform
from pathlib import Path

import pytest

from qh_trader.gateway.ctp_gateway import CtpHandshakeError
from qh_trader.gateway.terminal_info import (
    TerminalAccessMode,
    TerminalCollectorReport,
    TerminalInfoCollector,
    TerminalInfoError,
    TerminalInfoPayload,
    apply_relay_user_system_info,
)
from tests.unit.fake_ctp import FakeCtpBinding, FakeField
from tests.unit.test_ctp_gateway import make_gateway, make_settings

ROOT = Path(__file__).resolve().parents[2]
DEMO_DLL = ROOT / "docs/sinnow/6.7.13_apidemo/demo/WinDataCollect.dll"


def test_terminal_info_collector_real_dll_on_windows():
    """在 Windows 且存在官方采集库时，核验完整采集逻辑与特征摘要 (FR-LIVE-02)."""
    if platform.system() != "Windows" or not DEMO_DLL.is_file():
        pytest.skip("WinDataCollect.dll 未在本地部署或非 Windows 环境")

    collector = TerminalInfoCollector(
        lib_path=DEMO_DLL,
        access_mode=TerminalAccessMode.DIRECT,
    )
    assert collector.find_library() == DEMO_DLL.resolve()

    payload = collector.collect()
    assert isinstance(payload, TerminalInfoPayload)
    assert 0 < payload.length <= 344
    assert len(payload.raw_bytes) == payload.length

    report = payload.report
    assert isinstance(report, TerminalCollectorReport)
    assert report.api_version.startswith("sfit_")
    assert len(report.dll_hash) == 64
    assert report.system_info_len == payload.length
    assert report.access_mode == "direct"
    assert report.masked_digest.startswith("sha256:")
    assert len(report.masked_digest) == 23  # "sha256:" + 16 chars


def test_terminal_info_missing_library_raises_terminal_info_error():
    """采集库不存在时明确失败，阻止进入可交易状态 (A27, F17)."""
    collector = TerminalInfoCollector(
        lib_path="non_existent_dir/WinDataCollect.dll",
        access_mode=TerminalAccessMode.DIRECT,
        search_paths=(),
    )
    with pytest.raises(TerminalInfoError, match="看穿式监管采集库未找到"):
        collector.collect()


def test_terminal_info_report_does_not_leak_raw_payload():
    """报告、repr 及 as_mapping 绝不暴露原始二进制采集载荷 (NFR-06, A27)."""
    report = TerminalCollectorReport(
        collector_dll="C:/libs/WinDataCollect.dll",
        dll_hash="a" * 64,
        api_version="sfit_test_1.0",
        system_info_len=265,
        status_code=0,
        is_complete=True,
        collected_at="2026-09-28T00:00:00Z",
        masked_digest="sha256:1234567890abcdef",
        access_mode="direct",
    )
    payload = TerminalInfoPayload(
        raw_bytes=b"\x00\x01\x02\xffSECRET_PAYLOAD",
        length=18,
        report=report,
    )

    # 原始敏感载荷不得出现在 repr / str / as_mapping 中
    assert "SECRET_PAYLOAD" not in repr(report)
    assert "SECRET_PAYLOAD" not in str(report)
    assert "SECRET_PAYLOAD" not in repr(payload)
    assert "SECRET_PAYLOAD" not in str(payload)
    mapping = report.as_mapping()
    assert "SECRET_PAYLOAD" not in str(mapping)
    assert mapping["masked_digest"] == "sha256:1234567890abcdef"


def test_apply_relay_user_system_info_populates_fields():
    """中继模式下将���集信息精准注入 CThostFtdcUserSystemInfoField (A27)."""
    report = TerminalCollectorReport(
        collector_dll="dummy.dll",
        dll_hash="0" * 64,
        api_version="1.0",
        system_info_len=10,
        status_code=0,
        is_complete=True,
        collected_at="2026-09-28T00:00:00Z",
        masked_digest="sha256:test",
        access_mode="relay",
    )
    payload = TerminalInfoPayload(
        raw_bytes=b"0123456789",
        length=10,
        report=report,
    )

    field = FakeField("CThostFtdcUserSystemInfoField")
    apply_relay_user_system_info(
        field,
        payload,
        broker_id="9999",
        user_id="231495",
        app_id="simnow_client_test",
        public_ip="182.254.243.31",
        ip_port=40001,
    )

    assert field.BrokerID == "9999"
    assert field.UserID == "231495"
    assert field.ClientAppID == "simnow_client_test"
    assert field.ClientSystemInfoLen == 10
    assert field.ClientPublicIP == "182.254.243.31"
    assert field.ClientIPPort == 40001
    assert field.ClientLoginTime is not None


def test_gateway_handshake_in_direct_mode_with_collector(monkeypatch):
    """直连模式下握手自动采集并写入 session_report 与 dll_hashes (A27)."""
    # 模拟 collector
    mock_report = TerminalCollectorReport(
        collector_dll="WinDataCollect.dll",
        dll_hash="f" * 64,
        api_version="sfit_test_1.0",
        system_info_len=265,
        status_code=0,
        is_complete=True,
        collected_at="2026-09-28T00:00:00Z",
        masked_digest="sha256:deadbeef12345678",
        access_mode="direct",
    )
    mock_payload = TerminalInfoPayload(
        raw_bytes=b"X" * 265,
        length=265,
        report=mock_report,
    )

    monkeypatch.setattr(TerminalInfoCollector, "collect", lambda self: mock_payload)

    binding = FakeCtpBinding()
    settings = make_settings(terminal_mode="direct")
    gateway, sink, _ = make_gateway(binding=binding, settings=settings)

    session = gateway.connect()
    assert session.terminal_info is not None
    assert session.terminal_info["masked_digest"] == "sha256:deadbeef12345678"
    assert session.terminal_info["access_mode"] == "direct"
    assert session.dll_hashes["WinDataCollect.dll"] == "f" * 64
    assert gateway.ready_to_send is False
    assert gateway.mark_reconciled() is True
    assert gateway.ready_to_send is True


def test_gateway_handshake_missing_collector_blocks_login():
    """配置 direct 模式但采集库缺失时，握手抛出异常且阻止就绪 (A27, F17)."""
    binding = FakeCtpBinding()
    settings = make_settings(
        terminal_mode="direct",
        collector_lib_path="missing_path/WinDataCollect.dll",
    )
    gateway, sink, _ = make_gateway(binding=binding, settings=settings)

    with pytest.raises(CtpHandshakeError, match="terminal info collection failed"):
        gateway.connect()

    assert gateway.ready_to_send is False
    assert gateway.fault == "terminal_info_collection"
    assert binding.created == []  # 未进入底层连接与登录


def test_gateway_handshake_in_relay_mode_calls_register_user_system_info(monkeypatch):
    """中继模式下握手在认证后、登录前调用 RegisterUserSystemInfo (A27)."""
    mock_report = TerminalCollectorReport(
        collector_dll="WinDataCollect.dll",
        dll_hash="e" * 64,
        api_version="sfit_test_1.0",
        system_info_len=100,
        status_code=0,
        is_complete=True,
        collected_at="2026-09-28T00:00:00Z",
        masked_digest="sha256:relay1234567890",
        access_mode="relay",
    )
    mock_payload = TerminalInfoPayload(
        raw_bytes=b"Y" * 100,
        length=100,
        report=mock_report,
    )

    monkeypatch.setattr(TerminalInfoCollector, "collect", lambda self: mock_payload)

    binding = FakeCtpBinding()
    settings = make_settings(
        terminal_mode="relay",
        terminal_public_ip="112.65.19.116",
        terminal_ip_port=32205,
    )
    gateway, sink, _ = make_gateway(binding=binding, settings=settings)

    session = gateway.connect()
    assert session.terminal_info["access_mode"] == "relay"

    api = binding.created[0]
    call_names = [call[0] for call in api.calls]
    assert "ReqAuthenticate" in call_names
    assert "RegisterUserSystemInfo" in call_names
    assert "ReqUserLogin" in call_names

    # 必须保证认证 -> 注册终端信息 -> 登录的严格调用顺序 (A27)
    auth_idx = call_names.index("ReqAuthenticate")
    reg_idx = call_names.index("RegisterUserSystemInfo")
    login_idx = call_names.index("ReqUserLogin")
    assert auth_idx < reg_idx < login_idx

    reg_field = [call[1] for call in api.calls if call[0] == "RegisterUserSystemInfo"][0]
    assert reg_field.ClientPublicIP == "112.65.19.116"
    assert reg_field.ClientIPPort == 32205
    assert reg_field.ClientSystemInfoLen == 100


def test_gateway_relay_mode_registration_failure_blocks_login(monkeypatch):
    """中继模式下 RegisterUserSystemInfo 返回非零时中断握手且阻止就绪 (A27, F17)."""
    mock_report = TerminalCollectorReport(
        collector_dll="WinDataCollect.dll",
        dll_hash="e" * 64,
        api_version="sfit_test_1.0",
        system_info_len=100,
        status_code=0,
        is_complete=True,
        collected_at="2026-09-28T00:00:00Z",
        masked_digest="sha256:relay1234567890",
        access_mode="relay",
    )
    mock_payload = TerminalInfoPayload(
        raw_bytes=b"Y" * 100,
        length=100,
        report=mock_report,
    )

    monkeypatch.setattr(TerminalInfoCollector, "collect", lambda self: mock_payload)

    binding = FakeCtpBinding()
    binding.user_system_info_code = -1  # 模拟注册被柜台拒绝
    settings = make_settings(terminal_mode="relay")
    gateway, sink, _ = make_gateway(binding=binding, settings=settings)

    with pytest.raises(CtpHandshakeError, match="RegisterUserSystemInfo failed with return code -1"):
        gateway.connect()

    assert gateway.ready_to_send is False
    assert gateway.fault == "register_user_system_info_failed"
    api = binding.created[0]
    call_names = [call[0] for call in api.calls]
    assert "ReqUserLogin" not in call_names  # 绝不能进入登录步骤 (A27)
