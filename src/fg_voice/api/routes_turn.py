"""Turn-based voice flow: OpenAI TTS + OpenAI STT over plain TwiML.

Twilio Media Streams (`<Connect><Stream>`) is a paid feature and is
silently refused on trial accounts — the TwiML is fetched, the stream is
never opened, and the call drops. This flow uses only `<Play>` and
`<Record>`, which work on every account tier, while keeping OpenAI on
both ends of the audio:

    <Play> OpenAI-TTS mp3  →  <Record> caller  →  OpenAI STT
        →  OpenAI chat decides the next question  →  repeat

Trade-off vs. the realtime path: turn-based, so there is a pause between
the caller finishing and the agent replying, and no barge-in.

State is process-local (single-worker dev deploy). A restart mid-call
loses the conversation; the caller simply starts over.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import Response

from fg_voice.config import get_settings
from fg_voice.conversation.sql_report_sink import SqlReportSink
from fg_voice.obs.logging import get_logger
from fg_voice.persistence.db import get_session_maker
from fg_voice.pipeline import openai_voice
from fg_voice.utils.hashing import hash_msisdn

log = get_logger(__name__)
router = APIRouter(tags=["turn"])

# token -> mp3 bytes. Bounded by _MAX_CLIPS so a long-running process
# can't grow without limit; clips are single-use in practice.
_audio_cache: dict[str, bytes] = {}
_MAX_CLIPS = 200

# call_sid -> {"history": [...], "report_id": str, "caller_hash": str, "turns": int}
_calls: dict[str, dict[str, Any]] = {}

# Hard stop so a confused caller (or model) can't loop forever.
_MAX_TURNS = 14

_OPENING = (
    "Sentinel coastal alert line, this call is recorded. "
    "After the beep, tell me what hazard you're reporting."
)

_DID_NOT_CATCH = "Sorry, I didn't catch that. Could you say it again?"


def _public_base() -> str:
    """https base for URLs Twilio must fetch (TwiML actions, audio clips)."""
    return (
        get_settings()
        .public_wss_base.replace("wss://", "https://")
        .replace("ws://", "http://")
        .rstrip("/")
    )


async def _clip_url(text: str) -> str:
    """Synthesise `text` with OpenAI TTS, cache it, return its public URL."""
    audio = await openai_voice.synthesize(text)
    token = uuid.uuid4().hex
    if len(_audio_cache) >= _MAX_CLIPS:
        _audio_cache.pop(next(iter(_audio_cache)), None)
    _audio_cache[token] = audio
    return f"{_public_base()}/audio/{token}.mp3"


def _xml(body: str) -> Response:
    payload = '<?xml version="1.0" encoding="UTF-8"?>' + body
    return Response(content=payload, media_type="application/xml")


def _play_and_record(clip_url: str) -> Response:
    """Play one clip, then record the caller's answer."""
    action = f"{_public_base()}/voice/turn/next"
    return _xml(
        f"<Response>"
        f"<Play>{clip_url}</Play>"
        f'<Record action="{action}" method="POST" maxLength="25" timeout="4" '
        f'playBeep="true" trim="trim-silence" />'
        f"</Response>"
    )


def _play_and_hangup(clip_url: str) -> Response:
    return _xml(f"<Response><Play>{clip_url}</Play><Hangup /></Response>")


@router.get("/audio/{token}.mp3")
async def serve_audio(token: str) -> Response:
    """Serve a synthesised clip to Twilio. Public by design — Twilio's
    media fetcher cannot authenticate, and the tokens are unguessable
    and short-lived."""
    audio = _audio_cache.get(token)
    if audio is None:
        return Response(status_code=404)
    return Response(content=audio, media_type="audio/mpeg")


@router.post("/voice/turn/start")
async def turn_start(request: Request) -> Response:
    """Entry point for a turn-based call: greet + ask the first question."""
    form = await request.form()
    call_sid = str(form.get("CallSid", ""))
    caller = str(form.get("From", ""))
    settings = get_settings()
    caller_hash = hash_msisdn(caller, settings.caller_hash_pepper.get_secret_value())
    report_id = str(uuid.uuid4())

    _calls[call_sid] = {
        "history": [{"role": "assistant", "content": _OPENING}],
        "report_id": report_id,
        "caller_hash": caller_hash,
        "turns": 0,
    }
    log.info("turn.start", call_sid=call_sid, report_id=report_id)

    try:
        clip = await _clip_url(_OPENING)
    except Exception:
        log.exception("turn.tts_failed", call_sid=call_sid)
        return _xml(
            "<Response><Say>Sorry, we're unable to take your call right now. "
            "Please try again shortly.</Say><Hangup /></Response>"
        )
    return _play_and_record(clip)


@router.post("/voice/turn/next")
async def turn_next(
    request: Request,
    CallSid: Annotated[str, Form()] = "",
    RecordingUrl: Annotated[str, Form()] = "",
    RecordingDuration: Annotated[str, Form()] = "",
) -> Response:
    """One conversational turn: transcribe the caller, decide the reply,
    speak it, and either record again or finish."""
    state = _calls.get(CallSid)
    if state is None:
        # Process restarted mid-call, or an unknown CallSid.
        log.warning("turn.unknown_call", call_sid=CallSid)
        clip = await _clip_url("Sorry, this session expired. Please call again.")
        return _play_and_hangup(clip)

    state["turns"] += 1
    if state["turns"] > _MAX_TURNS:
        log.warning("turn.max_turns", call_sid=CallSid)
        clip = await _clip_url(
            "Thanks for calling. I'll pass on what I have. Our team will follow up. Stay safe."
        )
        return _play_and_hangup(clip)

    transcript = ""
    if RecordingUrl and RecordingDuration not in ("", "0"):
        audio = await openai_voice.fetch_twilio_recording(RecordingUrl)
        if audio:
            try:
                transcript = await openai_voice.transcribe(audio)
            except Exception:
                log.exception("turn.stt_failed", call_sid=CallSid)

    log.info("turn.transcript", call_sid=CallSid, turn=state["turns"], text=transcript[:120])

    if not transcript:
        clip = await _clip_url(_DID_NOT_CATCH)
        return _play_and_record(clip)

    state["history"].append({"role": "user", "content": transcript})

    try:
        reply, report_args = await openai_voice.next_turn(state["history"])
    except Exception:
        log.exception("turn.chat_failed", call_sid=CallSid)
        clip = await _clip_url("Sorry, something went wrong on our side. Please call again.")
        return _play_and_hangup(clip)

    if report_args:
        short_ref = await _save_report(CallSid, state, report_args)
        closing = reply or "Thank you, your report is submitted."
        if short_ref:
            closing = (
                f"Thank you. Your report is submitted. Your reference is {short_ref}. "
                "Our team is reviewing it now. Stay safe."
            )
        _calls.pop(CallSid, None)
        clip = await _clip_url(closing)
        return _play_and_hangup(clip)

    if not reply:
        reply = _DID_NOT_CATCH
    state["history"].append({"role": "assistant", "content": reply})
    clip = await _clip_url(reply)
    return _play_and_record(clip)


async def _save_report(call_sid: str, state: dict[str, Any], args: dict[str, Any]) -> str | None:
    """Persist the completed report; returns the caller-facing short ref."""
    try:
        call_state = openai_voice.build_call_state(
            state["report_id"], call_sid, state["caller_hash"], args
        )
        submitted = await SqlReportSink(get_session_maker()).write(call_state)
        log.info("turn.report_saved", call_sid=call_sid, short_ref=submitted.short_ref, args=args)
        return submitted.short_ref
    except Exception:
        log.exception("turn.report_save_failed", call_sid=call_sid)
        return None
