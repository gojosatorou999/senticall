"""Structured JSON logging with PII redaction.

Every log record is a JSON object. In production the sink is CloudWatch
Logs → OTel; locally it's stdout. The redaction processor scrubs anything
that looks like a phone number or Aadhaar-like ID before serialization —
belt-and-braces on top of the "never log raw msisdn" rule in the code."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, cast

import structlog

_PHONE_RE = re.compile(r"\+?\d{10,13}")
_AADHAAR_RE = re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")


def _redact_pii(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """Structlog processor: mask phone/Aadhaar-like strings in string values."""
    for k, v in list(event_dict.items()):
        if not isinstance(v, str):
            continue
        v = _PHONE_RE.sub("<redacted:phone>", v)
        v = _AADHAAR_RE.sub("<redacted:id>", v)
        event_dict[k] = v
    return event_dict


class _Tee:
    """Fan one write to several streams.

    structlog's PrintLoggerFactory takes a single file object, so teeing is
    how stdout keeps working while a durable copy lands on disk. Without
    the file copy, logs only exist in whatever terminal launched the
    process — which makes a dropped call impossible to diagnose after the
    fact."""

    def __init__(self, *streams: Any) -> None:
        self._streams = streams

    def write(self, data: str) -> None:
        for stream in self._streams:
            try:
                stream.write(data)
            except Exception:  # noqa: BLE001 - a broken sink must not kill logging
                pass

    def flush(self) -> None:
        for stream in self._streams:
            try:
                stream.flush()
            except Exception:  # noqa: BLE001
                pass


def configure_logging(
    level: str = "INFO",
    service: str = "fg_voice",
    log_file: str | None = None,
) -> None:
    """Idempotent. Call once at process start.

    `log_file` (FG_LOG_FILE) appends a copy of every record to disk in
    addition to stdout. Off by default; production ships stdout to
    CloudWatch and has no use for a local file."""
    sink: Any = sys.stdout
    if log_file:
        try:
            path = Path(log_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a", encoding="utf-8", buffering=1)
            sink = _Tee(sys.stdout, handle)
        except OSError:
            # An unwritable path must not stop the service from booting.
            sink = sys.stdout

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
        force=True,
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_pii,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=structlog.PrintLoggerFactory(file=sink),
        cache_logger_on_first_use=True,
    )
    # bind service-wide context
    structlog.contextvars.bind_contextvars(service=service)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))
