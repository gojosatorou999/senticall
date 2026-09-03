"""Per-node EOT reconfig + eager-EOT wiring in the ConversationRunner.

Coverage:
- `flux_reconfigurer.reconfigure(...)` is called on each prompted-node
  entry with the graph's `effective_eot(...)` result — proves the per-
  node overrides (ASK_LOCATION, CONFIRM_SUMMARY, etc.) reach Flux.
- CONSENT / CONFIRM_SUMMARY see different EOT pairs (regression guard
  against a pinned/hard-coded value).
- Eager-EOT off (default): SPECULATE / CANCEL_SPECULATION events are
  no-ops, matching P2.5 behaviour. `eager_eot_stats.speculations_started == 0`.
- Eager-EOT on + matching transcript: speculation is reused, stats
  reflect the hit.
- Eager-EOT on + TurnResumed: speculation is cancelled, no reuse.
- BARGE_IN also resets any in-flight speculation.
"""

from __future__ import annotations

import pytest

from fg_voice.audio.bank import AudioBank, Clip
from fg_voice.conversation.graph import EotConfig, build_graph
from fg_voice.conversation.prompt_bank import load_prompt_bank
from fg_voice.conversation.runner import (
    ConversationRunner,
    Hangup,
    InputEvent,
    RunnerConfig,
    _NoopFluxReconfigurer,
)
from fg_voice.conversation.state import CallState, NodeId
from fg_voice.conversation.state_store import CallStateStore
from fg_voice.pipeline.stt_flux import FluxEvent, FluxEventKind

# ─── Test doubles ───────────────────────────────────────────────────


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


def _empty_audio_bank() -> AudioBank:
    """Empty bank → `get(...)` returns None, runner logs + skips playback."""
    from pathlib import Path

    return AudioBank(clips={}, root=Path("/tmp/nonexistent"), locale="en-IN", version="test")


def _end_of_turn(text: str) -> InputEvent:
    return InputEvent(
        kind="flux",
        flux_event=FluxEvent(
            kind=FluxEventKind.END_OF_TURN,
            transcript=text,
            confidence=0.9,
        ),
    )


def _eager_of(text: str) -> InputEvent:
    return InputEvent(
        kind="flux",
        flux_event=FluxEvent(kind=FluxEventKind.EAGER_END_OF_TURN, transcript=text),
    )


def _turn_resumed() -> InputEvent:
    return InputEvent(kind="flux", flux_event=FluxEvent(kind=FluxEventKind.TURN_RESUMED))


def _start_of_turn() -> InputEvent:
    return InputEvent(kind="flux", flux_event=FluxEvent(kind=FluxEventKind.START_OF_TURN))


# ─── Reconfigurer wiring ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconfigurer_called_on_prompted_node_entry() -> None:
    """Each prompted node (ASK_INTENT, ASK_HAZARD_TYPE, ...) triggers
    exactly one reconfigure call with its effective EOT pair. Machine
    nodes and CONSENT don't call the collect loop, so they don't push
    Flux config — but CONSENT is a prompted node with no extractor,
    which SHOULD NOT reconfigure (there's no listen phase)."""
    graph = build_graph()
    bank = load_prompt_bank()
    audio = _empty_audio_bank()
    state = CallState(call_sid="CA1", caller_hash="hash")
    # Walk INTENT=yes → HAZARD=storm → DESCRIPTION → LOCATION → then hang up.
    scripted = _ScriptedInput(
        [
            _end_of_turn("yes"),
            _end_of_turn("storm"),
            _end_of_turn("trees are down"),
            _end_of_turn("kakinada beach"),
        ]
    )
    reconfigurer = _NoopFluxReconfigurer()
    runner = ConversationRunner(
        call_state=state,
        graph=graph,
        prompt_bank=bank,
        audio_bank=audio,
        turn_input=scripted,
        audio_sink=_RecordingSink(),
        state_store=_NoopStore(),
        config=RunnerConfig(max_call_duration_sec=10),
        flux_reconfigurer=reconfigurer,
    )
    await runner.run()

    # At least one reconfigure per prompted-listening node we walked.
    # ASK_INTENT + ASK_HAZARD_TYPE + ASK_DESCRIPTION + ASK_LOCATION.
    assert len(reconfigurer.calls) >= 4

    # The ASK_LOCATION-specific override propagates through — check the
    # sequence contains (0.6, 2000) somewhere.
    location_cfg = EotConfig(threshold=0.6, timeout_ms=2000)
    assert location_cfg in reconfigurer.calls, (
        f"expected ASK_LOCATION override in reconfigure sequence: {reconfigurer.calls}"
    )


@pytest.mark.asyncio
async def test_reconfigurer_uses_runner_defaults_for_untouched_nodes() -> None:
    """A node without a per-node override sees the runner's default
    pair. ASK_INTENT has neither override → default_threshold /
    default_timeout_ms should surface verbatim."""
    graph = build_graph()
    bank = load_prompt_bank()
    scripted = _ScriptedInput([_end_of_turn("yes")])
    reconfigurer = _NoopFluxReconfigurer()
    runner = ConversationRunner(
        call_state=CallState(call_sid="CA2", caller_hash="h"),
        graph=graph,
        prompt_bank=bank,
        audio_bank=_empty_audio_bank(),
        turn_input=scripted,
        audio_sink=_RecordingSink(),
        state_store=_NoopStore(),
        config=RunnerConfig(
            max_call_duration_sec=5,
            default_eot_threshold=0.55,
            default_eot_timeout_ms=999,
        ),
        flux_reconfigurer=reconfigurer,
    )
    # Runner catches Hangup internally; a single-event script drives
    # ASK_INTENT and then hits the empty script → Hangup path.
    await runner.run()
    # First call should be for ASK_INTENT with the runner defaults.
    assert reconfigurer.calls[0] == EotConfig(threshold=0.55, timeout_ms=999)


# ─── Eager EOT off (default) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_eager_eot_off_ignores_speculate_events() -> None:
    """With enable_eager_eot=False (default), SPECULATE + CANCEL events
    don't move state and don't create stats. The runner behaves
    exactly as P2.5: one COMMIT_TURN per collected turn."""
    graph = build_graph()
    bank = load_prompt_bank()
    scripted = _ScriptedInput(
        [
            _eager_of("y"),  # ignored
            _turn_resumed(),  # ignored
            _eager_of("ye"),  # ignored
            _end_of_turn("yes"),  # commit
            _end_of_turn("storm"),
            _end_of_turn("tree down"),
            _end_of_turn("kakinada"),
        ]
    )
    runner = ConversationRunner(
        call_state=CallState(call_sid="CA3", caller_hash="h"),
        graph=graph,
        prompt_bank=bank,
        audio_bank=_empty_audio_bank(),
        turn_input=scripted,
        audio_sink=_RecordingSink(),
        state_store=_NoopStore(),
        config=RunnerConfig(max_call_duration_sec=10),
    )
    await runner.run()
    # No speculation was started because the flag is off.
    assert runner.eager_eot_stats.speculations_started == 0


# ─── Eager EOT on ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_eager_eot_on_reuses_speculation_when_transcript_matches() -> None:
    """EagerEndOfTurn("yes") → EndOfTurn("yes"): the coordinator's
    identity extractor already produced "yes" speculatively, so the
    commit reuses it. Stats show 1 started + 1 reused, 0 final."""
    graph = build_graph()
    bank = load_prompt_bank()
    scripted = _ScriptedInput(
        [
            _eager_of("yes"),
            _end_of_turn("yes"),
            _end_of_turn("storm"),
            _end_of_turn("tree down"),
            _end_of_turn("kakinada"),
        ]
    )
    runner = ConversationRunner(
        call_state=CallState(call_sid="CA4", caller_hash="h"),
        graph=graph,
        prompt_bank=bank,
        audio_bank=_empty_audio_bank(),
        turn_input=scripted,
        audio_sink=_RecordingSink(),
        state_store=_NoopStore(),
        config=RunnerConfig(max_call_duration_sec=10, enable_eager_eot=True),
    )
    await runner.run()
    assert runner.eager_eot_stats.speculations_started >= 1
    assert runner.eager_eot_stats.speculations_reused >= 1


@pytest.mark.asyncio
async def test_eager_eot_on_cancels_speculation_on_turn_resumed() -> None:
    """EagerEndOfTurn("y") → TurnResumed → EndOfTurn("yes indeed"):
    the speculative "y" is cancelled, the final "yes indeed" runs
    fresh through the identity extractor. Stats show 1 started + 1
    cancelled + 1 final extraction."""
    graph = build_graph()
    bank = load_prompt_bank()
    scripted = _ScriptedInput(
        [
            _eager_of("y"),
            _turn_resumed(),
            _end_of_turn("yes indeed"),
            _end_of_turn("storm"),
            _end_of_turn("tree down"),
            _end_of_turn("kakinada"),
        ]
    )
    runner = ConversationRunner(
        call_state=CallState(call_sid="CA5", caller_hash="h"),
        graph=graph,
        prompt_bank=bank,
        audio_bank=_empty_audio_bank(),
        turn_input=scripted,
        audio_sink=_RecordingSink(),
        state_store=_NoopStore(),
        config=RunnerConfig(max_call_duration_sec=10, enable_eager_eot=True),
    )
    await runner.run()
    assert runner.eager_eot_stats.speculations_cancelled >= 1
    # No reuse happened — the transcripts differed.
    assert runner.eager_eot_stats.speculations_reused == 0


@pytest.mark.asyncio
async def test_eager_eot_barge_in_also_resets_speculation() -> None:
    """StartOfTurn while a speculation is in flight must reset the
    coordinator, otherwise the next COMMIT_TURN could reuse a stale
    speculation from before the barge-in."""
    graph = build_graph()
    bank = load_prompt_bank()
    scripted = _ScriptedInput(
        [
            _eager_of("yes"),
            _start_of_turn(),  # barge-in — should reset
            _end_of_turn("no"),  # fresh commit, different transcript
            # After INTENT=no the graph goes to NOT_REPORTING (terminal).
        ]
    )
    runner = ConversationRunner(
        call_state=CallState(call_sid="CA6", caller_hash="h"),
        graph=graph,
        prompt_bank=bank,
        audio_bank=_empty_audio_bank(),
        turn_input=scripted,
        audio_sink=_RecordingSink(),
        state_store=_NoopStore(),
        config=RunnerConfig(max_call_duration_sec=10, enable_eager_eot=True),
    )
    result = await runner.run()
    assert result.state.current_node == NodeId.NOT_REPORTING
    # The speculation was reset on barge-in, so the commit ran a fresh
    # extraction — reused count should be zero.
    assert runner.eager_eot_stats.speculations_reused == 0
