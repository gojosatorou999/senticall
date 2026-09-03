"""Alert fan-out dispatcher (§2.4 + §11 severity=extreme handling).

The relay drains every outbox row — most are ordinary reports. This
dispatcher filters to the two categories that need an ops response
within seconds:

- `life_safety_flag=true` — the safety tripwire caught a caller
  reporting injury / entrapment / drowning (§2.4)
- `severity=extreme` — the caller classified the hazard as extreme

Both go to every registered `AlertBackend`. Backends are cheap-to-add;
today we ship:

- `LogAlertBackend` — structured log entry. Always on so alerts are
  audit-traceable even when no external endpoint is configured.
- `WebhookAlertBackend` — HTTP POST to a configurable URL (Slack
  incoming webhook, PagerDuty events API, a private ops endpoint).
  Timeout-bounded so a slow webhook never stalls the relay.

- `SnsAlertBackend` — publish to an SNS topic; the topic fans to
  ops SMS + on-call rotation (Lambda subscription to PagerDuty for
  the extreme-severity / life-safety cases). See spec §14.1.

Design: backends are additive. A backend failure is caught locally
and logged so a chained backend's failure doesn't take out the others
— but at least ONE backend failing raises `AlertDeliveryError`, which
the relay treats as a normal dispatch failure (retry_count bumps,
row stays available for the next poll)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

import httpx

from fg_voice.obs.logging import get_logger
from fg_voice.persistence.models import OutboxEntry

log = get_logger(__name__)

# Timeout budget for a webhook call. Kept small — a hung webhook
# blocking the relay is worse than a missed alert (the log backend
# still fires, and the outbox row retries).
DEFAULT_WEBHOOK_TIMEOUT_SEC: Final[float] = 5.0


class AlertDeliveryError(Exception):
    """Raised when at least one backend failed. The relay bumps
    retry_count and re-attempts the row."""


class AlertBackend(Protocol):
    """One alert channel. Must be idempotent — the relay may re-deliver
    the same alert under retry."""

    async def send(self, alert: dict[str, Any]) -> None: ...


@dataclass(slots=True)
class LogAlertBackend:
    """Safe default. Emits a structured log entry, never raises."""

    async def send(self, alert: dict[str, Any]) -> None:
        log.warning(
            "alert.fired",
            severity=alert.get("severity"),
            life_safety=alert.get("life_safety_flag"),
            short_ref=alert.get("short_ref"),
            hazard_type=alert.get("hazard_type"),
            location=alert.get("location_raw"),
            call_sid=alert.get("call_sid"),
        )


@dataclass(slots=True)
class WebhookAlertBackend:
    """HTTP POST to a URL (Slack webhook, PagerDuty events API, etc).
    JSON body. `timeout_sec` is a hard cap on the whole request so a
    stuck endpoint can't block the relay indefinitely."""

    url: str
    timeout_sec: float = DEFAULT_WEBHOOK_TIMEOUT_SEC

    async def send(self, alert: dict[str, Any]) -> None:
        async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
            resp = await client.post(self.url, json=alert)
            resp.raise_for_status()


@dataclass(slots=True)
class SnsAlertBackend:
    """AWS SNS publish. Kept behind the Protocol so an operator with no
    AWS access still uses the Log + Webhook backends.

    Lazy-imports `aioboto3` so a deploy without SNS doesn't force the
    whole boto stack on the base image. The topic ARN is per-env; the
    subject line encodes the trigger so a paging Lambda can filter
    without deserializing the body."""

    topic_arn: str
    region_name: str = "ap-south-1"

    async def send(self, alert: dict[str, Any]) -> None:
        import aioboto3

        session = aioboto3.Session()
        subject = _sns_subject(alert)
        # SNS message + subject each have separate hard limits
        # (262144 bytes / 100 chars). The alert dicts we build in
        # `_build_alert` are tiny (< 2 KB) so no truncation dance.
        import json

        body = json.dumps(alert, default=str, separators=(",", ":"))
        async with session.client("sns", region_name=self.region_name) as client:
            await client.publish(
                TopicArn=self.topic_arn,
                Subject=subject,
                Message=body,
                MessageAttributes={
                    "trigger": {
                        "DataType": "String",
                        "StringValue": str(alert.get("trigger", "unknown")),
                    },
                    "short_ref": {
                        "DataType": "String",
                        "StringValue": str(alert.get("short_ref") or "unknown"),
                    },
                },
            )


def _sns_subject(alert: dict[str, Any]) -> str:
    """Short human-readable subject. SNS caps at 100 chars — trim
    aggressively so the alert never bounces on a formatting slip."""
    trigger = "LIFE-SAFETY" if alert.get("life_safety_flag") else "SEVERITY-EXTREME"
    short_ref = alert.get("short_ref") or "??"
    hazard = alert.get("hazard_type") or "unknown"
    subject = f"[FG {trigger}] {short_ref} — {hazard}"
    return subject[:100]


@dataclass(slots=True)
class AlertDispatcher:
    """Filters `report.*` outbox events, packages the alert payload,
    fans out to every backend. `backends` is evaluated left-to-right;
    one failure doesn't stop the others but does raise at the end."""

    backends: list[AlertBackend] = field(default_factory=list)

    async def dispatch(self, entry: OutboxEntry) -> None:
        if not _should_alert(entry):
            return
        payload = dict(entry.payload or {})
        alert = _build_alert(entry, payload)

        errors: list[str] = []
        for backend in self.backends:
            try:
                await backend.send(alert)
            except Exception as exc:
                errors.append(f"{type(backend).__name__}: {exc}")
                log.exception(
                    "alert.backend_failed",
                    backend=type(backend).__name__,
                    short_ref=alert.get("short_ref"),
                )
        if errors:
            # At least one backend failed; surface to the relay so
            # the row's retry_count bumps. The next poll re-attempts.
            # Note: idempotency is on the backend (Slack rate-limits
            # duplicates, PagerDuty dedups by incident_key), not here.
            raise AlertDeliveryError("; ".join(errors))


# ─── Filter + packaging ──────────────────────────────────────────────


def _should_alert(entry: OutboxEntry) -> bool:
    """Two triggers per spec: `life_safety_flag=true` OR
    `severity=extreme`. Both are OR — a life-safety incident with
    light severity is still an alert."""
    if not entry.event_type.startswith("report."):
        return False
    payload = entry.payload or {}
    if _has_flag(payload, "life_safety"):
        return True
    return str(payload.get("severity", "")).lower() == "extreme"


def _has_flag(payload: dict[str, Any], flag: str) -> bool:
    flags = payload.get("flags")
    if flags is None:
        return False
    if isinstance(flags, dict):
        return bool(flags.get(flag))
    if isinstance(flags, (list, tuple, set)):
        return flag in flags
    return False


def _build_alert(entry: OutboxEntry, payload: dict[str, Any]) -> dict[str, Any]:
    """Minimal payload that every backend understands. Ops dashboards
    can rehydrate the full report by calling GET /reports/{short_ref}
    (that endpoint lands with the admin console in P5-plus).

    Description is deliberately the PII-scrubbed `description_clean`
    (not the raw caller utterance) — Slack/PagerDuty payloads leave
    the trust boundary, and a full-text alert body can end up on
    unauthorised operators' phones."""
    return {
        "event_type": entry.event_type,
        "outbox_id": entry.id,
        "report_id": payload.get("report_id"),
        "short_ref": payload.get("short_ref"),
        "call_sid": payload.get("call_sid"),
        "hazard_type": payload.get("hazard_type"),
        "severity": payload.get("severity"),
        "water_depth_cm": payload.get("water_depth_cm"),
        "description_clean": payload.get("description_clean"),
        "location_raw": payload.get("location_raw"),
        "life_safety_flag": _has_flag(payload, "life_safety"),
        "trigger": ("life_safety" if _has_flag(payload, "life_safety") else "severity_extreme"),
        "created_at": payload.get("created_at"),
    }


# Helper for `asyncio.gather` if a caller wants concurrent fan-out
# with the same failure-collection semantics. Not wired into
# AlertDispatcher today (sequential is fine for < 5 backends).
async def _fan_out_parallel(
    backends: list[AlertBackend], alert: dict[str, Any]
) -> list[BaseException | None]:  # pragma: no cover — reserved for perf tuning
    results = await asyncio.gather(*[b.send(alert) for b in backends], return_exceptions=True)
    return [r if isinstance(r, BaseException) else None for r in results]


__all__ = [
    "DEFAULT_WEBHOOK_TIMEOUT_SEC",
    "AlertBackend",
    "AlertDeliveryError",
    "AlertDispatcher",
    "LogAlertBackend",
    "SnsAlertBackend",
    "WebhookAlertBackend",
]
