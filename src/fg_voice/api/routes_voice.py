"""Twilio HTTP webhooks: /voice/inbound, /voice/status, /voice/fallback.

Every one of these validates X-Twilio-Signature before doing anything
else. See CLAUDE.md invariant + spec §17.3."""

from __future__ import annotations

import uuid
from typing import Annotated, Protocol

from fastapi import APIRouter, Form, Header, HTTPException, Request, status
from fastapi.responses import Response

from fg_voice.config import get_settings
from fg_voice.obs.logging import get_logger
from fg_voice.persistence.session_store import get_session_store
from fg_voice.telephony.twilio_signature import (
    InvalidTwilioSignatureError,
    verify_twilio_signature,
)
from fg_voice.telephony.twilio_twiml import (
    connect_stream_twiml,
    fallback_twiml,
    gather_redirect_twiml,
)
from fg_voice.utils.drain import is_draining
from fg_voice.utils.hashing import hash_msisdn

log = get_logger(__name__)
router = APIRouter(prefix="/voice", tags=["voice"])

# Overridable at test time — the tests monkeypatch this module attribute
# with an in-memory store so we don't hit Redis for /voice/* unit tests.
_session_store_provider = get_session_store

# Set by main.py's lifespan when SMS pin-offer is enabled AND configured.
# Left as None in dev / when SMS is off — the status webhook then just
# logs and skips. Kept module-scoped so tests can inject a RecordingSender-
# backed service without patching the lifespan.
_sms_pin_offer_service: SmsPinOfferServiceLike | None = None


class SmsPinOfferServiceLike(Protocol):
    """Duck-typed reference (avoid an enrichment→api import cycle).
    Any object with `.maybe_send(*, call_sid, to_number)` satisfies
    this — the real impl is `enrichment.sms_pin_offer.SmsPinOfferService`."""

    async def maybe_send(self, *, call_sid: str, to_number: str) -> bool: ...


def set_sms_pin_offer_service(service: SmsPinOfferServiceLike | None) -> None:
    global _sms_pin_offer_service
    _sms_pin_offer_service = service


def _reconstruct_url(request: Request) -> str:
    """Rebuild the URL Twilio saw — behind an ALB the scheme and host
    live in X-Forwarded-* headers, not on request.url."""
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get(
        "host", request.url.hostname or ""
    )
    return f"{scheme}://{host}{request.url.path}"


async def _validate(request: Request, signature: str | None, params: dict[str, str]) -> None:
    settings = get_settings()
    if not settings.twilio_signature_validation_enabled:
        if settings.is_production():
            raise HTTPException(status_code=500, detail="signature validation disabled in prod")
        log.warning("twilio.signature_check_skipped", path=request.url.path)
        return
    try:
        verify_twilio_signature(signature, _reconstruct_url(request), params)
    except InvalidTwilioSignatureError as exc:
        log.warning(
            "twilio.signature_rejected",
            reason=str(exc),
            path=request.url.path,
            remote=request.client.host if request.client else None,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden") from exc


@router.post("/inbound", response_class=Response)
async def inbound(
    request: Request,
    x_twilio_signature: Annotated[str | None, Header(alias="X-Twilio-Signature")] = None,
) -> Response:
    """Twilio POSTs here when a call is answered. We return TwiML that
    hands control to the Media Streams WSS."""
    form = await request.form()
    params: dict[str, str] = {k: str(v) for k, v in form.items()}
    await _validate(request, x_twilio_signature, params)

    # Drain check (spec §14.3): if a SIGTERM fired, refuse to hand
    # this call to a worker that's about to die. Return the static
    # fallback TwiML so the caller gets the SMS-with-web-form path
    # instead of a dropped call.
    if is_draining():
        log.warning(
            "voice.inbound.drained",
            call_sid=params.get("CallSid", ""),
            note="task draining after SIGTERM; returning fallback TwiML",
        )
        return Response(content=fallback_twiml(), media_type="application/xml")

    settings = get_settings()
    call_sid = params.get("CallSid", "")
    caller = params.get("From", "")
    caller_hash = hash_msisdn(caller, settings.caller_hash_pepper.get_secret_value())

    # Mint the report_id BEFORE the stream opens — this is the
    # idempotency key for the whole call (§15.1).
    report_id = str(uuid.uuid4())

    store = await _session_store_provider()
    await store.create(
        call_sid=call_sid,
        report_id=report_id,
        caller_hash=caller_hash,
        direction="inbound",
    )

    log.info(
        "voice.inbound",
        call_sid=call_sid,
        report_id=report_id,
        caller_hash=caller_hash[:12] + "…",
        from_country=params.get("FromCountry"),
        runner_mode=settings.runner_mode,
    )

    # TURN_MODE: hand off to the <Play>/<Record> flow with OpenAI TTS+STT.
    # Required wherever Media Streams is unavailable (Twilio trial tier).
    if settings.turn_mode:
        base = (
            settings.public_wss_base.replace("wss://", "https://")
            .replace("ws://", "http://")
            .rstrip("/")
        )
        body = gather_redirect_twiml(f"{base}/voice/turn/start")
        return Response(content=body, media_type="application/xml")

    # Feature-flagged: when RUNNER_MODE is on we hand off to the
    # Gather-based flow (P2.6). The Media Streams path is unchanged.
    if settings.runner_mode:
        # Absolute, not relative. A bare "/voice/gather/start" makes Twilio
        # resolve against the request URL, which drops the call outright on
        # a tunnelled dev host — same reasoning as
        # `routes_gather._next_action_path`, which already builds the Gather
        # action absolutely. Keep the two consistent.
        base = (
            settings.public_wss_base.replace("wss://", "https://")
            .replace("ws://", "http://")
            .rstrip("/")
        )
        body = gather_redirect_twiml(f"{base}/voice/gather/start")
        return Response(content=body, media_type="application/xml")

    wss = f"{settings.public_wss_base}/ws/media"
    body = connect_stream_twiml(
        wss_url=wss,
        report_id=report_id,
        caller_hash=caller_hash,
    )
    return Response(content=body, media_type="application/xml")


@router.api_route("/debug-stream", methods=["GET", "POST"], response_class=Response)
async def debug_stream(request: Request) -> Response:
    """Diagnostic only: <Say> then <Connect><Stream>. If the caller hears
    the Say but /ws/media is never hit, the Stream verb itself is being
    refused (account capability), not our WSS endpoint."""
    settings = get_settings()
    wss = f"{settings.public_wss_base}/ws/media"
    body = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b"<Response>"
        b'<Say voice="alice">Debug test. Connecting the media stream now.</Say>'
        b'<Connect><Stream url="' + wss.encode() + b'" /></Connect>'
        b"</Response>"
    )
    log.info("voice.debug_stream.served", wss=wss)
    return Response(content=body, media_type="application/xml")


@router.post("/status", response_class=Response)
async def status_callback(
    request: Request,
    x_twilio_signature: Annotated[str | None, Header(alias="X-Twilio-Signature")] = None,
    CallSid: Annotated[str, Form()] = "",
    CallStatus: Annotated[str, Form()] = "",
    CallDuration: Annotated[str, Form()] = "",
) -> Response:
    """Lifecycle events: initiated | ringing | answered | completed.
    Persist final duration + outcome on completed, then fire the
    post-call SMS pin offer if enabled + triggerable (spec §7.3
    ladder attempt 4 / §11 pin-drop). SMS failure is logged, never
    propagated — a 5xx here would cause Twilio to retry the status
    webhook, which the outbox already handled once."""
    form = await request.form()
    form_params = {k: str(v) for k, v in form.items()}
    await _validate(request, x_twilio_signature, form_params)

    store = await _session_store_provider()

    if CallStatus == "completed":
        duration_sec = int(CallDuration) if CallDuration.isdigit() else None
        await store.finalize(call_sid=CallSid, duration_sec=duration_sec, outcome="completed")
        log.info("voice.status.completed", call_sid=CallSid, duration_sec=duration_sec)
        if _sms_pin_offer_service is not None:
            # `From` on the status webhook is the caller MSISDN. Kept
            # in this stack frame only per CLAUDE.md invariant #6.
            to_number = form_params.get("From", "")
            try:
                await _sms_pin_offer_service.maybe_send(call_sid=CallSid, to_number=to_number)
            except Exception:
                log.exception("voice.sms_pin_offer.unexpected_error", call_sid=CallSid)
    else:
        log.info("voice.status", call_sid=CallSid, status=CallStatus)

    return Response(status_code=204)


@router.post("/fallback", response_class=Response)
async def fallback(
    request: Request,
    x_twilio_signature: Annotated[str | None, Header(alias="X-Twilio-Signature")] = None,
) -> Response:
    """Configured on the Twilio number as the Fallback URL. Twilio hits
    it when /voice/inbound errors or times out. Never fails."""
    form = await request.form()
    # Still validate — the fallback URL is a webhook too. If validation
    # fails we still return the static apology (better than 403 during
    # an outage).
    try:
        await _validate(request, x_twilio_signature, {k: str(v) for k, v in form.items()})
    except HTTPException:
        log.warning("voice.fallback.bad_signature")
    body = fallback_twiml()
    return Response(content=body, media_type="application/xml")
