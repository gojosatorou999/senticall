"""PII redaction is belt-and-braces on top of the 'never log raw msisdn'
rule. If someone accidentally logs a phone number, the redaction
processor must scrub it before it hits stdout."""

from __future__ import annotations

import io
import json
import logging

import structlog

from fg_voice.obs.logging import configure_logging


def _capture_json_log(log_fn) -> dict:  # type: ignore[no-untyped-def]
    """Capture one structlog JSON record."""
    buf = io.StringIO()
    root = logging.getLogger()
    handler = logging.StreamHandler(buf)
    root.addHandler(handler)
    try:
        log_fn()
        for h in root.handlers:
            h.flush()
    finally:
        root.removeHandler(handler)
    # Structlog's PrintLoggerFactory writes to sys.stdout directly; the
    # handler above is a safety net. We assert on either sink.
    line = buf.getvalue().strip().splitlines()[-1] if buf.getvalue().strip() else ""
    return json.loads(line) if line else {}


def test_phone_number_redacted(capsys) -> None:  # type: ignore[no-untyped-def]
    configure_logging(level="INFO")
    log = structlog.get_logger("test")
    log.info("call", caller="+919876543210", note="reached +919876543210")
    captured = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(captured)
    assert "9876543210" not in payload["caller"]
    assert "<redacted:phone>" in payload["caller"]
    assert "<redacted:phone>" in payload["note"]


def test_aadhaar_like_redacted(capsys) -> None:  # type: ignore[no-untyped-def]
    configure_logging(level="INFO")
    log = structlog.get_logger("test")
    log.info("id", value="1234 5678 9012")
    captured = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(captured)
    assert "1234 5678 9012" not in payload["value"]
    assert "<redacted:id>" in payload["value"]


def test_non_string_values_pass_through(capsys) -> None:  # type: ignore[no-untyped-def]
    configure_logging(level="INFO")
    log = structlog.get_logger("test")
    log.info("metrics", count=42, ratio=0.87)
    captured = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(captured)
    assert payload["count"] == 42
    assert payload["ratio"] == 0.87
