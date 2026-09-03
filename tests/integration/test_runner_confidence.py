"""Runner-level integration of the confidence gates (§9.4).

- Low STT confidence → runner drops the transcript before extraction,
  ladder advances by one attempt on the same node.
- Low extraction confidence → runner drops the slot value, ladder
  advances (extractor was invoked but its output was rejected)."""

from __future__ import annotations

from pathlib import Path

import pytest

from fg_voice.audio.bank import AudioBank, Clip
from fg_voice.conversation.graph import build_graph
from fg_voice.conversation.prompt_bank import load_prompt_bank
from fg_voice.conversation.runner import (
    ConversationRunner,
    Hangup,
    InputEvent,
    RunnerConfig,
)
from fg_voice.conversation.state import CallState, NodeId
from fg_voice.conversation.state_store import CallStateStore
from fg_voice.pipeline.stt_flux import FluxEvent, FluxEventKind


class _NoopStore(CallStateStore):
    async def save(self, state: CallState) -> None:
        return None

    async def load(self, call_sid: str) -> CallState | None:
        return None


class _ScriptedInput:
    def __init__(self, events: list[InputEvent | None]) -> None:
        self._events = list(events)

    async def next_event(self, timeout_ms: int) -> InputEvent | None:
        if not self._events:
            raise Hangup()
        return self._events.pop(0)


class _RecordingSink:
    def __init__(self) -> None:
        self.clips: list[Clip] = []
        self.clears: list[str] = []

    async def play_clip(self, clip: Clip) -> None:
        self.clips.append(clip)

    async def send_clear(self, message: str) -> None:
        self.clears.append(message)


def _bank() -> AudioBank:
    return AudioBank(clips={}, root=Path("/tmp/x"), locale="en-IN", version="test")


def _eot(text: str, *, confidence: float = 0.9) -> InputEvent:
    return InputEvent(
        kind="flux",
        flux_event=FluxEvent(
            kind=FluxEventKind.END_OF_TURN,
            transcript=text,
            confidence=confidence,
        ),
    )


@pytest.mark.asyncio
async def test_low_stt_confidence_drops_transcript_and_advances_ladder() -> None:
    """Flux emits confidence=0.3 on the ASK_INTENT turn. Runner MUST
    NOT persist a slot value — it MUST advance the reprompt ladder,
    play `reprompt_intent_1` on the next turn."""
    graph = build_graph()
    bank = load_prompt_bank()
    scripted = _ScriptedInput(
        [
            _eot("yes", confidence=0.3),  # below 0.55 → drop
            _eot("yes", confidence=0.9),  # this one passes
            _eot("storm"),
            _eot("tree down"),
            _eot("kakinada"),
        ]
    )
    runner = ConversationRunner(
        call_state=CallState(call_sid="CA1", caller_hash="h"),
        graph=graph,
        prompt_bank=bank,
        audio_bank=_bank(),
        turn_input=scripted,
        audio_sink=_RecordingSink(),
        state_store=_NoopStore(),
        config=RunnerConfig(max_call_duration_sec=10),
    )
    await runner.run()

    # After the low-confidence drop, the runner played reprompt_intent_1
    # before the caller was heard again.
    assert "reprompt_intent_1" in runner.prompt_trail


@pytest.mark.asyncio
async def test_high_stt_confidence_passes_through() -> None:
    """Regression guard: a high-confidence "yes" on the first turn
    should NOT trigger reprompt_intent_1."""
    graph = build_graph()
    bank = load_prompt_bank()
    scripted = _ScriptedInput(
        [
            _eot("yes", confidence=0.9),
            _eot("storm"),
            _eot("tree down"),
            _eot("kakinada"),
        ]
    )
    runner = ConversationRunner(
        call_state=CallState(call_sid="CA2", caller_hash="h"),
        graph=graph,
        prompt_bank=bank,
        audio_bank=_bank(),
        turn_input=scripted,
        audio_sink=_RecordingSink(),
        state_store=_NoopStore(),
        config=RunnerConfig(max_call_duration_sec=10),
    )
    await runner.run()

    assert "reprompt_intent_1" not in runner.prompt_trail
    assert runner.state.current_node in (
        NodeId.CONFIRM_SUMMARY,
        NodeId.CONFIRM_LOCATION_LOW_CONF,
        NodeId.SUBMIT,
        NodeId.SUBMITTED,
        NodeId.END,
        NodeId.RESOLVE_LOCATION,
        NodeId.ASK_LOCATION,  # low-confidence-guard path
    )


@pytest.mark.asyncio
async def test_stt_none_confidence_treated_as_pass() -> None:
    """A COMMIT_TURN with confidence=None (control frame edge case)
    must not gate the transcript out — the runner has no basis to
    reject."""
    graph = build_graph()
    bank = load_prompt_bank()
    scripted = _ScriptedInput(
        [
            InputEvent(
                kind="flux",
                flux_event=FluxEvent(
                    kind=FluxEventKind.END_OF_TURN,
                    transcript="yes",
                    confidence=None,
                ),
            ),
            _eot("storm"),
            _eot("tree down"),
            _eot("kakinada"),
        ]
    )
    runner = ConversationRunner(
        call_state=CallState(call_sid="CA3", caller_hash="h"),
        graph=graph,
        prompt_bank=bank,
        audio_bank=_bank(),
        turn_input=scripted,
        audio_sink=_RecordingSink(),
        state_store=_NoopStore(),
        config=RunnerConfig(max_call_duration_sec=10),
    )
    await runner.run()
    assert "reprompt_intent_1" not in runner.prompt_trail
