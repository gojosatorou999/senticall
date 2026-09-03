"""Step-based TurnDriver — walks a full call one HTTP-turn at a time.

Mirrors the runner integration tests (test_runner.py) but exercises the
sync-per-request API the Gather routes depend on."""

from __future__ import annotations

import pytest

from fg_voice.conversation.driver import CallerInput, CallStateMissing, TurnDriver
from fg_voice.conversation.graph import build_graph
from fg_voice.conversation.prompt_bank import load_prompt_bank
from fg_voice.conversation.state import NodeId, Slot
from fg_voice.conversation.state_store import InMemoryCallStateStore


@pytest.fixture
def driver():
    return TurnDriver(
        graph=build_graph(),
        prompt_bank=load_prompt_bank(),
        state_store=InMemoryCallStateStore(),
    )


# ─── start_call ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_call_plays_consent_then_gathers_intent(driver):
    state, step = await driver.start_call(call_sid="CA1", caller_hash="hash1")
    assert step.action == "gather"
    # Consent notice + ask_intent both play in the first response.
    assert "consent_notice" in step.prompts_to_play
    assert step.prompts_to_play[-1] == "ask_intent"
    assert state.current_node is NodeId.ASK_INTENT


@pytest.mark.asyncio
async def test_state_persisted_after_start(driver):
    _state, _step = await driver.start_call(call_sid="CA_persist", caller_hash="h")
    loaded = await driver.state_store.load("CA_persist")
    assert loaded is not None
    assert loaded.current_node is NodeId.ASK_INTENT


# ─── step: yes/no on intent ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_step_no_at_intent_hangs_up(driver):
    await driver.start_call(call_sid="CA_no", caller_hash="h")
    _state, step = await driver.step(call_sid="CA_no", caller_input=CallerInput.speech("no thanks"))
    assert step.action == "hangup"
    assert step.hangup_after is True
    assert step.prompts_to_play[-1] == "not_reporting"


@pytest.mark.asyncio
async def test_step_yes_advances_to_hazard(driver):
    await driver.start_call(call_sid="CA_yes", caller_hash="h")
    state, step = await driver.step(
        call_sid="CA_yes", caller_input=CallerInput.speech("yes reporting")
    )
    assert step.action == "gather"
    assert step.prompts_to_play[-1] == "ask_hazard_type"
    assert state.current_node is NodeId.ASK_HAZARD_TYPE
    assert state.slots[Slot.INTENT].value == "yes"


# ─── DTMF ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dtmf_disarmed_on_initial_ask_advances_ladder(driver):
    await driver.start_call(call_sid="CA_dtmf1", caller_hash="h")
    _state, step = await driver.step(call_sid="CA_dtmf1", caller_input=CallerInput.dtmf(digit="1"))
    # DTMF on ask_intent (no map) → attempt+1 → reprompt_intent_1.
    assert step.prompts_to_play[-1] == "reprompt_intent_1"
    assert step.action == "gather"


@pytest.mark.asyncio
async def test_dtmf_armed_on_reprompt_maps_to_yes(driver):
    """Two unclear turns land us on reprompt_intent_2 (dtmf armed).
    Digit "1" on that prompt fills intent with source=dtmf."""
    await driver.start_call(call_sid="CA_dtmf2", caller_hash="h")
    await driver.step(call_sid="CA_dtmf2", caller_input=CallerInput.speech("uhh"))
    await driver.step(call_sid="CA_dtmf2", caller_input=CallerInput.speech("uhh"))
    state, step = await driver.step(call_sid="CA_dtmf2", caller_input=CallerInput.dtmf(digit="1"))
    assert state.slots[Slot.INTENT].value == "yes"
    assert state.slots[Slot.INTENT].source == "dtmf"
    assert state.slots[Slot.INTENT].confidence == 1.0
    assert step.action == "gather"
    assert step.prompts_to_play[-1] == "ask_hazard_type"


# ─── Reprompt ladder ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ladder_exhausts_to_timeout_exit(driver):
    await driver.start_call(call_sid="CA_ladder", caller_hash="h")
    for _ in range(5):
        _state, step = await driver.step(
            call_sid="CA_ladder", caller_input=CallerInput.speech("uhhh")
        )
        if step.action == "hangup":
            break
    assert step.action == "hangup"
    assert step.prompts_to_play[-1] == "timeout_exit"


@pytest.mark.asyncio
async def test_timeout_input_advances_ladder(driver):
    await driver.start_call(call_sid="CA_to", caller_hash="h")
    _state, step = await driver.step(call_sid="CA_to", caller_input=CallerInput.timeout())
    assert step.prompts_to_play[-1] == "reprompt_intent_1"


# ─── Safety tripwire ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_safety_tripwire_diverts_to_emergency(driver):
    await driver.start_call(call_sid="CA_safety", caller_hash="h")
    await driver.step(call_sid="CA_safety", caller_input=CallerInput.speech("yes"))
    state, step = await driver.step(
        call_sid="CA_safety",
        caller_input=CallerInput.speech("my brother is trapped, please help me"),
    )
    assert "life_safety" in state.flags
    assert state.resume_after_emergency is NodeId.ASK_HAZARD_TYPE
    # After the tripwire the runner plays the emergency prompt then END.
    assert "emergency_redirect" in step.prompts_to_play
    assert step.action == "hangup"


# ─── Missing state ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_step_without_prior_start_raises(driver):
    with pytest.raises(CallStateMissing):
        await driver.step(call_sid="never_started", caller_input=CallerInput.speech("hi"))


# ─── Full happy path (many turns) ────────────────────────────────────


@pytest.mark.asyncio
async def test_full_happy_path_flood_class(driver):
    await driver.start_call(call_sid="CA_happy", caller_hash="h")
    for utterance in [
        "yes",
        "storm damage",
        "wind broke a tree",
        "Vizag Beach",
        "yes",  # confirm low-conf location
        "extreme",
        "waist",  # storm is flood-class
        "yes",  # confirm summary
    ]:
        state, step = await driver.step(
            call_sid="CA_happy", caller_input=CallerInput.speech(utterance)
        )
        if step.action == "hangup":
            break
    assert step.action == "hangup"
    assert state.current_node is NodeId.SUBMITTED
    assert state.slots[Slot.HAZARD_TYPE].value == "storm"
    assert state.slots[Slot.WATER_DEPTH_CM].value == 90
    # Terminal prompt has a short_ref variable — verify it's populated.
    assert "submitted" in step.prompts_to_play
    subs = step.prompt_variables.get("submitted", {})
    assert subs.get("short_ref", "").startswith("FG-")
