"""/voice/inbound + /voice/status + /voice/fallback routes.

The most important assertion here is negative: a POST with a bad
signature MUST return 403 and MUST NOT touch the session store."""

from __future__ import annotations

import asyncio
from xml.etree.ElementTree import fromstring

import pytest
from fastapi.testclient import TestClient

from fg_voice.persistence.session_store import InMemorySessionStore
from fg_voice.telephony.twilio_signature import compute_signature

TOKEN = "test-auth-token"
INBOUND_PARAMS = {
    "CallSid": "CA00000000000000000000000000000001",
    "From": "+919876543210",
    "To": "+911800XXXXXXX",
    "CallStatus": "ringing",
    "AccountSid": "AC00000000000000000000000000000001",
}


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
    tc = TestClient(app)
    tc.session_store = store  # type: ignore[attr-defined]
    return tc


def _post(client: TestClient, path: str, params: dict[str, str], sign_url: str) -> object:
    sig = compute_signature(sign_url, params)
    return client.post(path, data=params, headers={"X-Twilio-Signature": sig})


def test_inbound_valid_signature_returns_twiml(client: TestClient) -> None:
    sign_url = "http://testserver/voice/inbound"
    r = _post(client, "/voice/inbound", INBOUND_PARAMS, sign_url)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    root = fromstring(r.content[r.content.index(b"<Response") :])
    stream = root.find("Connect/Stream")
    assert stream is not None
    assert stream.attrib["url"] == "wss://voice.sentinel.in/ws/media"


def test_inbound_forged_signature_returns_403(client: TestClient) -> None:
    r = client.post(
        "/voice/inbound",
        data=INBOUND_PARAMS,
        headers={"X-Twilio-Signature": "obviously-forged"},
    )
    assert r.status_code == 403


def test_inbound_missing_signature_returns_403(client: TestClient) -> None:
    r = client.post("/voice/inbound", data=INBOUND_PARAMS)
    assert r.status_code == 403


def test_inbound_writes_session(client: TestClient) -> None:
    sign_url = "http://testserver/voice/inbound"
    _post(client, "/voice/inbound", INBOUND_PARAMS, sign_url)
    store: InMemorySessionStore = client.session_store  # type: ignore[attr-defined]
    row = asyncio.new_event_loop().run_until_complete(store.get(INBOUND_PARAMS["CallSid"]))
    assert row is not None
    assert row.direction == "inbound"
    assert row.caller_hash != "" and row.caller_hash != "<none>"
    # The raw phone number must not leak into the stored row.
    assert "9876543210" not in row.caller_hash


def test_status_completed_sets_duration(client: TestClient) -> None:
    # Create the session first.
    sign_url_in = "http://testserver/voice/inbound"
    _post(client, "/voice/inbound", INBOUND_PARAMS, sign_url_in)

    status_params = {
        "CallSid": INBOUND_PARAMS["CallSid"],
        "CallStatus": "completed",
        "CallDuration": "42",
        "AccountSid": INBOUND_PARAMS["AccountSid"],
    }
    sign_url = "http://testserver/voice/status"
    r = _post(client, "/voice/status", status_params, sign_url)
    assert r.status_code == 204

    store: InMemorySessionStore = client.session_store  # type: ignore[attr-defined]
    row = asyncio.new_event_loop().run_until_complete(store.get(INBOUND_PARAMS["CallSid"]))
    assert row is not None
    assert row.duration_sec == 42
    assert row.outcome == "completed"


def test_fallback_returns_twiml_even_without_signature(client: TestClient) -> None:
    r = client.post("/voice/fallback", data={})
    assert r.status_code == 200
    assert b"<Response>" in r.content
    assert b"<Hangup" in r.content


def test_inbound_runner_mode_redirects_to_gather(
    dev_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When RUNNER_MODE=true the inbound webhook returns a `<Redirect>`
    into the Gather flow instead of the Media Streams TwiML."""
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("PUBLIC_WSS_BASE", "wss://voice.sentinel.in")
    monkeypatch.setenv("CALLER_HASH_PEPPER", "test-pepper")
    monkeypatch.setenv("RUNNER_MODE", "true")

    from fg_voice.api import routes_voice
    from fg_voice.main import app

    store = InMemorySessionStore()

    async def _override() -> InMemorySessionStore:
        return store

    monkeypatch.setattr(routes_voice, "_session_store_provider", _override)
    tc = TestClient(app)

    sign_url = "http://testserver/voice/inbound"
    r = _post(tc, "/voice/inbound", INBOUND_PARAMS, sign_url)
    assert r.status_code == 200
    root = fromstring(r.content[r.content.index(b"<Response") :])
    redirect = root.find("Redirect")
    assert redirect is not None
    # Absolute, not relative: Twilio resolves a bare path against the
    # request URL, which drops the call on a tunnelled dev host.
    assert redirect.text is not None
    assert redirect.text.startswith("https://")
    assert redirect.text.endswith("/voice/gather/start")
    # Media Streams TwiML must NOT be present.
    assert root.find("Connect/Stream") is None
