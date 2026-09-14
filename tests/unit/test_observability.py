"""Redaction at the final log boundary, scoped correlation and deterministic local metrics."""

import io
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from qh_trader.infrastructure.observability import (
    CORRELATION_FIELDS,
    JSONFormatter,
    LogSinkError,
    MetricsRegistry,
    Redactor,
    configure_logging,
    log_context,
)


def test_required_correlation_fields_and_nested_context_are_restored():
    output = io.StringIO()
    logger = logging.getLogger("observability.context-test")
    with configure_logging(stream=output, logger=logger, context={"account_alias": "demo"}):
        with log_context(
            strategy_id="trend",
            controller_id="controller-a",
            control_epoch=2,
            client_order_id="order-1",
            exchange_order_id="remote-1",
            journal_seq=7,
            trading_day=date(2024, 9, 9),
            rule_version="v1",
        ):
            logger.info("committed")
            with log_context(journal_seq=8):
                logger.info("nested")
            logger.info("restored")
        logger.info("outside")
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert all(set(CORRELATION_FIELDS) <= set(row) for row in rows)
    assert [row["journal_seq"] for row in rows] == [7, 8, 7, None]
    assert rows[0]["account_alias"] == "demo" and rows[0]["control_epoch"] == 2
    assert rows[0]["trading_day"] == "2024-09-09"
    assert datetime.fromisoformat(rows[0]["timestamp"]).tzinfo is not None


def test_structured_and_formatted_credentials_terminal_fields_and_identifiers_are_redacted():
    output = io.StringIO()
    logger = logging.getLogger("observability.redaction-test")
    with configure_logging(stream=output, logger=logger):
        logger.info(
            "AuthCode=%s, address=%s, MAC=%s",
            "hidden-auth",
            "192.168.20.30",
            "aa:bb:cc:dd:ee:ff",
            extra={
                "account_id": "123456789",
                "nested": {"Password": "hidden-password", "密钥": "秘密值"},
                "terminal_payload": b"hidden-payload",
            },
        )
        logger.info("request %s", {"url": "https://user:pass@example.invalid", "token": "hidden-token"})
        logger.info("密码='带 空格的秘密' endpoint=[fe80::abcd]")
        logger.info("account_id=%s", "998877665")
    text = output.getvalue()
    for private in (
        "hidden-auth",
        "192.168.20.30",
        "aa:bb:cc:dd:ee:ff",
        "123456789",
        "hidden-password",
        "秘密值",
        "hidden-payload",
        "hidden-token",
        "user:pass",
        "带 空格的秘密",
        "fe80::abcd",
        "998877665",
    ):
        assert private not in text
    assert "[REDACTED" in text
    for line in text.splitlines():
        json.loads(line)


@pytest.mark.parametrize("address", ["::1", "fe80::abcd", "2001:db8::1234", "ab:cd:ef:01:02:03:04:05"])
def test_ipv6_addresses_are_fully_redacted(address):
    assert Redactor().text("endpoint=[" + address + "]") == "endpoint=[[REDACTED_IP]]"


def test_registered_secrets_and_exception_messages_are_sanitized():
    output = io.StringIO()
    logger = logging.getLogger("observability.exception-test")
    with configure_logging(stream=output, logger=logger, secret_values=("raw key/+",)):
        logger.info("plain response %s", "raw key/+")
        logger.info("encoded response raw%20key%2F%2B")
        try:
            raise ValueError("failure raw key/+ token=another-secret")
        except ValueError:
            logger.exception("request failed")
    assert "raw key/+" not in output.getvalue()
    assert "raw%20key%2F%2B" not in output.getvalue()
    assert "another-secret" not in output.getvalue()
    record = json.loads(output.getvalue().splitlines()[-1])
    assert record["exception"]["type"] == "ValueError"
    assert record["exception"]["frames"]
    assert all(set(frame) == {"file", "line", "function"} for frame in record["exception"]["frames"])


def test_format_failure_does_not_dump_unsanitized_arguments_or_unknown_objects():
    class Unsafe:
        def __repr__(self):
            raise AssertionError("unsafe repr was called")

    formatter = JSONFormatter(Redactor(secret_values=("not-for-output",)))
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "broken %d", ("not-for-output",), None)
    assert "not-for-output" not in formatter.format(record)
    opaque = logging.LogRecord("test", logging.INFO, __file__, 1, "object %s", (Unsafe(),), None)
    assert "Unsafe" in formatter.format(opaque)


def test_redaction_occurs_before_truncation_and_json_remains_single_line():
    redactor = Redactor(("secret-value",), max_text=64)
    result = redactor.text("x" * 60 + "secret-value")
    assert "secret" not in result
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "first\nsecond", (), None)
    assert len(JSONFormatter().format(record).splitlines()) == 1


def test_paths_and_nonfinite_values_are_safe(tmp_path):
    redactor = Redactor(path_root=tmp_path)
    clean = redactor.clean(
        {
            "path": tmp_path / "events.log",
            "amount": Decimal("1.2500"),
            "bad": float("nan"),
            "data": b"raw-device-bytes",
            "at": datetime(2024, 1, 1, tzinfo=timezone.utc),
        }
    )
    assert clean["path"] == "events.log"
    assert clean["amount"] == "1.2500"
    assert clean["bad"] == "[NON_FINITE]"
    assert clean["data"] == "[REDACTED_BINARY]"


def test_log_sink_failure_is_explicit_and_marks_health_not_ready():
    class Broken(io.StringIO):
        def write(self, value):
            raise OSError("AuthCode=must-not-leak")

    logger = logging.getLogger("observability.broken-test")
    with configure_logging(stream=Broken(), logger=logger) as session:
        with pytest.raises(LogSinkError) as failure:
            logger.info("an event")
        assert "must-not-leak" not in str(failure.value)
        assert not session.handler.health.ready
        assert session.handler.health.failures == 1


def test_configuration_restores_handlers_and_thread_context_does_not_leak():
    output = io.StringIO()
    logger = logging.getLogger("observability.thread-test")
    previous = list(logger.handlers)
    with configure_logging(stream=output, logger=logger):

        def work(name):
            with log_context(strategy_id=name):
                logger.info("worker")

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(work, ["one", "two"]))
        logger.info("outside")
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert {row["strategy_id"] for row in rows[:2]} == {"one", "two"}
    assert rows[-1]["strategy_id"] is None
    assert logger.handlers == previous


def test_metrics_are_thread_safe_and_preserve_integer_counts():
    metrics = MetricsRegistry()

    def work(_):
        for _ in range(100):
            metrics.increment("events.total", labels={"result": "ok"})

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(work, range(8)))
    assert metrics.snapshot()[0]["value"] == 800
    metrics.increment("large.total", 2**54 + 1)
    assert next(row for row in metrics.snapshot() if row["name"] == "large.total")["value"] == 2**54 + 1
    snapshot = metrics.snapshot()
    snapshot[0]["labels"]["result"] = "changed"
    assert metrics.snapshot()[0]["labels"]["result"] == "ok"


def test_timing_uses_monotonic_clock_and_preserves_operation_errors():
    metrics = MetricsRegistry()
    with patch("qh_trader.infrastructure.observability.time.perf_counter", side_effect=[10.0, 10.5]):
        with pytest.raises(ValueError, match="operation failed"):
            with metrics.timer("operation.seconds"):
                raise ValueError("operation failed")
    row = metrics.snapshot()[0]
    assert row["count"] == 1 and row["sum"] == row["min"] == row["max"] == 0.5


@pytest.mark.parametrize(
    "labels",
    [{"AuthCode": "secret"}, {"account_id": "raw-account"}, {"client_order_id": "order-1"}, {"server": "10.0.0.1"}],
)
def test_private_metric_labels_are_rejected(labels):
    with pytest.raises(ValueError, match="identifiers"):
        MetricsRegistry().increment("requests.total", labels=labels)


@pytest.mark.parametrize("value", [-1, True, float("inf"), float("nan")])
def test_counter_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        MetricsRegistry().increment("requests.total", value)


def test_download_cli_emits_structured_events_and_metrics(tmp_path, monkeypatch, capsys):
    from qh_trader.data.downloader import FuturesDataDownloader
    from scripts.download_data import main

    monkeypatch.setattr(
        FuturesDataDownloader,
        "download_raw",
        lambda *args, **kwargs: SimpleNamespace(
            records=[{}], quality=SimpleNamespace(issues=["missing turnover"]), path=tmp_path / "raw.json"
        ),
    )
    assert main(["--symbols", "SHFE.rb2410", "--intervals", "1d", "--storage-dir", str(tmp_path)]) == 0
    records = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert all(row["component"] == "download_data" for row in records)
    assert any(row.get("mode") == "raw" and row["record_count"] == 1 for row in records)
    assert any("metrics" in row for row in records)
