"""Twilio Media Streams wire-format codec."""

from __future__ import annotations

import base64
import json

import pytest

from fg_voice.telephony.twilio_stream import (
    ConnectedEvent,
    DtmfEvent,
    MarkEvent,
    MediaEvent,
    StartEvent,
    StopEvent,
    UnknownEventError,
    build_clear,
    build_mark,
    build_media,
    envelope_kind,
    parse_inbound,
)


def test_parse_connected() -> None:
    evt = parse_inbound(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
    assert isinstance(evt, ConnectedEvent)
    assert evt.version == "1.0.0"


def test_parse_start() -> None:
    payload = {
        "event": "start",
        "streamSid": "MZxxx",
        "start": {
            "streamSid": "MZxxx",
            "callSid": "CAxxx",
            "accountSid": "ACxxx",
            "tracks": ["inbound"],
            "customParameters": {"report_id": "abc", "caller_hash": "def", "locale": "en-IN"},
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
        },
    }
    evt = parse_inbound(json.dumps(payload))
    assert isinstance(evt, StartEvent)
    assert evt.stream_sid == "MZxxx"
    assert evt.call_sid == "CAxxx"
    assert evt.custom_parameters == {"report_id": "abc", "caller_hash": "def", "locale": "en-IN"}
    assert evt.media_format_sample_rate == 8000


def test_parse_media_decodes_base64() -> None:
    payload_bytes = b"\xff" * 160
    payload = {
        "event": "media",
        "streamSid": "MZxxx",
        "media": {
            "track": "inbound",
            "chunk": "1",
            "timestamp": "20",
            "payload": base64.b64encode(payload_bytes).decode(),
        },
    }
    evt = parse_inbound(json.dumps(payload))
    assert isinstance(evt, MediaEvent)
    assert evt.payload == payload_bytes
    assert evt.chunk_index == 1
    assert evt.timestamp_ms == 20


def test_parse_dtmf() -> None:
    evt = parse_inbound(
        json.dumps(
            {"event": "dtmf", "streamSid": "MZxxx", "dtmf": {"digit": "5", "track": "inbound"}}
        )
    )
    assert isinstance(evt, DtmfEvent)
    assert evt.digit == "5"


def test_parse_stop() -> None:
    evt = parse_inbound(
        json.dumps(
            {
                "event": "stop",
                "streamSid": "MZxxx",
                "stop": {"accountSid": "ACxxx", "callSid": "CAxxx"},
            }
        )
    )
    assert isinstance(evt, StopEvent)
    assert evt.call_sid == "CAxxx"


def test_parse_mark() -> None:
    evt = parse_inbound(
        json.dumps({"event": "mark", "streamSid": "MZxxx", "mark": {"name": "greeting_end"}})
    )
    assert isinstance(evt, MarkEvent)
    assert evt.name == "greeting_end"


def test_parse_unknown_raises() -> None:
    with pytest.raises(UnknownEventError):
        parse_inbound(json.dumps({"event": "future_thing"}))


def test_build_media_wraps_base64() -> None:
    raw = build_media("MZxxx", b"\x00\x01\x02")
    msg = json.loads(raw)
    assert msg["event"] == "media"
    assert msg["streamSid"] == "MZxxx"
    assert base64.b64decode(msg["media"]["payload"]) == b"\x00\x01\x02"


def test_build_mark() -> None:
    msg = json.loads(build_mark("MZxxx", "prompt_end"))
    assert msg == {"event": "mark", "streamSid": "MZxxx", "mark": {"name": "prompt_end"}}


def test_build_clear() -> None:
    msg = json.loads(build_clear("MZxxx"))
    assert msg == {"event": "clear", "streamSid": "MZxxx"}


def test_envelope_kind_no_parse() -> None:
    assert envelope_kind('{"event":"media","x":1}') == "media"
    assert envelope_kind("not-json") == "invalid_json"
