"""CallState round-trip via the in-memory store — proves the JSON
serialisation contract without needing Redis."""

from __future__ import annotations

import pytest

from fg_voice.conversation.state import CallState, NodeId, Slot, SlotValue
from fg_voice.conversation.state_store import InMemoryCallStateStore


@pytest.mark.asyncio
async def test_save_and_load_roundtrips_slots_and_flags():
    store = InMemoryCallStateStore()
    state = CallState(call_sid="CA123", caller_hash="deadbeef")
    state.current_node = NodeId.ASK_HAZARD_TYPE
    state.set_slot(Slot.INTENT, SlotValue(value="yes", confidence=0.9, source="asr"))
    state.set_slot(
        Slot.WATER_DEPTH_CM,
        SlotValue(value=90, confidence=1.0, source="dtmf"),
    )
    state.add_flag("life_safety")

    await store.save(state)
    loaded = await store.load("CA123")
    assert loaded is not None
    assert loaded.call_sid == "CA123"
    assert loaded.current_node is NodeId.ASK_HAZARD_TYPE
    assert loaded.slots[Slot.INTENT].value == "yes"
    assert loaded.slots[Slot.WATER_DEPTH_CM].value == 90
    assert "life_safety" in loaded.flags


@pytest.mark.asyncio
async def test_load_missing_returns_none():
    store = InMemoryCallStateStore()
    assert await store.load("does-not-exist") is None


@pytest.mark.asyncio
async def test_delete_removes_state():
    store = InMemoryCallStateStore()
    state = CallState(call_sid="CA789", caller_hash="xx")
    await store.save(state)
    await store.delete("CA789")
    assert await store.load("CA789") is None


@pytest.mark.asyncio
async def test_report_id_preserved_across_save_load():
    store = InMemoryCallStateStore()
    state = CallState(call_sid="CA555", caller_hash="xx")
    original = state.report_id
    await store.save(state)
    loaded = await store.load("CA555")
    assert loaded is not None
    assert loaded.report_id == original
