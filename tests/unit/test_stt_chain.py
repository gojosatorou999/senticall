"""Unit tests for STTProviderChain.

Tests the fallback logic and DTMF injection without real WS connections.
Uses a fake Deepgram slot to avoid needing a Deepgram API key.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
import pytest_asyncio

from fg_voice.conversation.runner import InputEvent
from fg_voice.pipeline.circuit_breaker import CircuitBreaker
from fg_voice.pipeline.stt_chain import (
    AllSTTProvidersFailedError,
    STTProviderChain,
    _DeepgramSlot,
)
from fg_voice.pipeline.stt_flux import FluxEvent, FluxEventKind
from fg_voice.pipeline.stt_flux_ws import FluxConfig, FluxTransportError


# ── Fakes ──────────────────────────────────────────────────────────────


class _AlwaysFailConnector:
    """Fake WS connector that always raises — simulates Deepgram being down."""

    async def __call__(self, url: str, *, headers: dict, timeout_sec: float):
        raise FluxTransportError("Deepgram is down")


class _NeverConnectSlot:
    """Fake slot that always raises on connect."""

    circuit: CircuitBreaker

    def __init__(self) -> None:
        self.circuit = CircuitBreaker(failure_threshold=1, reset_sec=999.0, name="fake")

    async def connect(self) -> None:
        exc = FluxTransportError("Always down")
        self.circuit.on_failure(exc)
        raise exc

    async def send_audio(self, chunk: bytes) -> None:
        pass

    async def next_event(self, timeout_ms: int) -> InputEvent | None:
        return None

    async def close(self) -> None:
        pass


class _ScriptedSlot:
    """Fake slot that connects successfully and emits a scripted event sequence."""

    circuit: CircuitBreaker
    _event_queue: asyncio.Queue

    def __init__(self, events: list[InputEvent]) -> None:
        self.circuit = CircuitBreaker(failure_threshold=3, reset_sec=30.0, name="scripted")
        self._event_queue = asyncio.Queue()
        for evt in events:
            self._event_queue.put_nowait(evt)

    async def connect(self) -> None:
        self.circuit.on_success()

    async def send_audio(self, chunk: bytes) -> None:
        pass

    async def next_event(self, timeout_ms: int) -> InputEvent | None:
        try:
            return self._event_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def close(self) -> None:
        pass


# ── Tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_all_providers_failed_raises() -> None:
    chain = STTProviderChain(slots=[_NeverConnectSlot(), _NeverConnectSlot()])
    with pytest.raises(AllSTTProvidersFailedError):
        await chain.connect()


@pytest.mark.asyncio
async def test_falls_to_second_slot_when_first_fails() -> None:
    end_of_turn = InputEvent(
        kind="flux",
        flux_event=FluxEvent(
            kind=FluxEventKind.END_OF_TURN,
            transcript="test",
            confidence=0.9,
        ),
    )
    scripted = _ScriptedSlot([end_of_turn])
    chain = STTProviderChain(slots=[_NeverConnectSlot(), scripted])
    await chain.connect()
    assert chain._active is scripted
    evt = await chain.next_event(timeout_ms=100)
    assert evt is not None
    assert evt.kind == "flux"
    assert evt.flux_event is not None
    assert evt.flux_event.transcript == "test"


@pytest.mark.asyncio
async def test_inject_event_reaches_active_slot() -> None:
    scripted = _ScriptedSlot([])
    chain = STTProviderChain(slots=[scripted])
    await chain.connect()

    dtmf = InputEvent(kind="dtmf", dtmf_digit="5")
    chain.inject_event(dtmf)

    evt = await chain.next_event(timeout_ms=50)
    assert evt is not None
    assert evt.kind == "dtmf"
    assert evt.dtmf_digit == "5"


@pytest.mark.asyncio
async def test_send_audio_no_crash_when_slot_connected() -> None:
    scripted = _ScriptedSlot([])
    chain = STTProviderChain(slots=[scripted])
    await chain.connect()
    # Must not raise.
    await chain.send_audio(b"\x00" * 160)


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    scripted = _ScriptedSlot([])
    chain = STTProviderChain(slots=[scripted])
    await chain.connect()
    await chain.close()
    await chain.close()  # Must not raise.
