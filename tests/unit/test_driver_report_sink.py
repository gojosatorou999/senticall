"""Driver ↔ ReportSink wiring — SUBMIT writes exactly one report and
the short_ref returned by the sink is what the terminal prompt renders."""

from __future__ import annotations

import pytest

from fg_voice.conversation.driver import CallerInput, TurnDriver
from fg_voice.conversation.graph import build_graph
from fg_voice.conversation.prompt_bank import load_prompt_bank
from fg_voice.conversation.report_sink import InMemoryReportSink
from fg_voice.conversation.state import NodeId, Slot
from fg_voice.conversation.state_store import InMemoryCallStateStore


@pytest.fixture
def sink():
    return InMemoryReportSink()


@pytest.fixture
def driver(sink):
    return TurnDriver(
        graph=build_graph(),
        prompt_bank=load_prompt_bank(),
        state_store=InMemoryCallStateStore(),
        report_sink=sink,
    )


@pytest.mark.asyncio
async def test_submit_writes_exactly_one_report(driver, sink):
    await driver.start_call(call_sid="CA_SUB", caller_hash="h")
    for utterance in [
        "yes",
        "storm damage",
        "wind broke a tree",
        "Vizag Beach",
        "yes",  # confirm low-conf location
        "extreme",
        "waist",
        "yes",  # confirm summary
    ]:
        _state, step = await driver.step(
            call_sid="CA_SUB", caller_input=CallerInput.speech(utterance)
        )
        if step.action == "hangup":
            break

    assert len(sink.reports) == 1
    stored_state, submitted = sink.reports[0]
    assert stored_state.slots[Slot.HAZARD_TYPE].value == "storm"
    assert stored_state.current_node in (NodeId.SUBMIT, NodeId.SUBMITTED)
    assert submitted.short_ref.startswith("FG-")


@pytest.mark.asyncio
async def test_submitted_prompt_renders_sink_short_ref(driver, sink):
    await driver.start_call(call_sid="CA_REF", caller_hash="h")
    for utterance in [
        "yes",
        "oil spill",
        "black slick spreading",
        "Kakinada",
        "yes",  # confirm low-conf loc
        "moderate",
        "yes",  # confirm summary (non-flood → no depth)
    ]:
        _state, step = await driver.step(
            call_sid="CA_REF", caller_input=CallerInput.speech(utterance)
        )
        if step.action == "hangup":
            break

    assert step.action == "hangup"
    assert "submitted" in step.prompts_to_play
    subs = step.prompt_variables["submitted"]
    # The short_ref in the terminal prompt MUST equal what the sink
    # actually stored, not a client-side placeholder.
    assert len(sink.reports) == 1
    _stored, stored_submitted = sink.reports[0]
    assert subs["short_ref"] == stored_submitted.short_ref


@pytest.mark.asyncio
async def test_not_reporting_writes_no_report(driver, sink):
    await driver.start_call(call_sid="CA_NR", caller_hash="h")
    _state, step = await driver.step(call_sid="CA_NR", caller_input=CallerInput.speech("no thanks"))
    assert step.action == "hangup"
    # NOT_REPORTING never enters SUBMIT, so no report row.
    assert sink.reports == []


@pytest.mark.asyncio
async def test_safety_tripwire_writes_no_report_but_still_renders_terminal(driver, sink):
    """Emergency-redirect ends the call without submission; short_ref
    isn't needed here (the prompt has no {short_ref})."""
    await driver.start_call(call_sid="CA_TW", caller_hash="h")
    await driver.step(call_sid="CA_TW", caller_input=CallerInput.speech("yes"))
    _state, step = await driver.step(
        call_sid="CA_TW",
        caller_input=CallerInput.speech("my brother is trapped in the water"),
    )
    assert step.action == "hangup"
    assert "emergency_redirect" in step.prompts_to_play
    assert sink.reports == []


@pytest.mark.asyncio
async def test_short_ref_persisted_on_call_state(driver, sink):
    """After SUBMIT, `state.short_ref` is populated so a subsequent
    step-retry that lands on SUBMITTED can render the same reference."""
    await driver.start_call(call_sid="CA_PERSIST", caller_hash="h")
    for utterance in [
        "yes",
        "oil spill",
        "black slick",
        "Kakinada",
        "yes",
        "moderate",
        "yes",
    ]:
        state, step = await driver.step(
            call_sid="CA_PERSIST", caller_input=CallerInput.speech(utterance)
        )
        if step.action == "hangup":
            break
    assert state.short_ref is not None
    assert state.short_ref.startswith("FG-")
    # And the sink's short_ref matches what landed on the state.
    _stored, submitted = sink.reports[0]
    assert state.short_ref == submitted.short_ref
