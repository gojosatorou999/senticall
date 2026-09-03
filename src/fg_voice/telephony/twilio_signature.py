"""Twilio webhook signature validation.

Non-negotiable — see spec §17.3 and the P1 exit gate. Without this,
anyone can POST fake `<Voice>` events to `/voice/inbound` and stand up
ghost calls in our system. Fail closed, fail fast.

Uses `twilio.request_validator.RequestValidator` (canonical) — we don't
reimplement HMAC-SHA1 ourselves because getting the URL-encoding rules
wrong is a foot-gun."""

from __future__ import annotations

from twilio.request_validator import RequestValidator  # type: ignore[import-untyped]

from fg_voice.config import get_settings


class InvalidTwilioSignatureError(Exception):
    """Raised by `verify_twilio_signature` when the signature does not
    match. Route handlers turn this into HTTP 403."""


def verify_twilio_signature(
    signature: str | None,
    url: str,
    params: dict[str, str],
) -> None:
    """Raise `InvalidTwilioSignatureError` unless the header is a valid
    signature for (url, params) under our Twilio auth token.

    `url` MUST be the exact URL Twilio POSTed to, reconstructed from
    the request. Behind an ALB this means using the forwarded scheme
    and host — the route handler is responsible for that."""
    settings = get_settings()
    token = settings.twilio_auth_token.get_secret_value()
    if not token:
        # An empty token means a misconfigured server. Fail closed even
        # in dev — otherwise the P1 exit gate ("signature validation
        # rejects a forged webhook") is a lie.
        raise InvalidTwilioSignatureError("Twilio auth token not configured")
    if not signature:
        raise InvalidTwilioSignatureError("Missing X-Twilio-Signature header")

    validator = RequestValidator(token)
    if not validator.validate(url, params, signature):
        raise InvalidTwilioSignatureError("Signature does not match")


def compute_signature(url: str, params: dict[str, str]) -> str:
    """Compute a valid signature — used in tests only. Never called from
    the request-handling path."""
    settings = get_settings()
    validator = RequestValidator(settings.twilio_auth_token.get_secret_value())
    return str(validator.compute_signature(url, params))
