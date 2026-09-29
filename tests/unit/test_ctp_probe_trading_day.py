"""夜盘显式交易日与柜台相符时放行探测，不靠自然日相等判断休市。"""

from datetime import datetime, timezone

from scripts import ctp_probe
from tests.unit.test_ctp_live_assembly_and_probe import (
    DAY,
    SYMBOL,
    build_probe_binding,
    install_fake_counter,
    probe_arguments,
)


class NightClock(datetime):
    @classmethod
    def now(cls, tz=None):
        # 假柜台交易日 2024-09-10，实际夜盘自然日 2024-09-09。
        instant = datetime(2024, 9, 9, 13, 30, tzinfo=timezone.utc)
        return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)


def test_explicit_night_trading_day_allows_order_round_trip_without_legacy_override(monkeypatch):
    binding = build_probe_binding()
    install_fake_counter(monkeypatch, binding)
    monkeypatch.setattr(ctp_probe, "datetime", NightClock)
    args = ctp_probe.build_parser().parse_args(
        probe_arguments("runs/pytest", "--order-symbol", SYMBOL, "--expected-trading-day", DAY.isoformat())
    )
    args.account = args.account or "test-night-account"
    report, code = ctp_probe.run_probe(args)
    assert code == 0
    assert len(binding.api.insert_fields) == len(binding.api.action_fields) == 1
    assert "trading_day_warning" not in report
    step = next(item for item in report["steps"] if item["name"] == "counter_trading_day")
    assert step["detail"]["matches_local_date"] is False
    assert step["detail"]["matches_expected_trading_day"] is True


def test_explicit_mismatch_cannot_be_bypassed_by_legacy_allow_flag(monkeypatch):
    binding = build_probe_binding()
    install_fake_counter(monkeypatch, binding)
    args = ctp_probe.build_parser().parse_args(
        probe_arguments(
            "runs/pytest", "--order-symbol", SYMBOL, "--expected-trading-day", "2024-09-11", "--allow-non-trading-day"
        )
    )
    args.account = args.account or "test-night-account"
    report, code = ctp_probe.run_probe(args)
    assert code == 2
    assert binding.api.insert_fields == [] and binding.api.action_fields == []
    step = next(item for item in report["steps"] if item["name"] == "counter_trading_day")
    assert step["status"] == "failed"
