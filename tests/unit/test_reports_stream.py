"""SSE reports feed + main.py lifespan wiring.

Uses `httpx.AsyncClient` because `TestClient`'s streaming support
doesn't play well with the SSE keepalive pattern."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from fg_voice.api import routes_reports
from fg_voice.persistence.broker import InProcessBroker, ReportEvent

# ─── The generator (unit-level, no HTTP) ─────────────────────────────


class _StubRequest:
    """Tiny stand-in for FastAPI's Request — the generator only calls
    `await request.is_disconnected()`."""

    def __init__(self, disconnect_after: float | None = None) -> None:
        self._disconnected = False
        self._deadline = disconnect_after

    async def is_disconnected(self) -> bool:
        if self._deadline is None:
            return False
        loop = asyncio.get_running_loop()
        if loop.time() >= self._deadline:
            self._disconnected = True
        return self._disconnected


@pytest.mark.asyncio
async def test_event_stream_emits_initial_hello_then_event():
    broker = InProcessBroker()
    req = _StubRequest()
    gen = routes_reports._event_stream(req, broker)

    hello = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert hello.startswith(b":")  # SSE comment

    # Publish, then read the next frame.
    await broker.publish(ReportEvent("report.submitted", {"short_ref": "FG-ABCD"}))
    frame = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert frame.startswith(b"event: report.submitted\n")
    assert b'"short_ref":"FG-ABCD"' in frame

    await gen.aclose()


@pytest.mark.asyncio
async def test_event_stream_ends_on_client_disconnect(monkeypatch):
    """Once `Request.is_disconnected` returns True, the generator
    exits without yielding another frame."""
    monkeypatch.setattr(routes_reports, "KEEPALIVE_INTERVAL_SEC", 0.05)
    broker = InProcessBroker()
    loop = asyncio.get_running_loop()
    req = _StubRequest(disconnect_after=loop.time() + 0.15)

    gen = routes_reports._event_stream(req, broker)
    frames: list[bytes] = []
    try:
        async for frame in gen:
            frames.append(frame)
            if len(frames) > 20:
                break
    except StopAsyncIteration:
        pass
    # Should have emitted the hello + a keepalive or two, then stopped.
    assert frames[0].startswith(b":")
    assert broker.subscriber_count == 0


@pytest.mark.asyncio
async def test_stream_returns_503_when_broker_not_wired():
    routes_reports.set_broker(None)
    from fg_voice.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/api/v1/reports/stream")
        assert r.status_code == 503


# The end-to-end HTTP test was tempting but not worth the flakiness:
# `httpx.ASGITransport` doesn't drive lifespan, and `client.stream()`
# on a long-lived SSE response hangs on teardown. The generator-level
# tests above already prove the frame shape; `test_main_lifespan.py`
# proves the wiring. The 503-when-broker-not-wired case above hits
# the actual FastAPI route.
_ = json  # keep the import for future use without a fresh line
