"""Media Streams WebSocket echo bot — end-to-end over an in-process
TestClient. Proves the wire format works both ways without needing a
real Twilio call."""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(dev_env: None) -> TestClient:
    from fg_voice.main import app

    return TestClient(app)


def _start_msg() -> str:
    return json.dumps(
        {
            "event": "start",
            "streamSid": "MZ_TEST",
            "start": {
                "streamSid": "MZ_TEST",
                "callSid": "CA_TEST",
                "accountSid": "AC_TEST",
                "tracks": ["inbound"],
                "customParameters": {
                    "report_id": "rid-1",
                    "caller_hash": "hash-1",
                    "locale": "en-IN",
                },
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
            },
        }
    )


def _media_msg(payload: bytes) -> str:
    return json.dumps(
        {
            "event": "media",
            "streamSid": "MZ_TEST",
            "media": {
                "track": "inbound",
                "chunk": "1",
                "timestamp": "20",
                "payload": base64.b64encode(payload).decode(),
            },
        }
    )


def _stop_msg() -> str:
    return json.dumps(
        {
            "event": "stop",
            "streamSid": "MZ_TEST",
            "stop": {"accountSid": "AC_TEST", "callSid": "CA_TEST"},
        }
    )


def test_echo_bot_returns_greeting_then_echoes(client: TestClient) -> None:
    with client.websocket_connect("/ws/media") as ws:
        ws.send_text(_start_msg())

        # Drain the greeting frames; expect at least one media event and
        # eventually a mark named `greeting_end`.
        saw_media = False
        saw_mark = False
        for _ in range(400):  # plenty of headroom for the beep
            raw = ws.receive_text()
            msg = json.loads(raw)
            if msg["event"] == "media":
                saw_media = True
            elif msg["event"] == "mark" and msg["mark"]["name"] == "greeting_end":
                saw_mark = True
                break
        assert saw_media, "greeting produced no media frames"
        assert saw_mark, "greeting_end mark never arrived"

        # Now send one caller frame and expect it back verbatim.
        caller_frame = bytes([0x7F, 0x80] * 80)  # 160 bytes = one 20 ms frame
        ws.send_text(_media_msg(caller_frame))
        echoed = json.loads(ws.receive_text())
        assert echoed["event"] == "media"
        assert base64.b64decode(echoed["media"]["payload"]) == caller_frame

        ws.send_text(_stop_msg())
