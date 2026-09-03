"""Graceful-drain flag + /voice/inbound behaviour under drain.

Pins two invariants (spec §14.3):

1. `is_draining()` is False at import time and stays False until
   `mark_draining()` fires — a fresh worker never refuses calls.
2. Once `mark_draining()` fires, `/voice/inbound` returns the
   fallback TwiML (Hangup) instead of `<Connect><Stream>`, so no new
   call is handed to a task that's about to disappear.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fg_voice.persistence.session_store import InMemorySessionStore
from fg_voice.telephony.twilio_signature import compute_signature
from fg_voice.utils.drain import is_draining, mark_draining, reset_for_tests

TOKEN = "test-auth-token"
_INBOUND = {
    "CallSid": "CA_DRAIN_1",
    "From": "+919876543210",
    "To": "+911800XXXXXXX",
    "CallStatus": "ringing",
    "AccountSid": "AC00000000000000000000000000000001",
}


@pytest.fixture(autouse=True)
def _reset_drain_flag() -> None:
    reset_for_tests()
    yield
    reset_for_tests()


@pytest.fixture
def client(dev_env: None, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("PUBLIC_WSS_BASE", "wss://voice.sentinel.in")
    monkeypatch.setenv("CALLER_HASH_PEPPER", "test-pepper")
    from fg_voice.api import routes_voice
    from fg_voice.main import app

    store = InMemorySessionStore()

    async def _override_store() -> InMemorySessionStore:
        return store

    monkeypatch.setattr(routes_voice, "_session_store_provider", _override_store)
    return TestClient(app)


def _sign_and_post(client: TestClient, path: str, params: dict[str, str]) -> object:
    sig = compute_signature(f"http://testserver{path}", params)
    return client.post(path, data=params, headers={"X-Twilio-Signature": sig})


def test_flag_defaults_to_false() -> None:
    assert is_draining() is False


def test_mark_draining_flips_flag_idempotently() -> None:
    assert is_draining() is False
    mark_draining()
    assert is_draining() is True
    mark_draining()  # idempotent — no error
    assert is_draining() is True


def test_inbound_normal_returns_connect_stream(client: TestClient) -> None:
    r = _sign_and_post(client, "/voice/inbound", _INBOUND)
    assert r.status_code == 200
    assert b"<Connect>" in r.content
    assert b"<Stream" in r.content


def test_inbound_when_draining_returns_fallback_hangup(client: TestClient) -> None:
    mark_draining()
    r = _sign_and_post(client, "/voice/inbound", _INBOUND)
    assert r.status_code == 200
    # Fallback TwiML: apology + Hangup. Never <Connect>.
    assert b"<Connect>" not in r.content
    assert b"<Hangup" in r.content


def test_inbound_drained_still_validates_signature(client: TestClient) -> None:
    """A bad-signature POST must 403 BEFORE the drain check fires —
    otherwise an attacker with the drain flag flipped could probe
    without a signature."""
    mark_draining()
    r = client.post(
        "/voice/inbound",
        data=_INBOUND,
        headers={"X-Twilio-Signature": "obviously-forged"},
    )
    assert r.status_code == 403
