"""Flux WS transport — connect, send, event iterator, close.

Uses a fake connector so no real network or Deepgram key is needed;
production behaviour is proven by exercising the exact contract the
real websockets library exposes (`send` / `recv` / `close`)."""

from __future__ import annotations

import asyncio
import json
from collections import deque

import pytest

from fg_voice.pipeline.stt_flux import FluxAction, FluxEventKind, action_for
from fg_voice.pipeline.stt_flux_ws import (
    FLUX_WSS_BASE,
    FluxClient,
    FluxConfig,
    FluxTransportError,
    build_url,
)

# ─── Fakes ──────────────────────────────────────────────────────────


class _FakeWS:
    """Scripted WS double. `_recv_script` is a deque of items each
    `recv()` call returns; a `ConnectionClosedError` marker string
    ends the stream. `sent` records everything the client sends."""

    def __init__(self, recv_script: list[str | bytes | Exception]) -> None:
        self._recv_script: deque[str | bytes | Exception] = deque(recv_script)
        self.sent: list[str | bytes] = []
        self.closed: bool = False

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)

    async def recv(self) -> str | bytes:
        if not self._recv_script:
            # Simulate a server-side close when the script is exhausted.
            raise _FakeConnectionClosed("end of script")
        item = self._recv_script.popleft()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


class _FakeConnectionClosed(Exception):
    """Named to match the WS library's class-name substring check."""

    def __init__(self, msg: str) -> None:
        super().__init__(msg)


_FakeConnectionClosed.__name__ = "ConnectionClosedOK"


def _connector_from(ws: _FakeWS):  # type: ignore[no-untyped-def]
    async def _connect(url, *, headers, timeout_sec):  # type: ignore[no-untyped-def]
        # Capture the last-used args on the ws for assertions.
        ws.last_url = url  # type: ignore[attr-defined]
        ws.last_headers = headers  # type: ignore[attr-defined]
        return ws

    return _connect


# ─── URL builder ────────────────────────────────────────────────────


def test_build_url_includes_all_params() -> None:
    config = FluxConfig(
        api_key="dg_xyz",
        eot_threshold=0.72,
        eot_timeout_ms=1500,
        keyterms=("storm surge", "cyclone"),
    )
    url = build_url(config)
    assert url.startswith(FLUX_WSS_BASE + "?")
    # Core knobs make it in.
    assert "model=flux-general-en" in url
    assert "encoding=mulaw" in url
    assert "sample_rate=8000" in url
    assert "eot_threshold=0.72" in url
    assert "eot_timeout_ms=1500" in url
    # Keyterms round-trip through URL encoding — space becomes +.
    assert "keyterm=storm+surge" in url
    assert "keyterm=cyclone" in url


def test_build_url_empty_keyterms() -> None:
    url = build_url(FluxConfig(api_key="k"))
    assert "keyterm=" not in url


# ─── Client lifecycle ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_sets_auth_header() -> None:
    ws = _FakeWS(recv_script=[])
    client = FluxClient(config=FluxConfig(api_key="dg_secret"), connector=_connector_from(ws))
    await client.connect()
    assert ws.last_headers == {"Authorization": "Token dg_secret"}  # type: ignore[attr-defined]
    await client.close()
    assert ws.closed


@pytest.mark.asyncio
async def test_connect_twice_raises() -> None:
    ws = _FakeWS(recv_script=[])
    client = FluxClient(config=FluxConfig(api_key="k"), connector=_connector_from(ws))
    await client.connect()
    with pytest.raises(FluxTransportError, match="already connected"):
        await client.connect()
    await client.close()


@pytest.mark.asyncio
async def test_connect_timeout_maps_to_transport_error() -> None:
    async def slow_connector(url, *, headers, timeout_sec):  # type: ignore[no-untyped-def]
        raise TimeoutError()

    client = FluxClient(
        config=FluxConfig(api_key="k"),
        connector=slow_connector,
        connect_timeout_sec=0.01,
    )
    with pytest.raises(FluxTransportError, match="timed out"):
        await client.connect()


@pytest.mark.asyncio
async def test_send_audio_forwards_bytes() -> None:
    ws = _FakeWS(recv_script=[])
    client = FluxClient(config=FluxConfig(api_key="k"), connector=_connector_from(ws))
    await client.connect()
    await client.send_audio(b"\x00" * 160)
    assert ws.sent == [b"\x00" * 160]
    await client.close()


@pytest.mark.asyncio
async def test_send_audio_before_connect_noops() -> None:
    ws = _FakeWS(recv_script=[])
    client = FluxClient(config=FluxConfig(api_key="k"), connector=_connector_from(ws))
    # Never connected — send should not raise.
    await client.send_audio(b"\x00")
    assert ws.sent == []


@pytest.mark.asyncio
async def test_send_config_emits_configure_frame() -> None:
    ws = _FakeWS(recv_script=[])
    client = FluxClient(config=FluxConfig(api_key="k"), connector=_connector_from(ws))
    await client.connect()
    await client.send_config({"eot_threshold": 0.8, "eot_timeout_ms": 800})
    assert len(ws.sent) == 1
    frame = json.loads(ws.sent[0])
    assert frame == {
        "type": "Configure",
        "config": {"eot_threshold": 0.8, "eot_timeout_ms": 800},
    }
    await client.close()


# ─── Event iteration ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_events_iterator_parses_and_yields() -> None:
    script: list[str | bytes | Exception] = [
        json.dumps({"type": "StartOfTurn"}),
        json.dumps({"type": "EagerEndOfTurn", "transcript": "moder"}),
        json.dumps({"type": "EndOfTurn", "transcript": "moderate", "confidence": 0.9}),
    ]
    ws = _FakeWS(recv_script=script)
    client = FluxClient(config=FluxConfig(api_key="k"), connector=_connector_from(ws))
    await client.connect()

    events = []
    async for e in client.events():
        events.append(e)

    kinds = [e.kind for e in events]
    assert kinds == [
        FluxEventKind.START_OF_TURN,
        FluxEventKind.EAGER_END_OF_TURN,
        FluxEventKind.END_OF_TURN,
    ]
    assert action_for(events[-1]) is FluxAction.COMMIT_TURN
    assert events[-1].transcript == "moderate"
    await client.close()


@pytest.mark.asyncio
async def test_events_skips_malformed_frames() -> None:
    script: list[str | bytes | Exception] = [
        "not json at all",
        json.dumps({"type": "EndOfTurn", "transcript": "ok"}),
    ]
    ws = _FakeWS(recv_script=script)
    client = FluxClient(config=FluxConfig(api_key="k"), connector=_connector_from(ws))
    await client.connect()

    events = [e async for e in client.events()]
    assert len(events) == 1
    assert events[0].kind is FluxEventKind.END_OF_TURN
    await client.close()


@pytest.mark.asyncio
async def test_events_server_close_terminates_cleanly() -> None:
    # Empty script → recv raises _FakeConnectionClosed on first call.
    ws = _FakeWS(recv_script=[])
    client = FluxClient(config=FluxConfig(api_key="k"), connector=_connector_from(ws))
    await client.connect()
    events = [e async for e in client.events()]
    assert events == []
    await client.close()


@pytest.mark.asyncio
async def test_events_transport_error_on_unexpected_exception() -> None:
    ws = _FakeWS(recv_script=[RuntimeError("network gone")])
    client = FluxClient(config=FluxConfig(api_key="k"), connector=_connector_from(ws))
    await client.connect()
    with pytest.raises(FluxTransportError, match="recv failed"):
        _ = [e async for e in client.events()]
    await client.close()


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    ws = _FakeWS(recv_script=[])
    client = FluxClient(config=FluxConfig(api_key="k"), connector=_connector_from(ws))
    await client.connect()
    await client.close()
    await client.close()  # second call must not raise
    assert ws.closed


@pytest.mark.asyncio
async def test_events_before_connect_raises() -> None:
    client = FluxClient(config=FluxConfig(api_key="k"), connector=_connector_from(_FakeWS([])))
    with pytest.raises(FluxTransportError, match="not connected"):
        _ = [e async for e in client.events()]


# ─── Integration with keyterms module ──────────────────────────────


@pytest.mark.asyncio
async def test_config_carries_keyterms_from_builder() -> None:
    from fg_voice.rag.keyterms import build_keyterms

    ws = _FakeWS(recv_script=[])
    config = FluxConfig(api_key="k", keyterms=tuple(build_keyterms()[:5]))
    client = FluxClient(config=config, connector=_connector_from(ws))
    await client.connect()
    assert "keyterm=" in ws.last_url  # type: ignore[attr-defined]
    await client.close()


# Small guard so pytest picks up async tests without a session-scoped
# conftest override.
_ = asyncio  # keep the import warm
