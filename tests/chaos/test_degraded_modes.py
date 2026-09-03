"""Chaos suite — every degraded mode from spec §2.6 must produce the
documented behaviour (not a crash, not silent data loss).

§2.6 table:
    STT down       → DTMF IVR mode (all categorical slots via DTMF)
    TTS down       → serve from pre-rendered audio bank (~85% of turns)
    Extraction LLM → rule-based keyword extractor + DTMF confirmation
    RDS (outbox)   → caller hears success; outbox relay retries on recovery
    RAG index down → store raw location_text, geo_confidence=0.6, confirm path

Note: the ConversationRunner handles conversation flow only.
Report persistence (outbox/RDS) is tested in tests/unit/test_outbox_relay.py
and tests/unit/test_sql_report_sink.py. The runner-level degraded modes
are STT, TTS, LLM extraction, RAG resolution, and worker crash.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOUBLES_DIR = _REPO_ROOT / "tests" / "integration"
if str(_DOUBLES_DIR) not in sys.path:
    sys.path.insert(0, str(_DOUBLES_DIR))

from _doubles import RecordingAudioSink, ScriptedTurnInput  # type: ignore[import-not-found]

from fg_voice.audio.bank import load_audio_bank
from fg_voice.conversation.graph import build_graph
from fg_voice.conversation.prompt_bank import load_prompt_bank
from fg_voice.conversation.runner import ConversationRunner, RunnerConfig
from fg_voice.conversation.state import CallState, NodeId
from fg_voice.conversation.state_store import InMemoryCallStateStore

_RENDER_SCRIPT = _REPO_ROOT / "scripts" / "render_audio_bank.py"


@pytest.fixture(scope="module")
def audio_bank(tmp_path_factory):
    render_dir = tmp_path_factory.mktemp("chaos_bank")
    spec = importlib.util.spec_from_file_location("_render_bank_chaos", _RENDER_SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_render_bank_chaos"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    mod.render_bank(render_dir, "en-IN", "silence", "Aditi")
    return load_audio_bank(render_dir, locale="en-IN")


def _runner_for(inp: ScriptedTurnInput, audio_bank, *, sink=None) -> ConversationRunner:
    graph = build_graph()
    prompt_bank = load_prompt_bank()
    store = InMemoryCallStateStore()
    sink = sink or RecordingAudioSink()
    state = CallState(call_sid="CA_chaos", caller_hash="chaos_hash")
    return ConversationRunner(
        call_state=state,
        graph=graph,
        prompt_bank=prompt_bank,
        audio_bank=audio_bank,
        turn_input=inp,
        audio_sink=sink,
        state_store=store,
        config=RunnerConfig(stream_sid="MZ_chaos", max_call_duration_sec=60),
    )


def _sludge_script_with_location_confirm() -> ScriptedTurnInput:
    """Standard non-flood script.
    Location always triggers CONFIRM_LOCATION_LOW_CONF (keyword conf=0.6)."""
    inp = ScriptedTurnInput()
    inp.push_transcript("yes")
    inp.push_transcript("oil spill on the beach")
    inp.push_transcript("Large oil spill near the jetty")
    inp.push_transcript("Kakinada harbour")
    inp.push_transcript("yes")  # confirm_location_low_conf
    inp.push_transcript("moderate")
    inp.push_transcript("yes")  # confirm_summary
    return inp


# ── §2.6 — STT down → DTMF IVR mode ─────────────────────────────────
# When categorical slots can't be filled by ASR, the reprompt ladder
# arms DTMF on the 2nd reprompt. The caller can complete entirely via
# keypad presses.


@pytest.mark.asyncio
async def test_stt_down_categorical_slots_completeable_via_dtmf(audio_bank) -> None:
    """All categorical slots can be filled via DTMF when ASR is unclear."""
    inp = ScriptedTurnInput()

    # INTENT: unclear x 2 → DTMF armed → "1" = yes
    inp.push_transcript("uhhh")  # unclear → reprompt 1
    inp.push_transcript("mmm")  # unclear → reprompt 2 (DTMF armed)
    inp.push_dtmf("1")  # yes

    # HAZARD_TYPE: unclear x 2 → "2" = sludge_oil (non-flood, skips ASK_DEPTH)
    inp.push_transcript("uhhh")
    inp.push_transcript("mmm")
    inp.push_dtmf("2")  # sludge_oil

    # DESCRIPTION: free-text, accepted on 3rd attempt
    inp.push_transcript("uhhh")
    inp.push_transcript("mmm")
    inp.push_transcript("oil at the jetty")  # accepted on attempt 3 regardless

    # LOCATION: accepted immediately (free-text)
    inp.push_transcript("Kakinada")
    inp.push_transcript("yes")  # confirm_location_low_conf

    # SEVERITY: unclear x 2 → "3" = extreme
    inp.push_transcript("uhhh")
    inp.push_transcript("mmm")
    inp.push_dtmf("3")  # extreme

    # CONFIRM_SUMMARY: unclear x 1 → reprompt_confirm_1 (DTMF armed) → "1" = yes
    inp.push_transcript("uhhh")  # attempt 0 → 1 (reprompt_confirm_1 has DTMF map)
    inp.push_dtmf("1")  # yes (on reprompt_confirm_1)

    runner = _runner_for(inp, audio_bank)
    result = await runner.run()

    assert result.state.current_node == NodeId.SUBMITTED, (
        f"STT-down DTMF path did not reach SUBMITTED, got {result.state.current_node}"
    )
    assert result.state.slots.get("hazard_type") is not None


# ── §2.6 — TTS down → pre-rendered audio bank ────────────────────────
# The audio bank serves ~85% of utterances. The runner completes a
# call using only bank clips (static prompts) without live TTS.


@pytest.mark.asyncio
async def test_tts_down_bank_clips_serve_static_prompts(audio_bank) -> None:
    """Runner completes when only pre-rendered bank clips are available."""
    inp = _sludge_script_with_location_confirm()
    sink = RecordingAudioSink()
    runner = _runner_for(inp, audio_bank, sink=sink)
    result = await runner.run()

    assert result.state.current_node == NodeId.SUBMITTED
    # Bank clips were played for the static prompts (consent, ask_intent, etc.)
    assert len(sink.played_prompts) > 0, "No prompts played — audio bank not loaded"
    # Static prompts must include 'consent_notice', 'ask_intent', etc.
    assert any("consent" in p for p in sink.played_prompts), (
        f"consent_notice not played; played: {sink.played_prompts}"
    )


# ── §2.6 — Extraction LLM down → keyword rules ───────────────────────
# The HybridExtractor falls back to keyword rules when the LLM backend
# is unavailable. Keyword rules are always active as the default path.


@pytest.mark.asyncio
async def test_keyword_rules_complete_call_without_llm(audio_bank) -> None:
    """Keyword-rule extraction (no LLM) completes the happy path."""
    inp = _sludge_script_with_location_confirm()
    runner = _runner_for(inp, audio_bank)
    result = await runner.run()

    # Runner uses keyword rules by default (no LLM configured in tests)
    assert result.state.current_node == NodeId.SUBMITTED
    assert result.state.slots.get("hazard_type") is not None
    assert result.state.slots["hazard_type"].source == "asr"


# ── §2.6 — Worker crash → state persisted per transition ─────────────
# The CallState is written to the store after every node transition.
# A worker crash mid-call leaves valid state for the post-call DAG.


@pytest.mark.asyncio
async def test_state_persisted_after_every_transition(audio_bank) -> None:
    """State is saved after each node transition — crash recovery is possible."""
    saves: list[str] = []

    class TrackingStore(InMemoryCallStateStore):
        async def save(self, state: CallState) -> None:
            saves.append(state.current_node)
            await super().save(state)

    inp = _sludge_script_with_location_confirm()
    sink = RecordingAudioSink()
    state = CallState(call_sid="CA_track", caller_hash="track_hash")
    graph = build_graph()
    prompt_bank = load_prompt_bank()
    store = TrackingStore()

    runner = ConversationRunner(
        call_state=state,
        graph=graph,
        prompt_bank=prompt_bank,
        audio_bank=audio_bank,
        turn_input=inp,
        audio_sink=sink,
        state_store=store,
        config=RunnerConfig(stream_sid="MZ_track", max_call_duration_sec=60),
    )
    result = await runner.run()

    # Multiple saves: at least CONSENT, ASK_INTENT, ..., SUBMITTED
    assert len(saves) >= 5, (
        f"Expected ≥5 state saves (one per transition), got {len(saves)}: {saves}"
    )
    # The final save must match the terminal node
    assert saves[-1] == result.state.current_node == NodeId.SUBMITTED


# ── §2.6 — RAG index unavailable → confirm path (confidence=0.6) ─────
# With keyword extraction, location confidence=0.6 (hardcoded in
# extract_free_text). This always triggers CONFIRM_LOCATION_LOW_CONF.
# The confirmed location is stored and the call proceeds to ASK_SEVERITY.


@pytest.mark.asyncio
async def test_rag_unavailable_confirm_location_path(audio_bank) -> None:
    """Without real RAG, keyword extractor gives conf=0.6 → CONFIRM_LOCATION_LOW_CONF.
    Using sludge_oil (non-flood-class) to avoid ASK_DEPTH branch."""
    inp = ScriptedTurnInput()
    inp.push_transcript("yes")
    inp.push_transcript("sludge oil slick")  # non-flood-class → skips ASK_DEPTH
    inp.push_transcript("Oil visible near the shore")
    inp.push_transcript("some coastal village near the sea")
    inp.push_transcript("yes")  # CONFIRM_LOCATION_LOW_CONF → confirms it
    inp.push_transcript("moderate")
    inp.push_transcript("yes")

    runner = _runner_for(inp, audio_bank)
    result = await runner.run()

    assert result.state.current_node == NodeId.SUBMITTED
    loc = result.state.slots.get("location")
    assert loc is not None, "location slot must be set"
    # Location was confirmed (confidence updated to 0.75 from confirmation)
    assert loc.value == "some coastal village near the sea"


# ── §2.6 — Immediate hangup → runner exits cleanly ───────────────────


@pytest.mark.asyncio
async def test_caller_hangup_before_consent_exits_cleanly(audio_bank) -> None:
    """Caller hangs up before saying anything — runner catches Hangup."""
    from fg_voice.conversation.runner import Hangup

    class InstantHangupInput:
        async def next_event(self, timeout_ms: int):  # type: ignore[no-untyped-def]
            raise Hangup

    sink = RecordingAudioSink()
    state = CallState(call_sid="CA_hangup", caller_hash="hang_hash")
    store = InMemoryCallStateStore()
    graph = build_graph()
    prompt_bank = load_prompt_bank()

    runner = ConversationRunner(
        call_state=state,
        graph=graph,
        prompt_bank=prompt_bank,
        audio_bank=audio_bank,
        turn_input=InstantHangupInput(),  # type: ignore[arg-type]
        audio_sink=sink,
        state_store=store,
        config=RunnerConfig(stream_sid="MZ_hang", max_call_duration_sec=60),
    )

    result = await runner.run()
    assert result.terminated_via == "hangup"


# ── §2.6 — Emergency redirect → call resumes ─────────────────────────


@pytest.mark.asyncio
async def test_emergency_tripwire_fires_and_call_ends_at_END(audio_bank) -> None:
    """Safety tripwire fires, emergency_redirect plays, call ends at END.

    The graph routes EMERGENCY_REDIRECT → END (the resume path is stored
    in CallState.resume_after_emergency for the post-call DAG, but the
    v1 call itself terminates so the caller can immediately dial 112)."""
    inp = ScriptedTurnInput()
    inp.push_transcript("yes my father is trapped in water")  # → EMERGENCY_REDIRECT → END

    sink = RecordingAudioSink()
    runner = _runner_for(inp, audio_bank, sink=sink)
    result = await runner.run()

    # Call terminates at END after emergency redirect
    assert result.state.current_node in (NodeId.EMERGENCY_REDIRECT, NodeId.END)
    # life_safety flag must be set
    assert "life_safety" in result.state.flags, (
        f"life_safety flag not set; flags: {result.state.flags}"
    )
    # emergency_redirect prompt must have been played
    assert any("emergency" in p for p in sink.played_prompts), (
        f"emergency_redirect not played; played: {sink.played_prompts}"
    )
    # resume node saved for post-call DAG
    assert result.state.resume_after_emergency is not None


# ── §2.6 — No-input timeout ladder → TIMEOUT_EXIT ────────────────────


@pytest.mark.asyncio
async def test_silent_caller_reaches_timeout_exit(audio_bank) -> None:
    """No-input on every prompt exhausts the ladder and reaches TIMEOUT_EXIT."""
    inp = ScriptedTurnInput()
    # 4 no-inputs to exhaust the 3-rung ladder:
    # attempt 0→1→2→3; at attempt 4>3 → TIMEOUT_EXIT
    inp.push_no_input()  # attempt 0: ask_intent
    inp.push_no_input()  # attempt 1: reprompt_1
    inp.push_no_input()  # attempt 2: reprompt_2 (DTMF armed)
    inp.push_no_input()  # attempt 3: ask_intent fallback → advance_ladder → 4>3 → TIMEOUT_EXIT

    runner = _runner_for(inp, audio_bank)
    result = await runner.run()

    assert result.state.current_node == NodeId.TIMEOUT_EXIT, (
        f"Expected TIMEOUT_EXIT, got {result.state.current_node}"
    )


# ── §2.6 — Max call duration → graceful exit ─────────────────────────


@pytest.mark.asyncio
async def test_max_call_duration_exits_cleanly(audio_bank) -> None:
    """Call running over max_call_duration_sec exits via TIMEOUT_EXIT."""
    import asyncio

    class SlowTurnInput:
        """Simulates a very slow caller — times out the call."""

        async def next_event(self, timeout_ms: int):  # type: ignore[no-untyped-def]
            await asyncio.sleep(10)  # longer than max_call_duration_sec=1
            return None

    sink = RecordingAudioSink()
    state = CallState(call_sid="CA_max_dur", caller_hash="dur_hash")
    store = InMemoryCallStateStore()
    graph = build_graph()
    prompt_bank = load_prompt_bank()

    runner = ConversationRunner(
        call_state=state,
        graph=graph,
        prompt_bank=prompt_bank,
        audio_bank=audio_bank,
        turn_input=SlowTurnInput(),  # type: ignore[arg-type]
        audio_sink=sink,
        state_store=store,
        config=RunnerConfig(stream_sid="MZ_dur", max_call_duration_sec=1),
    )

    result = await runner.run()
    # Must terminate gracefully — not an exception
    assert result.terminated_via == "max_call_duration"
