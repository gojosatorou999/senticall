"""X-Twilio-Signature validation.

The P1 exit gate is "signature validation rejects a forged webhook".
These tests pin that: a correctly computed signature passes, anything
else raises InvalidTwilioSignatureError."""

from __future__ import annotations

import pytest

from fg_voice.telephony.twilio_signature import (
    InvalidTwilioSignatureError,
    compute_signature,
    verify_twilio_signature,
)

TOKEN = "test-auth-token-do-not-use-outside-tests"
URL = "https://voice.sentinel.in/voice/inbound"
PARAMS = {
    "CallSid": "CA1234567890abcdef1234567890abcdef",
    "From": "+919876543210",
    "To": "+911800XXXXXXX",
    "CallStatus": "ringing",
}


@pytest.fixture(autouse=True)
def _twilio_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", TOKEN)


def test_valid_signature_accepted() -> None:
    sig = compute_signature(URL, PARAMS)
    # Should not raise.
    verify_twilio_signature(sig, URL, PARAMS)


def test_forged_signature_rejected() -> None:
    with pytest.raises(InvalidTwilioSignatureError, match="Signature does not match"):
        verify_twilio_signature("obviously-not-a-signature", URL, PARAMS)


def test_missing_signature_rejected() -> None:
    with pytest.raises(InvalidTwilioSignatureError, match="Missing"):
        verify_twilio_signature(None, URL, PARAMS)


def test_tampered_params_rejected() -> None:
    sig = compute_signature(URL, PARAMS)
    tampered = {**PARAMS, "From": "+919999999999"}
    with pytest.raises(InvalidTwilioSignatureError):
        verify_twilio_signature(sig, URL, tampered)


def test_tampered_url_rejected() -> None:
    sig = compute_signature(URL, PARAMS)
    with pytest.raises(InvalidTwilioSignatureError):
        verify_twilio_signature(sig, "https://evil.example.com/voice/inbound", PARAMS)


def test_empty_token_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "")
    with pytest.raises(InvalidTwilioSignatureError, match="not configured"):
        verify_twilio_signature("anything", URL, PARAMS)
