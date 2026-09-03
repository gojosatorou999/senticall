"""OpenAI TTS / STT / reasoning for the turn-based (Gather+Record) call flow.

Twilio Media Streams is a paid feature, so the realtime full-duplex path
in `openai_client.py` cannot run on a trial account. This module backs
the turn-based fallback instead: every caller-facing utterance is
synthesised by OpenAI TTS and played with `<Play>`, and every caller
utterance is recorded by Twilio and transcribed by OpenAI — so neither
Twilio's TTS nor its ASR is in the audio path.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID

import httpx

from fg_voice.config import get_settings
from fg_voice.conversation.state import CallState, Slot, SlotValue
from fg_voice.obs.logging import get_logger

log = get_logger(__name__)

_OPENAI_BASE = "https://api.openai.com/v1"

TTS_MODEL = "gpt-4o-mini-tts"
TTS_VOICE = "alloy"
STT_MODEL = "gpt-4o-transcribe"
CHAT_MODEL = "gpt-4.1-mini"

# Biases transcription toward the hazard vocabulary + Indian coastal
# place names. Without it, "tsunami" and village names come back as
# plausible-sounding neighbours.
_STT_PROMPT = (
    "Coastal disaster hotline in India. Expect words like floods, flooding, tsunami, "
    "storm, cyclone, sludge, oil spill, high tide, abnormal tide, erosion, waterlogging, "
    "nala overflow, ankle deep, knee deep, waist deep, light, moderate, extreme, "
    "and Indian place names such as Visakhapatnam, Kakinada, Anaparthi, Machilipatnam, "
    "Bheemunipatnam, Chennai, Puri, Digha."
)

_SYSTEM_PROMPT = """You are Sentinel, a calm and concise AI voice agent for a coastal \
disaster hotline in India. You are speaking to a caller on the phone.

Collect these four things, ONE question at a time, in this order:
1) Hazard type — the disaster type. Offer these options aloud: floods, tsunami, storm \
damage, sludge or oil, unusual tides, or something else.
2) Location — a beach, landmark, town, or village name.
3) Severity — light, moderate, or extreme.
4) A short description of what they are seeing.

Rules:
- Keep every reply to ONE short sentence. This is a phone call, not a chat.
- Extract EVERY field the caller has already given, even if they volunteer several at \
once, and even if they use different words ("flooding"/"water logging" = flood, \
"tidal wave" = tsunami, "very bad"/"really severe" = extreme, "knee deep" implies flood). \
Then ask ONLY for what is still missing.
- NEVER re-ask something the caller has already answered. Doing so is the worst \
possible failure here — callers may be in danger and have no patience for it.
- If the caller says anyone is hurt or trapped, tell them to hang up and call 112, then \
continue if they stay on.
- Only if a reply is genuinely unintelligible or empty, briefly re-ask that one question.
- Never invent a description. `description` must be the caller's own account of what they \
are seeing, and you must have asked for it at least once before saving.
- Call save_report ONLY on a turn where you are not also asking a question. If you still \
need something, ask for it and do not call the function yet.
- As soon as all four fields are genuinely known, call save_report. Do not ask for \
confirmation first.
"""

_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "save_report",
            "description": "Save the collected hazard report. Call only once all four fields are known.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hazard_type": {
                        "type": "string",
                        "enum": [
                            "flood",
                            "tsunami",
                            "storm",
                            "sludge_oil",
                            "abnormal_tide",
                            "erosion",
                            "other",
                        ],
                    },
                    "location": {"type": "string"},
                    "severity": {"type": "string", "enum": ["light", "moderate", "extreme"]},
                    "description": {"type": "string"},
                },
                "required": ["hazard_type", "location", "severity", "description"],
            },
        },
    }
]


def _headers() -> dict[str, str]:
    key = get_settings().openai_api_key.get_secret_value()
    return {"Authorization": f"Bearer {key}"}


async def synthesize(text: str) -> bytes:
    """OpenAI TTS → mp3 bytes, ready to serve to Twilio's <Play>."""
    payload = {
        "model": TTS_MODEL,
        "input": text,
        "voice": TTS_VOICE,
        "response_format": "mp3",
        "instructions": (
            "Speak clearly and calmly, at a measured pace, like an emergency dispatcher "
            "on an Indian coastal hotline. Warm but efficient."
        ),
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(f"{_OPENAI_BASE}/audio/speech", headers=_headers(), json=payload)
        resp.raise_for_status()
        return resp.content


async def transcribe(audio: bytes, filename: str = "audio.wav") -> str:
    """OpenAI STT on a Twilio recording → transcript text ("" if silent)."""
    files = {"file": (filename, audio, "audio/wav")}
    data = {"model": STT_MODEL, "prompt": _STT_PROMPT, "response_format": "json"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{_OPENAI_BASE}/audio/transcriptions", headers=_headers(), files=files, data=data
        )
        resp.raise_for_status()
        return (resp.json().get("text") or "").strip()


async def next_turn(history: list[dict[str, Any]]) -> tuple[str, dict[str, Any] | None]:
    """Run one conversational step.

    Returns `(reply_text, report_args)`. `report_args` is non-None when the
    model decided the report is complete, in which case the caller should
    be thanked and the call ended."""
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}, *history]
    payload = {
        "model": CHAT_MODEL,
        "messages": messages,
        "tools": _TOOLS,
        "tool_choice": "auto",
        "temperature": 0.3,
        "max_tokens": 200,
    }
    async with httpx.AsyncClient(timeout=25.0) as client:
        resp = await client.post(
            f"{_OPENAI_BASE}/chat/completions", headers=_headers(), json=payload
        )
        resp.raise_for_status()
        choice = resp.json()["choices"][0]["message"]

    tool_calls = choice.get("tool_calls") or []
    if tool_calls:
        call = tool_calls[0]
        try:
            args = json.loads(call["function"]["arguments"])
        except (KeyError, json.JSONDecodeError):
            args = {}
        return (choice.get("content") or "").strip(), args

    return (choice.get("content") or "").strip(), None


def build_call_state(report_id: str, call_sid: str, caller_hash: str, args: dict) -> CallState:
    """Project `save_report` tool-call arguments onto a minimal `CallState`
    so `SqlReportSink.write` persists it exactly as the slot-filling graph
    would. Shared by the realtime and turn-based paths."""
    state = CallState(
        call_sid=call_sid,
        report_id=UUID(report_id),
        caller_hash=caller_hash,
        direction="inbound",
    )
    for key, slot in (
        ("hazard_type", Slot.HAZARD_TYPE),
        ("location", Slot.LOCATION),
        ("severity", Slot.SEVERITY),
        ("description", Slot.DESCRIPTION),
    ):
        value = args.get(key)
        if value:
            state.set_slot(slot, SlotValue(value=str(value), confidence=1.0, source="llm"))
    return state


async def fetch_twilio_recording(recording_url: str) -> bytes | None:
    """Download a Twilio recording as WAV.

    Twilio's `action` webhook can fire a beat before the media is
    retrievable, so this retries briefly rather than failing the turn."""
    settings = get_settings()
    auth = (settings.twilio_account_sid, settings.twilio_auth_token.get_secret_value())
    url = recording_url if recording_url.endswith(".wav") else f"{recording_url}.wav"

    async with httpx.AsyncClient(timeout=15.0, auth=auth) as client:
        for attempt in range(5):
            try:
                resp = await client.get(url)
                if resp.status_code == 200 and resp.content:
                    return resp.content
                log.info("recording.retry", attempt=attempt, status=resp.status_code)
            except httpx.HTTPError as exc:
                log.info("recording.retry_error", attempt=attempt, error=str(exc))
            await asyncio.sleep(0.6)
    log.warning("recording.unavailable", url=url)
    return None
