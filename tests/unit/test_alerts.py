"""AlertDispatcher — filter logic + backend fan-out semantics."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from fg_voice.persistence.alerts import (
    AlertDeliveryError,
    AlertDispatcher,
    LogAlertBackend,
    WebhookAlertBackend,
)
from fg_voice.persistence.models import OutboxEntry


@dataclass(slots=True)
class _RecordingBackend:
    """Test double — records every alert without raising."""

    sent: list[dict[str, Any]] = field(default_factory=list)
    raise_next: bool = False
    next_error: str = "backend boom"

    async def send(self, alert: dict[str, Any]) -> None:
        if self.raise_next:
            self.raise_next = False
            raise RuntimeError(self.next_error)
        self.sent.append(alert)


def _entry(event_type: str = "report.submitted", **payload_overrides) -> OutboxEntry:
    payload = {
        "report_id": "aaaaaaaa-0000-0000-0000-000000000042",
        "short_ref": "FG-ALRT",
        "call_sid": "CA_alert_001",
        "hazard_type": "abnormal_tide",
        "severity": "moderate",
        "water_depth_cm": 50,
        "location_raw": "Kakinada beach",
        "flags": [],
        **payload_overrides,
    }
    e = OutboxEntry(event_type=event_type, payload=payload)
    e.id = 42
    return e


# ─── Filter behaviour ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ordinary_report_is_not_alerted():
    """Moderate severity, no life_safety flag → the alert dispatcher
    skips it silently. Regular reports still land in CSV + SSE via the
    other dispatchers in the chain."""
    backend = _RecordingBackend()
    dispatcher = AlertDispatcher(backends=[backend])
    await dispatcher.dispatch(_entry())
    assert backend.sent == []


@pytest.mark.asyncio
async def test_life_safety_flag_triggers_alert():
    backend = _RecordingBackend()
    dispatcher = AlertDispatcher(backends=[backend])
    await dispatcher.dispatch(_entry(flags=["life_safety"]))
    assert len(backend.sent) == 1
    alert = backend.sent[0]
    assert alert["life_safety_flag"] is True
    assert alert["trigger"] == "life_safety"
    assert alert["short_ref"] == "FG-ALRT"


@pytest.mark.asyncio
async def test_extreme_severity_triggers_alert():
    backend = _RecordingBackend()
    dispatcher = AlertDispatcher(backends=[backend])
    await dispatcher.dispatch(_entry(severity="extreme"))
    assert len(backend.sent) == 1
    assert backend.sent[0]["severity"] == "extreme"
    assert backend.sent[0]["trigger"] == "severity_extreme"


@pytest.mark.asyncio
async def test_life_safety_takes_precedence_in_trigger_label():
    """When BOTH triggers match, the trigger label surfaces the more
    serious one so ops SLAs page on the right cause."""
    backend = _RecordingBackend()
    dispatcher = AlertDispatcher(backends=[backend])
    await dispatcher.dispatch(_entry(severity="extreme", flags=["life_safety"]))
    assert backend.sent[0]["trigger"] == "life_safety"
    assert backend.sent[0]["life_safety_flag"] is True


@pytest.mark.asyncio
async def test_flags_as_dict_still_read_correctly():
    """Retry-shape safety — flags stored as {life_safety: true} dict
    (from the Report row) must trigger the same as list form."""
    backend = _RecordingBackend()
    dispatcher = AlertDispatcher(backends=[backend])
    await dispatcher.dispatch(_entry(flags={"life_safety": True}))
    assert len(backend.sent) == 1


@pytest.mark.asyncio
async def test_non_report_events_are_skipped():
    """`moderation.applied` or `alert.echo` events must not re-fire
    alerts — that's an infinite-loop risk once we add downstream
    dispatchers that themselves write outbox rows."""
    backend = _RecordingBackend()
    dispatcher = AlertDispatcher(backends=[backend])
    entry = OutboxEntry(
        event_type="moderation.applied",
        payload={"severity": "extreme", "flags": ["life_safety"]},
    )
    entry.id = 99
    await dispatcher.dispatch(entry)
    assert backend.sent == []


# ─── Fan-out semantics ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_all_backends_receive_the_alert():
    b1 = _RecordingBackend()
    b2 = _RecordingBackend()
    b3 = _RecordingBackend()
    dispatcher = AlertDispatcher(backends=[b1, b2, b3])
    await dispatcher.dispatch(_entry(severity="extreme"))
    assert len(b1.sent) == 1
    assert len(b2.sent) == 1
    assert len(b3.sent) == 1
    # Same payload delivered to each.
    assert b1.sent[0] == b2.sent[0] == b3.sent[0]


@pytest.mark.asyncio
async def test_one_backend_failure_does_not_stop_the_others():
    """A failing backend must not swallow the alert on the successful
    ones. The failure surfaces at the end so the relay retries the
    row — but the successful backends already delivered."""
    ok = _RecordingBackend()
    bad = _RecordingBackend()
    bad.raise_next = True
    bad.next_error = "downstream 500"

    dispatcher = AlertDispatcher(backends=[bad, ok])
    with pytest.raises(AlertDeliveryError, match="downstream 500"):
        await dispatcher.dispatch(_entry(severity="extreme"))

    # The successful backend still got the alert.
    assert len(ok.sent) == 1


@pytest.mark.asyncio
async def test_all_backends_ok_no_exception():
    dispatcher = AlertDispatcher(backends=[_RecordingBackend(), _RecordingBackend()])
    await dispatcher.dispatch(_entry(severity="extreme"))  # must not raise


# ─── Log backend ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_backend_never_raises():
    """LogAlertBackend is the safe default. It has to be crash-proof
    or the whole chain breaks when the log system misbehaves."""
    backend = LogAlertBackend()
    # Empty alert should not throw.
    await backend.send({})
    # Non-string values should not throw.
    await backend.send({"severity": None, "short_ref": None, "life_safety_flag": False})


# ─── Webhook backend ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_webhook_posts_json_body_to_url():
    """Uses httpx's MockTransport so no real network call fires."""
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = request.content
        captured["content_type"] = request.headers.get("content-type", "")
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(_handler)

    # Patch the AsyncClient constructor inside alerts.py so it uses
    # our mock transport. Cleaner than exposing a factory hook.
    import fg_voice.persistence.alerts as alerts_mod

    _orig_client = alerts_mod.httpx.AsyncClient

    def _fake_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return _orig_client(*args, **kwargs)

    alerts_mod.httpx.AsyncClient = _fake_client  # type: ignore[misc]
    try:
        backend = WebhookAlertBackend(url="https://hooks.example.com/pager")
        await backend.send(
            {
                "short_ref": "FG-Z1",
                "severity": "extreme",
                "hazard_type": "storm",
            }
        )
    finally:
        alerts_mod.httpx.AsyncClient = _orig_client  # type: ignore[misc]

    assert captured["method"] == "POST"
    assert captured["url"] == "https://hooks.example.com/pager"
    assert b'"short_ref"' in captured["body"]
    assert b'"severity":"extreme"' in captured["body"]
    assert captured["content_type"].startswith("application/json")


@pytest.mark.asyncio
async def test_webhook_5xx_raises_so_relay_retries():
    """A downstream 5xx MUST bubble up as an exception so the outbox
    row's retry_count bumps rather than the alert being silently lost."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="try again")

    transport = httpx.MockTransport(_handler)

    import fg_voice.persistence.alerts as alerts_mod

    _orig_client = alerts_mod.httpx.AsyncClient

    def _fake_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return _orig_client(*args, **kwargs)

    alerts_mod.httpx.AsyncClient = _fake_client  # type: ignore[misc]
    try:
        backend = WebhookAlertBackend(url="https://hooks.example.com/pager")
        with pytest.raises(httpx.HTTPStatusError):
            await backend.send({"short_ref": "FG-BOOM"})
    finally:
        alerts_mod.httpx.AsyncClient = _orig_client  # type: ignore[misc]


# ─── End-to-end via the relay ────────────────────────────────────────


@pytest.mark.asyncio
async def test_end_to_end_relay_fires_alert_only_on_severity_extreme(tmp_path):
    """Two rows land in the outbox: one moderate (no alert), one
    extreme (alert). Drain → the recording backend sees exactly one."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from fg_voice.conversation.sql_report_sink import SqlReportSink
    from fg_voice.conversation.state import CallState, Slot, SlotValue
    from fg_voice.persistence.db import Base, override_engine, reset_engine
    from fg_voice.persistence.relay import OutboxRelay

    eng = create_async_engine("sqlite+aiosqlite:///:memory:", pool_pre_ping=False)
    override_engine(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        sink = SqlReportSink()

        moderate = CallState(call_sid="CA_e2e_alert_ok", caller_hash="h")
        moderate.set_slot(Slot.HAZARD_TYPE, SlotValue(value="storm", confidence=0.9, source="asr"))
        moderate.set_slot(Slot.SEVERITY, SlotValue(value="moderate", confidence=0.9, source="asr"))
        await sink.write(moderate)

        extreme = CallState(call_sid="CA_e2e_alert_hit", caller_hash="h")
        extreme.set_slot(Slot.HAZARD_TYPE, SlotValue(value="storm", confidence=0.9, source="asr"))
        extreme.set_slot(Slot.SEVERITY, SlotValue(value="extreme", confidence=0.9, source="asr"))
        await sink.write(extreme)

        backend = _RecordingBackend()
        relay = OutboxRelay(dispatcher=AlertDispatcher(backends=[backend]))
        await relay.drain_once()

        assert len(backend.sent) == 1
        assert backend.sent[0]["severity"] == "extreme"
        assert backend.sent[0]["call_sid"] == "CA_e2e_alert_hit"
    finally:
        await eng.dispose()
        reset_engine()
