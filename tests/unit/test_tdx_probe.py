"""[测试工具] TDX 证据边界、截断拒绝与夜盘归属的离线检查 (S1-12, GAP-TDX-01)."""

from __future__ import annotations

import json
from datetime import date, datetime, time
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from qh_trader.core.constants import Exchange, MarketPhase
from qh_trader.core.objects import InstrumentId
from scripts import init_env, tdx_probe


def test_evidence_path_rejects_escape_before_network(tmp_path, monkeypatch):
    monkeypatch.setattr(tdx_probe, "ROOT", tmp_path)
    monkeypatch.setattr(tdx_probe, "run_probe", lambda args: pytest.fail("路径拒绝前不应联网"))
    assert tdx_probe.main(["--output", "runs/s0/../../secret.json"]) == 2
    with pytest.raises(ValueError, match="runs/s0"):
        tdx_probe.evidence_target(tmp_path, str(tmp_path.parent / "elsewhere.json"))
    assert (
        tdx_probe.evidence_target(tmp_path, "runs/s0/nested/evidence.json")
        == (tmp_path / "runs/s0/nested/evidence.json").resolve()
    )


def test_pagination_does_not_claim_short_page_is_complete():
    rows, depth = tdx_probe.collect_pages(lambda start, count: [{"value": start}], max_pages=1, count=700)
    assert rows == [{"value": 0}]
    assert depth["complete"] is False
    assert depth["termination"] == "page_limit"
    with pytest.raises(ValueError, match="分页重复"):
        tdx_probe.collect_pages(lambda start, count: [{"value": 1}], max_pages=2, count=700)


def test_missing_and_empty_fields_are_not_verified():
    assert tdx_probe._fields([])["status"] == "unavailable"
    assert tdx_probe._fields([{"volume": 1, "open_interest": 2}])["status"] == "failed"
    assert tdx_probe._fields([{"volume": 1, "open_interest": 2, "turnover": 0}])["status"] == "failed"


def _report():
    return {
        "schema_version": 1,
        "kind": "tdx_runtime_probe",
        "source_id": "tdx_exhq",
        "generated_at": "2026-10-06T00:00:00+00:00",
        "scope": "research_only",
        "checks": {"comparison": {"status": "unavailable"}},
    }


def test_init_env_attaches_failed_or_partial_research_evidence_without_closing_gaps(tmp_path):
    path = tmp_path / "runs/s0/probe.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_report()), encoding="utf-8")
    ref = init_env.tdx_evidence_reference(tmp_path, "runs/s0/probe.json")
    assert ref["path"] == "runs/s0/probe.json"
    assert ref["sha256"] == init_env.get_file_hash(path)
    assert ref["closes_gaps"] is False


@pytest.mark.parametrize("changes", [{"kind": "ctp_runtime_probe"}, {"schema_version": True}, {"scope": "live"}])
def test_init_env_rejects_wrong_evidence_type(tmp_path, changes):
    path = tmp_path / "runs/s0/probe.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({**_report(), **changes}), encoding="utf-8")
    with pytest.raises(ValueError, match="类型或版本"):
        init_env.tdx_evidence_reference(tmp_path, path)


def test_init_env_rejects_project_file_outside_s0(tmp_path):
    path = tmp_path / "probe.json"
    path.write_text(json.dumps(_report()), encoding="utf-8")
    with pytest.raises(ValueError, match="runs/s0"):
        init_env.tdx_evidence_reference(tmp_path, path)


class UnreachableClient:
    def __init__(self, *args, **kwargs):
        pass

    def connect(self):
        raise ConnectionError("synthetic offline failure")

    def close(self):
        pass


def test_connection_failure_is_archived_as_unavailable_without_fabricated_history():
    args = tdx_probe.build_parser().parse_args([])
    report = tdx_probe.run_probe(args, client_factory=UnreachableClient)
    assert report["handshake_success_rate"] == 0
    assert report["nodes"][0]["status"] == "failed"
    assert report["checks"]["comparison"]["status"] == "unavailable"
    assert report["checks"]["history"]["status"] == "unavailable"
    assert report["wire_capture"]["frames"] == []


def comparison_fixture():
    tz = ZoneInfo("Asia/Shanghai")
    day = date(2026, 9, 30)
    sessions = [
        SimpleNamespace(
            start=datetime(2026, 9, 29, 21, 0, tzinfo=tz),
            end=datetime(2026, 9, 29, 21, 5, tzinfo=tz),
            phase=MarketPhase.CONTINUOUS,
        ),
        SimpleNamespace(
            start=datetime(2026, 9, 30, 9, 0, tzinfo=tz),
            end=datetime(2026, 9, 30, 9, 5, tzinfo=tz),
            phase=MarketPhase.CONTINUOUS,
        ),
    ]
    calendar = SimpleNamespace(
        sessions_for_day=lambda instrument, trading_day: sessions,
        version="independent-night-fixture",
        source_id="offline-manual-fixture",
    )
    daily = [{"datetime": datetime(2026, 9, 30), "volume": 30, "open_interest": 40, "price": Decimal(101)}]
    minutes = [
        {"datetime": datetime(2026, 9, 29, 21, 5), "volume": 10, "open_interest": 35},
        {"datetime": datetime(2026, 9, 30, 9, 5), "volume": 20, "open_interest": 40},
    ]
    trades = [
        {"time": time(21, 3), "volume": 10, "price": Decimal(100)},
        {"time": time(9, 3), "volume": 20, "price": Decimal(102)},
    ]
    client = SimpleNamespace(
        get_instrument_bars=lambda category, market, code, start, count: (
            [] if start else daily if category == 4 else minutes
        ),
        get_history_transaction_data=lambda market, code, target_date, start, count: [] if start else trades,
    )
    return client, {
        "symbol": "SHFE.RB2701",
        "instrument": InstrumentId(Exchange.SHFE, "rb2701"),
        "trading_day": day,
        "calendar": calendar,
        "max_pages": 2,
        "count": 700,
        "price_tick": Decimal(1),
    }


def test_three_way_comparison_assigns_previous_night_by_explicit_sessions():
    client, kwargs = comparison_fixture()
    report = tdx_probe.compare_trading_day(client, **kwargs)
    assert report["volume"] == {"status": "passed", "daily": 30, "five_minute_total": 30, "transaction_total": 30}
    assert report["open_interest"]["status"] == "passed"
    assert report["settlement_proxy"]["within_one_tick"] is True
    assert report["settlement_proxy"]["official_settlement"] is False
    assert report["settlement_proxy"]["vwap_minus_proxy"] == Decimal(3040) / 30 - 101


def test_equal_sample_totals_cannot_verify_truncated_transactions():
    client, kwargs = comparison_fixture()
    kwargs["max_pages"] = 1
    report = tdx_probe.compare_trading_day(client, **kwargs)
    assert report["status"] == "unavailable"
    assert report["volume"]["status"] == "unavailable"
    assert report["settlement_proxy"]["status"] == "unavailable"
