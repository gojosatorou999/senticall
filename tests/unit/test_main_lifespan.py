"""main.py lifespan starts + stops the outbox relay cleanly.

Uses the lifespan context manager directly rather than through
httpx.ASGITransport — that transport doesn't drive lifespan events,
so the relay task would never be created and the assertion would
always fail with a misleading "task is None"."""

from __future__ import annotations

import asyncio

import pytest

from fg_voice.api import routes_reports


@pytest.mark.asyncio
async def test_relay_starts_and_stops_in_lifespan(dev_env: None, monkeypatch):
    """RELAY_ENABLED=true → lifespan creates a background OutboxRelay
    task and stashes it on `app.state.relay_task`. Lifespan exit sets
    the shutdown event and awaits the task before returning."""
    monkeypatch.setenv("RELAY_ENABLED", "true")
    monkeypatch.setenv("RELAY_POLL_INTERVAL_SEC", "0.05")
    monkeypatch.setenv("CALLER_HASH_PEPPER", "test-pepper")

    from fg_voice.main import app, lifespan

    async with lifespan(app):
        relay_task = getattr(app.state, "relay_task", None)
        assert relay_task is not None, "lifespan should create a relay task"
        assert not relay_task.done(), "relay task should still be running mid-lifespan"
        # Let the relay poll a couple of times so we know it's alive.
        await asyncio.sleep(0.15)
        assert not relay_task.done()
        # The broker was wired into routes_reports too.
        assert routes_reports._broker_provider() is not None

    # After lifespan exits, the relay task must be finished (or cancelled).
    assert relay_task.done(), "lifespan exit should stop the relay task"
    # And the broker singleton was cleared so a subsequent lifespan
    # doesn't inherit stale state.
    assert routes_reports._broker_provider() is None


@pytest.mark.asyncio
async def test_relay_disabled_when_env_flag_false(dev_env: None, monkeypatch):
    """RELAY_ENABLED=false leaves `app.state.relay_task` unset — useful
    for workers that only serve HTTP with no DB writes."""
    monkeypatch.setenv("RELAY_ENABLED", "false")
    monkeypatch.setenv("CALLER_HASH_PEPPER", "test-pepper")

    from fg_voice.main import app, lifespan

    async with lifespan(app):
        assert getattr(app.state, "relay_task", None) is None
        # Broker also skipped — no drain, no fan-out.
        assert routes_reports._broker_provider() is None
