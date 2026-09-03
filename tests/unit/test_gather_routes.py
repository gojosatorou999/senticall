"""/voice/gather/start + /voice/gather/next FastAPI integration.

Uses in-memory CallState + session stores so no Redis is required.
Signature validation is enforced — a bad signature must 403 and MUST
NOT create any state."""

from __future__ import annotations

from xml.etree.ElementTree import fromstring

import pytest
from fastapi.testclient import TestClient

from fg_voice.conversation.state_store import InMemoryCallStateStore
from fg_voice.persistence.session_store import InMemorySessionStore
from fg_voice.telephony.twilio_signature import compute_signature

TOKEN = "test-auth-token"
START_URL = "http://testserver/voice/gather/start"
NEXT_URL = "http://testserver/voice/gather/next"

INBOUND_PARAMS = {
    "CallSid": "CA_GATHER_0001",
    "From": "+919876543210",
    "To": "+911800XXXXXXX",
    "CallStatus": "in-progress",
    "AccountSid": "AC00000000000000000000000000000001",
}


@pytest.fixture
def client(dev_env: None, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("CALLER_HASH_PEPPER", "test-pepper")
    monkeypatch.setenv("RUNNER_MODE", "true")

    from fg_voice.api import routes_gather
    from fg_voice.main import app

    session = InMemorySessionStore()
    call_state = InMemoryCallStateStore()

    async def _session() -> InMemorySessionStore:
        return session

    async def _call_state() -> InMemoryCallStateStore:
        return call_state

    monkeypatch.setattr(routes_gather, "_session_store_provider", _session)
    monkeypatch.setattr(routes_gather, "_call_state_store_provider", _call_state)

    tc = TestClient(app)
    tc.call_state_store = call_state  # type: ignore[attr-defined]
    tc.session_store = session  # type: ignore[attr-defined]
    return tc


def _post(client: TestClient, path: str, params: dict[str, str], sign_url: str):
    sig = compute_signature(sign_url, params)
    return client.post(path, data=params, headers={"X-Twilio-Signature": sig})


def _parse(body: bytes):
    prefix = b'<?xml version="1.0" encoding="UTF-8"?>'
    return fromstring(body[len(prefix) :] if body.startswith(prefix) else body)


# ─── /start ─────────────────────────────────────────────────────────


def test_start_returns_gather_twiml(client):
    resp = _post(client, "/voice/gather/start", INBOUND_PARAMS, START_URL)
    assert resp.status_code == 200
    root = _parse(resp.content)
    assert root.tag == "Response"
    gather = root.find("Gather")
    assert gather is not None
    # First response should surface the ask_intent question.
    inner = gather.find("Say")
    assert inner is not None
    assert "reporting a hazard" in (inner.text or "").lower()


def test_start_persists_call_state(client):
    import asyncio

    _post(client, "/voice/gather/start", INBOUND_PARAMS, START_URL)
    state = asyncio.run(client.call_state_store.load(INBOUND_PARAMS["CallSid"]))
    assert state is not None
    assert state.call_sid == INBOUND_PARAMS["CallSid"]


def test_start_rejects_bad_signature(client):
    resp = client.post(
        "/voice/gather/start",
        data=INBOUND_PARAMS,
        headers={"X-Twilio-Signature": "nope"},
    )
    assert resp.status_code == 403


# ─── /next ──────────────────────────────────────────────────────────


def test_next_speech_yes_advances_to_hazard(client):
    _post(client, "/voice/gather/start", INBOUND_PARAMS, START_URL)

    next_params = {"CallSid": INBOUND_PARAMS["CallSid"], "SpeechResult": "yes reporting"}
    resp = _post(client, "/voice/gather/next", next_params, NEXT_URL)
    assert resp.status_code == 200
    root = _parse(resp.content)
    gather = root.find("Gather")
    assert gather is not None
    inner = gather.find("Say")
    assert inner is not None
    assert "hazard" in (inner.text or "").lower()


def test_next_no_hangs_up(client):
    _post(client, "/voice/gather/start", INBOUND_PARAMS, START_URL)
    next_params = {"CallSid": INBOUND_PARAMS["CallSid"], "SpeechResult": "no thanks"}
    resp = _post(client, "/voice/gather/next", next_params, NEXT_URL)
    root = _parse(resp.content)
    # No Gather → we ended the call.
    assert root.find("Gather") is None
    assert root.find("Hangup") is not None


def test_next_dtmf_after_two_unclear_maps_to_yes(client):
    _post(client, "/voice/gather/start", INBOUND_PARAMS, START_URL)
    # Two unclear turns — moves ladder to reprompt_intent_2 (DTMF armed).
    for _ in range(2):
        _post(
            client,
            "/voice/gather/next",
            {"CallSid": INBOUND_PARAMS["CallSid"], "SpeechResult": "uhh"},
            NEXT_URL,
        )
    resp = _post(
        client,
        "/voice/gather/next",
        {"CallSid": INBOUND_PARAMS["CallSid"], "Digits": "1"},
        NEXT_URL,
    )
    root = _parse(resp.content)
    gather = root.find("Gather")
    assert gather is not None
    inner = gather.find("Say")
    assert "hazard" in (inner.text or "").lower()


def test_next_timeout_no_speech_no_digits_advances_ladder(client):
    _post(client, "/voice/gather/start", INBOUND_PARAMS, START_URL)
    resp = _post(
        client,
        "/voice/gather/next",
        {"CallSid": INBOUND_PARAMS["CallSid"]},  # nothing → timeout
        NEXT_URL,
    )
    root = _parse(resp.content)
    gather = root.find("Gather")
    assert gather is not None
    inner = gather.find("Say")
    # After a timeout on ask_intent → reprompt_intent_1.
    assert "sorry" in (inner.text or "").lower() or "hazard" in (inner.text or "").lower()


def test_next_state_missing_returns_fatal_hangup(client):
    """Calling /next without a matching /start (or after state expired)
    returns a fatal-hangup TwiML, not a 500."""
    resp = _post(
        client,
        "/voice/gather/next",
        {"CallSid": "CA_never_started", "SpeechResult": "yes"},
        NEXT_URL,
    )
    assert resp.status_code == 200
    root = _parse(resp.content)
    assert root.find("Hangup") is not None


def test_next_bad_signature_rejected(client):
    _post(client, "/voice/gather/start", INBOUND_PARAMS, START_URL)
    resp = client.post(
        "/voice/gather/next",
        data={"CallSid": INBOUND_PARAMS["CallSid"], "SpeechResult": "yes"},
        headers={"X-Twilio-Signature": "nope"},
    )
    assert resp.status_code == 403


# ─── Full happy path over the routes ─────────────────────────────────


def test_happy_path_terminates_at_submitted_twiml(client):
    _post(client, "/voice/gather/start", INBOUND_PARAMS, START_URL)
    for utterance in [
        "yes",
        "storm damage",
        "wind broke a tree",
        "Vizag Beach",
        "yes",  # confirm low-conf location
        "extreme",
        "waist",
        "yes",  # confirm summary
    ]:
        resp = _post(
            client,
            "/voice/gather/next",
            {"CallSid": INBOUND_PARAMS["CallSid"], "SpeechResult": utterance},
            NEXT_URL,
        )
    root = _parse(resp.content)
    # Terminal → Hangup, and the submitted prompt got rendered with an FG-ref.
    assert root.find("Hangup") is not None
    say_texts = [s.text or "" for s in root.findall("Say")]
    joined = " ".join(say_texts)
    assert "FG-" in joined
