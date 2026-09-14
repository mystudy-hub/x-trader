"""Structured, redacted logging and thread-safe in-process metrics for application entry points."""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import re
import sys
import threading
import time
import traceback
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import quote, quote_plus

from qh_trader.infrastructure.private_fields import is_private_field, redact_private_assignments

CORRELATION_FIELDS = (
    "account_alias",
    "strategy_id",
    "controller_id",
    "control_epoch",
    "client_order_id",
    "exchange_order_id",
    "front_id",
    "session_id",
    "order_ref",
    "journal_seq",
    "trading_day",
    "rule_version",
)
_CONTEXT: ContextVar[Mapping[str, Any] | None] = ContextVar("qh_log_context", default=None)
_BUILTIN_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
_RESERVED = {"schema_version", "timestamp", "level", "logger", "event", "exception"}
_LOG_PRIVATE = frozenset({"accountid", "investorid", "userid", "username", "账号", "账户号"})
_OCTET = r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"
_IPV4 = re.compile(r"(?<![\w.])" + _OCTET + r"(?:\." + _OCTET + r"){3}(?![\w.])")
_IPV6 = re.compile(r"(?<![\w:])(?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}(?:%[\w.-]+)?(?![\w:])")
_MAC = re.compile(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f])")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_HEADERS = re.compile(r"""(?im)\b(authorization|proxy[-_ ]authorization|cookie|set-cookie)["']?\s*:\s*[^\r\n]+""")
_USERINFO = re.compile(r"(?i)(\b(?:https?|tcp|ssl)://)[^/\s@]+@")
_USER_PATH = re.compile(r"(?i)([A-Z]:[\\/]Users[\\/])[^\\/\s]+")
_METRIC_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.:]*$")


class LogSinkError(OSError):
    """The log sink failed; callers must treat observability as unavailable."""


class Redactor:
    def __init__(
        self, secret_values: Sequence[str] = (), *, path_root: Path | None = None, max_text: int = 8192
    ) -> None:
        secret_values = tuple(secret_values)
        if any(not isinstance(value, str) or not value for value in secret_values):
            raise ValueError("registered secrets must be nonempty strings")
        if max_text < 64:
            raise ValueError("max_text must allow a useful diagnostic message")
        values = {variant for value in secret_values for variant in (value, quote(value, safe=""), quote_plus(value))}
        self._secrets = tuple(sorted(values, key=len, reverse=True))
        self.path_root = path_root.resolve() if path_root is not None else None
        self.max_text = max_text

    def text(self, value: str) -> str:
        for secret in self._secrets:
            value = value.replace(secret, "[REDACTED]")
        value = _HEADERS.sub(lambda match: match.group(1) + ": [REDACTED]", value)
        value = _BEARER.sub("Bearer [REDACTED]", value)
        value = _USERINFO.sub(lambda match: match.group(1) + "[REDACTED]@", value)
        value = redact_private_assignments(value, extra_private=_LOG_PRIVATE)
        value = _IPV4.sub("[REDACTED_IP]", value)

        def ipv6(match):
            try:
                ipaddress.IPv6Address(match.group(0))
            except ValueError:
                return match.group(0)
            return "[REDACTED_IP]"

        value = _IPV6.sub(ipv6, value)
        value = _MAC.sub("[REDACTED_MAC]", value)
        value = _USER_PATH.sub(lambda match: match.group(1) + "[REDACTED_USER]", value)
        return value if len(value) <= self.max_text else value[: self.max_text] + "[TRUNCATED]"

    def clean(self, value: Any, *, _seen: frozenset[int] = frozenset(), _depth: int = 0) -> Any:
        if _depth > 32:
            return "[MAX_DEPTH]"
        if value is None or type(value) in (bool, int):
            return value
        if isinstance(value, Enum):
            return self.clean(value.value)
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Decimal):
            return str(value) if value.is_finite() else "[NON_FINITE]"
        if isinstance(value, float):
            return value if math.isfinite(value) else "[NON_FINITE]"
        if isinstance(value, (bytes, bytearray, memoryview)):
            return "[REDACTED_BINARY]"
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat() if value.tzinfo is not None else "[NAIVE_TIMESTAMP]"
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, Path):
            path = value.resolve()
            text = (
                path.relative_to(self.path_root).as_posix()
                if self.path_root and path.is_relative_to(self.path_root)
                else path.name
            )
            return self.text(text)
        if isinstance(value, BaseException):
            try:
                message = self.text(str(value))
            except Exception:
                message = "[UNPRINTABLE]"
            return {"type": type(value).__name__, "message": message}
        if id(value) in _seen:
            return "[CYCLE]"
        seen = _seen | {id(value)}
        if is_dataclass(value) and not isinstance(value, type):
            value = {field.name: getattr(value, field.name) for field in fields(value)}
        if isinstance(value, Mapping):
            result = {}
            for key, item in value.items():
                name = key if isinstance(key, str) else f"<{type(key).__name__}>"
                normalized = re.sub(r"[^a-z0-9]", "", name.casefold())
                result[self.text(name)] = (
                    "[REDACTED]"
                    if is_private_field(name) or normalized in _LOG_PRIVATE
                    else self.clean(item, _seen=seen, _depth=_depth + 1)
                )
            return result
        if isinstance(value, (list, tuple, set, frozenset)):
            items = [self.clean(item, _seen=seen, _depth=_depth + 1) for item in value]
            return tuple(items) if isinstance(value, tuple) else items
        return f"<{type(value).__name__}>"


@contextmanager
def log_context(**values: Any):
    if set(values) & _RESERVED:
        raise ValueError("log context cannot override record identity")
    token = _CONTEXT.set({**(_CONTEXT.get() or {}), **values})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


class JSONFormatter(logging.Formatter):
    def __init__(self, redactor: Redactor | None = None, *, context: Mapping[str, Any] | None = None) -> None:
        super().__init__()
        self.redactor = redactor or Redactor()
        self.context = dict(context or {})

    def format(self, record: logging.LogRecord) -> str:
        clean = self.redactor.clean
        template = record.msg if isinstance(record.msg, str) else json.dumps(clean(record.msg), ensure_ascii=False)
        try:
            message = template % clean(record.args) if record.args else template
        except (TypeError, ValueError, KeyError):
            message = template + " [FORMAT_ERROR]"
        extra = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _BUILTIN_RECORD_FIELDS and key not in _RESERVED
        }
        context = {**dict.fromkeys(CORRELATION_FIELDS), **self.context, **(_CONTEXT.get() or {}), **extra}
        data = clean(context)
        data.update(
            schema_version=1,
            timestamp=datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            level=record.levelname,
            logger=self.redactor.text(record.name),
            event=self.redactor.text(message),
        )
        if record.exc_info and record.exc_info[1] is not None:
            data["exception"] = clean(record.exc_info[1])
            data["exception"]["frames"] = [
                {
                    "file": self.redactor.text(Path(frame.filename).name),
                    "line": frame.lineno,
                    "function": self.redactor.text(frame.name),
                }
                for frame in traceback.extract_tb(record.exc_info[2])
            ]
        return json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


@dataclass
class LoggingHealth:
    failures: int = 0
    last_error_type: str | None = None

    @property
    def ready(self) -> bool:
        return self.failures == 0


class JSONLogHandler(logging.Handler):
    def __init__(self, stream: TextIO, formatter: JSONFormatter, *, owns_stream: bool = False) -> None:
        super().__init__()
        self.stream = stream
        self.owns_stream = owns_stream
        self.health = LoggingHealth()
        self.setFormatter(formatter)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.stream.write(self.format(record) + "\n")
            self.stream.flush()
        except Exception as exc:
            self.health.failures += 1
            self.health.last_error_type = type(exc).__name__
            raise LogSinkError("structured log sink failed; observability is not ready") from None

    def close(self) -> None:
        if self.owns_stream:
            self.stream.close()
        super().close()


class LoggingSession:
    def __init__(self, logger: logging.Logger, handler: JSONLogHandler, level: int) -> None:
        self.logger, self.handler = logger, handler
        self.previous_handlers, self.previous_level = list(logger.handlers), logger.level
        self.previous_propagate = logger.propagate
        logger.handlers = [handler]
        logger.setLevel(level)
        logger.propagate = False
        self.closed = False

    def __enter__(self) -> LoggingSession:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        if not self.closed:
            self.logger.handlers = self.previous_handlers
            self.logger.setLevel(self.previous_level)
            self.logger.propagate = self.previous_propagate
            self.handler.close()
            self.closed = True


def configure_logging(
    *,
    stream: TextIO | None = None,
    file: Path | None = None,
    context: Mapping[str, Any] | None = None,
    secret_values: Sequence[str] = (),
    path_root: Path | None = None,
    logger: logging.Logger | None = None,
    level: int = logging.INFO,
) -> LoggingSession:
    if stream is not None and file is not None:
        raise ValueError("choose one structured log destination")
    redactor = Redactor(secret_values, path_root=path_root)
    if file is not None:
        file.parent.mkdir(parents=True, exist_ok=True)
        stream = file.open("a", encoding="utf-8")
    handler = JSONLogHandler(
        stream if stream is not None else sys.stderr,
        JSONFormatter(redactor, context=context),
        owns_stream=file is not None,
    )
    return LoggingSession(logger if logger is not None else logging.getLogger(), handler, level)


class MetricsRegistry:
    """Counters, gauges and duration summaries. Raw account/order identifiers are excluded from labels."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._metrics: dict[tuple[str, tuple], dict[str, Any]] = {}
        self._redactor = Redactor()

    def _key(self, name: str, labels: Mapping[str, str] | None) -> tuple[str, tuple]:
        if not _METRIC_NAME.fullmatch(name):
            raise ValueError("invalid metric name")
        pairs = []
        for key, value in (labels or {}).items():
            if not isinstance(key, str):
                raise ValueError("metric label names must be strings")
            normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
            if (
                is_private_field(key)
                or normalized in _LOG_PRIVATE
                or normalized in {"clientorderid", "exchangeorderid"}
                or not isinstance(value, str)
                or self._redactor.text(value) != value
            ):
                raise ValueError("private or high-cardinality identifiers cannot be metric labels")
            pairs.append((key, value))
        return name, tuple(sorted(pairs))

    def _entry(self, key, kind):
        entry = self._metrics.setdefault(
            key, {"kind": kind, "value": 0, "count": 0, "sum": 0.0, "min": None, "max": None}
        )
        if entry["kind"] != kind:
            raise ValueError("metric kind cannot change")
        return entry

    @staticmethod
    def _number(value, *, nonnegative=False):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or (nonnegative and value < 0)
        ):
            raise ValueError("metric values must be finite numbers within their domain")
        return value

    def increment(self, name: str, amount: int | float = 1, *, labels: Mapping[str, str] | None = None) -> None:
        amount = self._number(amount, nonnegative=True)
        key = self._key(name, labels)
        with self._lock:
            entry = self._entry(key, "counter")
            entry["value"] = self._number(entry["value"] + amount, nonnegative=True)

    def gauge(self, name: str, value: int | float, *, labels: Mapping[str, str] | None = None) -> None:
        value = self._number(value)
        key = self._key(name, labels)
        with self._lock:
            self._entry(key, "gauge")["value"] = value

    def observe(self, name: str, value: int | float, *, labels: Mapping[str, str] | None = None) -> None:
        value = self._number(value, nonnegative=True)
        key = self._key(name, labels)
        with self._lock:
            entry = self._entry(key, "summary")
            total = self._number(entry["sum"] + value, nonnegative=True)
            entry["count"] += 1
            entry["sum"] = total
            entry["min"] = value if entry["min"] is None else min(entry["min"], value)
            entry["max"] = value if entry["max"] is None else max(entry["max"], value)

    @contextmanager
    def timer(self, name: str, *, labels: Mapping[str, str] | None = None):
        labels = dict(labels or {})
        key = self._key(name, labels)
        with self._lock:
            self._entry(key, "summary")
        started = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, time.perf_counter() - started, labels=labels)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"name": name, "labels": dict(labels), **dict(value)}
                for (name, labels), value in sorted(self._metrics.items())
            ]
