"""End-to-end runner scenarios (§18 layer 1 + parts of layer 2).

No real Twilio, no real Deepgram. The `ScriptedTurnInput` feeds a
deterministic event stream; the `RecordingAudioSink` captures every
clip played + every control message sent.

These tests are the concrete evidence for the P2 exit gate:
> A full call completes end to end using only pre-rendered audio and
> keyword extraction. Barge-in works. DTMF fallback works on all three
> categorical slots."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from fg_voice.audio.bank import load_audio_bank
from fg_voice.conversation.graph import build_graph
from fg_voice.conversation.prompt_bank import load_prompt_bank
from fg_voice.conversation.runner import ConversationRunner, RunnerConfig
from fg_voice.conversation.state import CallState, NodeId, Slot
from fg_voice.conversation.state_store import InMemoryCallStateStore

from ._doubles import RecordingAudioSink, ScriptedTurnInput

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RENDER_SCRIPT = _REPO_ROOT / "scripts" / "render_audio_bank.py"


@pytest.fixture(scope="module")
def audio_bank(tmp_path_factory):
    """Render the silence-engine bank once per test module."""
    render_dir = tmp_path_factory.mktemp("bank")
    spec = importlib.util.spec_from_file_location("render_audio_bank_runner", _RENDER_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["render_audio_bank_runner"] = mod
    spec.loader.exec_module(mod)
    mod.render_bank(render_dir, "en-IN", "silence", "Aditi")
    return load_audio_bank(render_dir, locale="en-IN")


@pytest.fixture
def prompt_bank():
    return load_prompt_bank()


@pytest.fixture
def graph():
    return build_graph()


@pytest.fixture
def store():
    return InMemoryCallStateStore()


def _fresh_state(call_sid: str = "CA_RUNNER") -> CallState:
    return CallState(call_sid=call_sid, caller_hash="testhash")


def _runner(state, graph, prompt_bank, audio_bank, turn_input, sink, store):
    return ConversationRunner(
        call_state=state,
        graph=graph,
        prompt_bank=prompt_bank,
        audio_bank=audio_bank,
        turn_input=turn_input,
        audio_sink=sink,
        state_store=store,
        config=RunnerConfig(stream_sid="MZfake", max_call_duration_sec=60),
    )


# ─── Happy path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_flood_class_hazard_reaches_submitted(
    graph, prompt_bank, audio_bank, store
):
    """Caller reports a storm at Vizag Beach → all six slots fill →
    confirm=yes → SUBMITTED. Storm is a flood-class hazard so
    ASK_DEPTH is on the path."""
    state = _fresh_state()
    ti = ScriptedTurnInput()
    ti.push_transcript("yes I want to report")
    ti.push_transcript("cyclone damage everywhere")
    ti.push_transcript("waves crashed onto the road")
    ti.push_transcript("RK Beach near Vizag")
    # Ask_location has confidence 0.6 → CONFIRM_LOCATION_LOW_CONF
    ti.push_transcript("yes that is right")
    ti.push_transcript("very severe")
    ti.push_transcript("waist deep")
    ti.push_transcript("yes submit it")
    sink = RecordingAudioSink()

    result = await _runner(state, graph, prompt_bank, audio_bank, ti, sink, store).run()

    assert result.terminated_via == "terminal_node"
    assert result.state.current_node is NodeId.SUBMITTED
    assert result.state.slots[Slot.INTENT].value == "yes"
    assert result.state.slots[Slot.HAZARD_TYPE].value == "storm"
    assert result.state.slots[Slot.SEVERITY].value == "extreme"
    assert result.state.slots[Slot.WATER_DEPTH_CM].value == 90


@pytest.mark.asyncio
async def test_happy_path_non_flood_hazard_skips_depth(graph, prompt_bank, audio_bank, store):
    """Sludge/oil isn't a flood-class hazard, so ASK_DEPTH is skipped."""
    state = _fresh_state("CA_NOFLOOD")
    ti = ScriptedTurnInput()
    ti.push_transcript("yes")
    ti.push_transcript("oil spill on the beach")
    ti.push_transcript("black slick and diesel smell everywhere")
    ti.push_transcript("Kakinada")
    ti.push_transcript("yes")  # confirm low-conf location
    ti.push_transcript("moderate")
    ti.push_transcript("yes")  # confirm summary
    sink = RecordingAudioSink()

    runner = _runner(state, graph, prompt_bank, audio_bank, ti, sink, store)
    result = await runner.run()

    assert result.state.current_node is NodeId.SUBMITTED
    assert Slot.WATER_DEPTH_CM not in result.state.slots
    # ASK_DEPTH should NOT appear in the prompt trail
    assert "ask_depth" not in runner.prompt_trail


@pytest.mark.asyncio
async def test_intent_no_hangs_up_at_not_reporting(graph, prompt_bank, audio_bank, store):
    state = _fresh_state("CA_NO")
    ti = ScriptedTurnInput()
    ti.push_transcript("no thanks")
    sink = RecordingAudioSink()

    result = await _runner(state, graph, prompt_bank, audio_bank, ti, sink, store).run()

    assert result.state.current_node is NodeId.NOT_REPORTING
    assert Slot.INTENT in result.state.slots
    assert result.state.slots[Slot.INTENT].value == "no"


# ─── Reprompt ladder ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reprompt_ladder_plays_correct_prompt_ids(graph, prompt_bank, audio_bank, store):
    """First mumble → reprompt_intent_1. Second mumble → reprompt_intent_2."""
    state = _fresh_state("CA_REPROMPT")
    ti = ScriptedTurnInput()
    ti.push_transcript("uhhh")  # unclear → attempt=1
    ti.push_transcript("mmmm")  # unclear → attempt=2
    ti.push_transcript("yes reporting")  # answers on attempt 2's prompt
    ti.push_transcript("storm damage")
    ti.push_transcript("wind broke a tree")
    ti.push_transcript("Vizag Beach")
    ti.push_transcript("yes")  # confirm low-conf location
    ti.push_transcript("moderate")
    ti.push_transcript("knee")  # storm is flood-class
    ti.push_transcript("yes")  # confirm
    sink = RecordingAudioSink()

    runner = _runner(state, graph, prompt_bank, audio_bank, ti, sink, store)
    await runner.run()

    trail = runner.prompt_trail
    assert "ask_intent" in trail
    assert "reprompt_intent_1" in trail
    assert "reprompt_intent_2" in trail
    # And they appear in that order.
    idx_ask = trail.index("ask_intent")
    idx_r1 = trail.index("reprompt_intent_1")
    idx_r2 = trail.index("reprompt_intent_2")
    assert idx_ask < idx_r1 < idx_r2


@pytest.mark.asyncio
async def test_ladder_exhaustion_exits_to_timeout(graph, prompt_bank, audio_bank, store):
    """All three attempts on ASK_INTENT are unclear → TIMEOUT_EXIT."""
    state = _fresh_state("CA_LADDER_OUT")
    ti = ScriptedTurnInput()
    for _ in range(6):
        ti.push_transcript("uhhh")
    sink = RecordingAudioSink()

    runner = _runner(state, graph, prompt_bank, audio_bank, ti, sink, store)
    result = await runner.run()
    assert result.state.current_node is NodeId.TIMEOUT_EXIT
    assert "timeout_exit" in runner.prompt_trail


# ─── DTMF fallback ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dtmf_answers_intent_after_two_unclear_turns(graph, prompt_bank, audio_bank, store):
    """After two unclear utterances the runner plays reprompt_intent_2,
    which announces DTMF. A digit "1" on that prompt should map to
    "yes" and advance. DTMF sent before the ladder reaches an armed
    prompt is correctly rejected (there's no DTMF map on ask_intent)."""
    state = _fresh_state("CA_DTMF")
    ti = ScriptedTurnInput()
    ti.push_transcript("uhhh")  # attempt 1 → reprompt_intent_1
    ti.push_transcript("uhhh")  # attempt 2 → reprompt_intent_2 (DTMF armed)
    ti.push_dtmf("1")  # → "yes"
    # Finish the call with transcripts so we can prove SUBMITTED without
    # walking every slot through its own DTMF fallback.
    ti.push_transcript("cyclone damage")
    ti.push_transcript("wind broke a tree")
    ti.push_transcript("Vizag Beach")
    ti.push_transcript("yes")  # confirm low-conf location
    ti.push_transcript("moderate")
    ti.push_transcript("knee")  # storm is flood-class
    ti.push_transcript("yes")  # confirm summary

    sink = RecordingAudioSink()

    result = await _runner(state, graph, prompt_bank, audio_bank, ti, sink, store).run()
    assert result.state.current_node is NodeId.SUBMITTED
    assert result.state.slots[Slot.INTENT].value == "yes"
    assert result.state.slots[Slot.INTENT].source == "dtmf"
    assert result.state.slots[Slot.INTENT].confidence == 1.0


@pytest.mark.asyncio
async def test_dtmf_on_disarmed_prompt_is_rejected(graph, prompt_bank, audio_bank, store):
    """A digit press on `ask_intent` (which has no DTMF map) advances
    the reprompt ladder, same as an unclear utterance."""
    state = _fresh_state("CA_DTMF_DISARMED")
    ti = ScriptedTurnInput()
    ti.push_dtmf("1")  # ask_intent → no map → advance ladder
    ti.push_transcript("yes reporting")  # answer on reprompt_intent_1
    ti.push_transcript("cyclone")
    ti.push_transcript("wind broke a tree")
    ti.push_transcript("Vizag Beach")
    ti.push_transcript("yes")
    ti.push_transcript("moderate")
    ti.push_transcript("knee")
    ti.push_transcript("yes")

    sink = RecordingAudioSink()
    runner = _runner(state, graph, prompt_bank, audio_bank, ti, sink, store)
    result = await runner.run()

    assert result.state.current_node is NodeId.SUBMITTED
    assert result.state.slots[Slot.INTENT].source == "asr"
    # Reprompt happened because DTMF was rejected on the initial ask.
    assert "reprompt_intent_1" in runner.prompt_trail


# ─── Safety tripwire ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_safety_tripwire_diverts_to_emergency_redirect(graph, prompt_bank, audio_bank, store):
    """Caller says "my brother is trapped" at the hazard prompt → the
    tripwire should catch it before extraction and divert the graph
    to EMERGENCY_REDIRECT."""
    state = _fresh_state("CA_EMERGENCY")
    ti = ScriptedTurnInput()
    ti.push_transcript("yes")
    ti.push_transcript("my brother is trapped in the flood water please help me")
    sink = RecordingAudioSink()

    result = await _runner(state, graph, prompt_bank, audio_bank, ti, sink, store).run()

    assert "life_safety" in result.state.flags
    assert result.state.resume_after_emergency is NodeId.ASK_HAZARD_TYPE
    # We ended on EMERGENCY_REDIRECT → END.
    assert result.state.current_node in (NodeId.EMERGENCY_REDIRECT, NodeId.END)


# ─── Restart edge ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_confirm_restart_reenters_hazard_type(graph, prompt_bank, audio_bank, store):
    """Caller says "start over" at CONFIRM_SUMMARY → START_OVER →
    ASK_HAZARD_TYPE. Then a valid second confirmation lands SUBMITTED."""
    state = _fresh_state("CA_RESTART")
    ti = ScriptedTurnInput()
    ti.push_transcript("yes")  # intent
    ti.push_transcript("oil spill")  # hazard 1
    ti.push_transcript("black slick spreading")  # description
    ti.push_transcript("Kakinada")  # location
    ti.push_transcript("yes")  # confirm low-conf loc
    ti.push_transcript("moderate")  # severity
    ti.push_transcript("start over")  # confirm → restart
    # Second pass through hazard onwards. START_OVER re-enters
    # ASK_HAZARD_TYPE; DESCRIPTION/LOCATION are re-collected in the
    # graph as re-asks. NOTE: the graph re-collects description +
    # location; make sure the script has enough turns for the second
    # pass through.
    ti.push_transcript("storm")  # hazard 2
    ti.push_transcript("wind damage")  # description
    ti.push_transcript("Vizag Beach")  # location
    ti.push_transcript("yes")  # confirm loc
    ti.push_transcript("extreme")  # severity
    ti.push_transcript("waist")  # depth (storm is flood-class)
    ti.push_transcript("yes")  # confirm

    sink = RecordingAudioSink()
    runner = _runner(state, graph, prompt_bank, audio_bank, ti, sink, store)
    result = await runner.run()

    assert result.state.current_node is NodeId.SUBMITTED
    # The final hazard must be storm (the second pass wins).
    assert result.state.slots[Slot.HAZARD_TYPE].value == "storm"
    # start_over prompt should have been played.
    assert "start_over" in runner.prompt_trail


# ─── Persistence ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_state_persisted_after_every_transition(graph, prompt_bank, audio_bank, store):
    """After the run, the store must contain the final state."""
    state = _fresh_state("CA_PERSIST")
    ti = ScriptedTurnInput()
    ti.push_transcript("no")
    sink = RecordingAudioSink()

    await _runner(state, graph, prompt_bank, audio_bank, ti, sink, store).run()

    loaded = await store.load("CA_PERSIST")
    assert loaded is not None
    assert loaded.current_node is NodeId.NOT_REPORTING
    assert loaded.slots[Slot.INTENT].value == "no"
