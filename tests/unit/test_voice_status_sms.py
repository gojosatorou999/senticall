"""/voice/status → SmsPinOfferService wiring.

Verifies that a `CallStatus=completed` webhook hands off the caller's
`From` number + `CallSid` to the injected pin-offer service, and that
the service being absent (SMS disabled) is silently tolerated.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fg_voice.persistence.session_store import InMemorySessionStore
from fg_voice.telephony.twilio_signature import compute_signature

TOKEN = "test-auth-token"

_STATUS_PARAMS = {
    "CallSid": "CA_STATUS_1",
    "From": "+919000000000",
    "CallStatus": "completed",
    "CallDuration": "23",
    "AccountSid": "AC00000000000000000000000000000001",
}


class _RecordingService:
    """Duck-typed stand-in for `SmsPinOfferService`. Records every call
    and returns True — matches the .maybe_send Protocol shape."""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def maybe_send(self, *, call_sid: str, to_number: str) -> bool:
        self.calls.append({"call_sid": call_sid, "to_number": to_number})
        return True


class _RaisingService:
    async def maybe_send(self, *, call_sid: str, to_number: str) -> bool:
        raise RuntimeError("service blew up")


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
    # Ensure any previous test's injected service doesn't leak.
    routes_voice.set_sms_pin_offer_service(None)
    tc = TestClient(app)
    tc.session_store = store  # type: ignore[attr-defined]
    return tc


def _sign_and_post(client: TestClient, path: str, params: dict[str, str]) -> object:
    sig = compute_signature(f"http://testserver{path}", params)
    return client.post(path, data=params, headers={"X-Twilio-Signature": sig})


def test_status_completed_dispatches_to_pin_offer_when_wired(client: TestClient) -> None:
    from fg_voice.api import routes_voice

    service = _RecordingService()
    routes_voice.set_sms_pin_offer_service(service)
    try:
        r = _sign_and_post(client, "/voice/status", _STATUS_PARAMS)
        assert r.status_code == 204
        assert len(service.calls) == 1
        assert service.calls[0]["call_sid"] == _STATUS_PARAMS["CallSid"]
        assert service.calls[0]["to_number"] == _STATUS_PARAMS["From"]
    finally:
        routes_voice.set_sms_pin_offer_service(None)


def test_status_non_completed_does_not_dispatch(client: TestClient) -> None:
    from fg_voice.api import routes_voice

    service = _RecordingService()
    routes_voice.set_sms_pin_offer_service(service)
    try:
        params = {**_STATUS_PARAMS, "CallStatus": "ringing"}
        r = _sign_and_post(client, "/voice/status", params)
        assert r.status_code == 204
        assert service.calls == []  # only `completed` fires the SMS
    finally:
        routes_voice.set_sms_pin_offer_service(None)


def test_status_completed_no_service_wired_still_returns_204(client: TestClient) -> None:
    from fg_voice.api import routes_voice

    routes_voice.set_sms_pin_offer_service(None)
    r = _sign_and_post(client, "/voice/status", _STATUS_PARAMS)
    assert r.status_code == 204


def test_status_completed_service_error_still_returns_204(client: TestClient) -> None:
    """A service exception must NEVER propagate to Twilio — a 5xx here
    would cause the status callback to retry, which could re-fire any
    downstream side-effects (SMS included) after the CallState TTL
    expires."""
    from fg_voice.api import routes_voice

    routes_voice.set_sms_pin_offer_service(_RaisingService())
    try:
        r = _sign_and_post(client, "/voice/status", _STATUS_PARAMS)
        assert r.status_code == 204
    finally:
        routes_voice.set_sms_pin_offer_service(None)
