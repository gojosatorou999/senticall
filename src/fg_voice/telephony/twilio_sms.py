"""Thin async wrapper around Twilio's Messages API.

Two-line contract: give it `to`, `from`, `body`, get back a message SID
(or an exception). The Protocol lets tests inject a `RecordingSmsSender`
without hitting the real API — the same pattern used by the alert
webhook backend and the enrichment extractors.

Deliberately NOT using the official `twilio` Python SDK — its client is
sync-only and would need a threadpool hop on every send. The Messages
API surface we need (POST + basic auth + form body) is tiny; a plain
`httpx.AsyncClient` is simpler and keeps the async path clean.

Degraded mode (CLAUDE.md invariant #7): if the API returns 5xx or
times out, `TwilioSmsError` is raised. Callers (currently
`SmsPinOfferService`) log-and-swallow — the caller has already ended
the call and hearing "we couldn't SMS you" would be jarring. Any
sustained SMS outage should surface via the metric on the log entry,
not by leaking into the /voice/status webhook response."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol

import httpx

from fg_voice.obs.logging import get_logger

log = get_logger(__name__)

DEFAULT_TIMEOUT_SEC: Final[float] = 5.0
TWILIO_API_BASE: Final[str] = "https://api.twilio.com/2010-04-01"


class TwilioSmsError(Exception):
    """Raised on any non-2xx response, timeout, or transport failure."""


class SmsSender(Protocol):
    """Send-only interface. The real Twilio impl and the test
    RecordingSmsSender both satisfy this."""

    async def send(self, *, to: str, from_: str, body: str) -> str: ...


@dataclass(slots=True)
class TwilioSmsSender:
    """Real Twilio Messages API client. `account_sid` + `auth_token`
    come from settings; the caller passes `from_` per-call so surge
    mode or a per-region rotation stays possible without rebuilding
    the sender."""

    account_sid: str
    auth_token: str
    timeout_sec: float = DEFAULT_TIMEOUT_SEC

    async def send(self, *, to: str, from_: str, body: str) -> str:
        url = f"{TWILIO_API_BASE}/Accounts/{self.account_sid}/Messages.json"
        data = {"To": to, "From": from_, "Body": body}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
                resp = await client.post(
                    url,
                    data=data,
                    auth=(self.account_sid, self.auth_token),
                )
        except httpx.HTTPError as exc:
            raise TwilioSmsError(f"transport error: {exc}") from exc
        if resp.status_code >= 300:
            # Twilio returns a JSON error body — surface just the code +
            # message so we don't accidentally log credentials in the URL.
            body_snip = resp.text[:200]
            raise TwilioSmsError(f"twilio {resp.status_code}: {body_snip}")
        try:
            sid = resp.json().get("sid")
        except ValueError as exc:  # non-JSON success is a Twilio bug
            raise TwilioSmsError(f"non-JSON response: {exc}") from exc
        if not sid or not isinstance(sid, str):
            raise TwilioSmsError("response missing 'sid'")
        return sid


@dataclass(slots=True)
class RecordingSmsSender:
    """Test double + a useful dry-run for staging. Captures every send
    in `.sent` and returns a synthetic SID."""

    sent: list[dict[str, str]]

    def __init__(self) -> None:
        self.sent = []

    async def send(self, *, to: str, from_: str, body: str) -> str:
        self.sent.append({"to": to, "from": from_, "body": body})
        return f"SM_test_{len(self.sent):04d}"


__all__ = [
    "DEFAULT_TIMEOUT_SEC",
    "RecordingSmsSender",
    "SmsSender",
    "TwilioSmsError",
    "TwilioSmsSender",
]
