"""Twilio Media Streams WebSocket frame codec.

The protocol is JSON envelopes over a single WS. Six inbound event
types, four outbound. See spec §15.3.

We keep this file *pure*: no I/O, no config, no logging. Route handlers
own the socket; this module owns the wire format."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Literal


# ── inbound events ───────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class ConnectedEvent:
    """First message after WS open. Nothing to do."""

    protocol: str
    version: str


@dataclass(frozen=True, slots=True)
class StartEvent:
    """Fired once. Extract `streamSid` + custom Parameters and initialise
    CallState."""

    stream_sid: str
    call_sid: str
    account_sid: str
    tracks: tuple[str, ...]
    custom_parameters: dict[str, str]
    media_format_encoding: str
    media_format_sample_rate: int
    media_format_channels: int


@dataclass(frozen=True, slots=True)
class MediaEvent:
    """A 20 ms μ-law frame from the caller."""

    stream_sid: str
    track: Literal["inbound", "outbound"]
    chunk_index: int
    timestamp_ms: int
    payload: bytes  # already base64-decoded


@dataclass(frozen=True, slots=True)
class DtmfEvent:
    """A DTMF keypress. DTMF always wins over concurrent ASR (§15.3)."""

    stream_sid: str
    digit: str
    track: Literal["inbound", "outbound"]


@dataclass(frozen=True, slots=True)
class StopEvent:
    """Twilio hung up. Finalise + enqueue post-call DAG."""

    stream_sid: str
    account_sid: str
    call_sid: str


@dataclass(frozen=True, slots=True)
class MarkEvent:
    """Playback of an outbound `mark` we sent earlier has completed.
    Used for timing (when did our prompt actually finish playing?)."""

    stream_sid: str
    name: str


InboundEvent = ConnectedEvent | StartEvent | MediaEvent | DtmfEvent | StopEvent | MarkEvent


class UnknownEventError(ValueError):
    """Twilio added a new event we don't handle. Log and continue."""


def parse_inbound(raw: str | bytes) -> InboundEvent:
    """Parse one JSON envelope from the Media Streams WSS.

    Raises `UnknownEventError` for unrecognised event types so callers
    can log-and-skip rather than crash the WS."""
    msg = json.loads(raw)
    event = msg.get("event")
    if event == "connected":
        return ConnectedEvent(protocol=msg.get("protocol", ""), version=msg.get("version", ""))
    if event == "start":
        start = msg["start"]
        fmt = start.get("mediaFormat", {})
        return StartEvent(
            stream_sid=start["streamSid"],
            call_sid=start["callSid"],
            account_sid=start.get("accountSid", ""),
            tracks=tuple(start.get("tracks", ())),
            custom_parameters=dict(start.get("customParameters", {})),
            media_format_encoding=fmt.get("encoding", "audio/x-mulaw"),
            media_format_sample_rate=int(fmt.get("sampleRate", 8000)),
            media_format_channels=int(fmt.get("channels", 1)),
        )
    if event == "media":
        media = msg["media"]
        return MediaEvent(
            stream_sid=msg.get("streamSid", ""),
            track=media.get("track", "inbound"),
            chunk_index=int(media.get("chunk", 0)),
            timestamp_ms=int(media.get("timestamp", 0)),
            payload=base64.b64decode(media["payload"]),
        )
    if event == "dtmf":
        return DtmfEvent(
            stream_sid=msg.get("streamSid", ""),
            digit=str(msg["dtmf"]["digit"]),
            track=msg["dtmf"].get("track", "inbound"),
        )
    if event == "stop":
        stop = msg["stop"]
        return StopEvent(
            stream_sid=msg.get("streamSid", ""),
            account_sid=stop.get("accountSid", ""),
            call_sid=stop.get("callSid", ""),
        )
    if event == "mark":
        return MarkEvent(
            stream_sid=msg.get("streamSid", ""),
            name=msg["mark"]["name"],
        )
    raise UnknownEventError(f"Unknown Twilio media event: {event!r}")


# ── outbound envelopes ───────────────────────────────────────────
def build_media(stream_sid: str, ulaw_payload: bytes) -> str:
    """Outbound audio frame. Payload must be μ-law-encoded PCM."""
    return json.dumps(
        {
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": base64.b64encode(ulaw_payload).decode("ascii")},
        },
        separators=(",", ":"),
    )


def build_mark(stream_sid: str, name: str) -> str:
    """Ask Twilio to notify us when everything sent before this mark has
    finished playing to the caller. Used to time when a prompt finished
    so the no-input timeout can start."""
    return json.dumps(
        {"event": "mark", "streamSid": stream_sid, "mark": {"name": name}},
        separators=(",", ":"),
    )


def build_clear(stream_sid: str) -> str:
    """Flush buffered outbound audio. Fired on barge-in."""
    return json.dumps(
        {"event": "clear", "streamSid": stream_sid},
        separators=(",", ":"),
    )


def envelope_kind(raw: str | bytes) -> str:
    """Cheap inspection without full parsing — useful for structured logs."""
    try:
        obj: dict[str, Any] = json.loads(raw)
        return str(obj.get("event", "unknown"))
    except json.JSONDecodeError:
        return "invalid_json"
