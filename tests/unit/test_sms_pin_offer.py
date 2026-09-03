"""SmsPinOfferService — decision + send behaviour.

Pins the trigger + suppression rules from spec §7.3 (ladder attempt 4)
and §11 (pin-drop) so a subtle edit to the graph enum values or the
CallState schema can't silently stop the SMS from firing.

The `SmsSender` here is `RecordingSmsSender` — no network, capture
`.sent` per call and assert against it.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from fg_voice.conversation.state import CallState, NodeId, Slot, SlotValue
from fg_voice.conversation.state_store import InMemoryCallStateStore
from fg_voice.enrichment.sms_pin_offer import SmsPinOfferService
from fg_voice.telephony.twilio_sms import RecordingSmsSender


def _make_state(
    *,
    current_node: NodeId,
    short_ref: str | None = "FG-ABCD",
    location_conf: float | None = 0.95,
    life_safety: bool = False,
) -> CallState:
    state = CallState(
        call_sid="CA_TEST",
        caller_hash="hash",
        current_node=current_node,
        short_ref=short_ref,
    )
    if location_conf is not None:
        state.slots[Slot.LOCATION] = SlotValue(
            value="near bheemili beach",
            confidence=location_conf,
            source="asr",
        )
    if life_safety:
        state.flags.add("life_safety")
    return state


@pytest.fixture
def sender() -> RecordingSmsSender:
    return RecordingSmsSender()


@pytest.fixture
def store() -> InMemoryCallStateStore:
    return InMemoryCallStateStore()


@pytest.fixture
def service(sender: RecordingSmsSender, store: InMemoryCallStateStore) -> SmsPinOfferService:
    return SmsPinOfferService(
        sender=sender,
        state_store=store,
        from_number="+15005550006",
        web_base_url="https://voice.sentinel.in/",
        location_confidence_threshold=0.85,
    )


# ─── Trigger paths ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_timeout_exit_triggers_sms(
    service: SmsPinOfferService,
    sender: RecordingSmsSender,
    store: InMemoryCallStateStore,
) -> None:
    state = _make_state(current_node=NodeId.TIMEOUT_EXIT)
    await store.save(state)
    sent = await service.maybe_send(call_sid=state.call_sid, to_number="+919000000000")
    assert sent is True
    assert len(sender.sent) == 1
    msg = sender.sent[0]
    assert msg["to"] == "+919000000000"
    assert msg["from"] == "+15005550006"
    assert "FG-ABCD" in msg["body"]
    assert "https://voice.sentinel.in/pin/FG-ABCD" in msg["body"]


@pytest.mark.asyncio
async def test_missing_location_slot_triggers_sms(
    service: SmsPinOfferService, sender: RecordingSmsSender, store: InMemoryCallStateStore
) -> None:
    state = _make_state(current_node=NodeId.SUBMITTED, location_conf=None)
    await store.save(state)
    sent = await service.maybe_send(call_sid=state.call_sid, to_number="+919000000000")
    assert sent is True
    assert len(sender.sent) == 1


@pytest.mark.asyncio
async def test_low_confidence_location_triggers_sms(
    service: SmsPinOfferService, sender: RecordingSmsSender, store: InMemoryCallStateStore
) -> None:
    state = _make_state(current_node=NodeId.SUBMITTED, location_conf=0.60)
    await store.save(state)
    sent = await service.maybe_send(call_sid=state.call_sid, to_number="+919000000000")
    assert sent is True


# ─── Suppression paths ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_life_safety_flag_suppresses_sms(
    service: SmsPinOfferService, sender: RecordingSmsSender, store: InMemoryCallStateStore
) -> None:
    state = _make_state(
        current_node=NodeId.EMERGENCY_REDIRECT, location_conf=0.30, life_safety=True
    )
    await store.save(state)
    sent = await service.maybe_send(call_sid=state.call_sid, to_number="+919000000000")
    assert sent is False
    assert sender.sent == []


@pytest.mark.asyncio
async def test_no_short_ref_suppresses_sms(
    service: SmsPinOfferService, sender: RecordingSmsSender, store: InMemoryCallStateStore
) -> None:
    state = _make_state(current_node=NodeId.TIMEOUT_EXIT, short_ref=None)
    await store.save(state)
    sent = await service.maybe_send(call_sid=state.call_sid, to_number="+919000000000")
    assert sent is False
    assert sender.sent == []


@pytest.mark.asyncio
async def test_high_confidence_location_and_normal_termination_no_sms(
    service: SmsPinOfferService, sender: RecordingSmsSender, store: InMemoryCallStateStore
) -> None:
    """Happy-path call — no reason to SMS. This is the majority case."""
    state = _make_state(current_node=NodeId.SUBMITTED, location_conf=0.95)
    await store.save(state)
    sent = await service.maybe_send(call_sid=state.call_sid, to_number="+919000000000")
    assert sent is False
    assert sender.sent == []


@pytest.mark.asyncio
async def test_missing_to_number_short_circuits(
    service: SmsPinOfferService, sender: RecordingSmsSender, store: InMemoryCallStateStore
) -> None:
    state = _make_state(current_node=NodeId.TIMEOUT_EXIT)
    await store.save(state)
    sent = await service.maybe_send(call_sid=state.call_sid, to_number="")
    assert sent is False
    assert sender.sent == []


@pytest.mark.asyncio
async def test_no_call_state_short_circuits(
    service: SmsPinOfferService, sender: RecordingSmsSender
) -> None:
    """Twilio can retry the /voice/status callback — after the CallState
    TTL expires, there's nothing to load. Silently skip."""
    sent = await service.maybe_send(call_sid="CA_unknown", to_number="+919000000000")
    assert sent is False
    assert sender.sent == []


# ─── URL formatting ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_url_trailing_slash_tolerated(
    sender: RecordingSmsSender, store: InMemoryCallStateStore
) -> None:
    service = SmsPinOfferService(
        sender=sender,
        state_store=store,
        from_number="+15005550006",
        web_base_url="https://voice.sentinel.in//",  # excess slashes
    )
    state = _make_state(current_node=NodeId.TIMEOUT_EXIT, short_ref="FG-9999")
    await store.save(state)
    await service.maybe_send(call_sid=state.call_sid, to_number="+919000000000")
    assert "https://voice.sentinel.in/pin/FG-9999" in sender.sent[0]["body"]


# ─── Sender failure ─────────────────────────────────────────────────


class _RaisingSender:
    async def send(self, *, to: str, from_: str, body: str) -> str:
        from fg_voice.telephony.twilio_sms import TwilioSmsError

        raise TwilioSmsError("simulated timeout")


@pytest.mark.asyncio
async def test_sender_failure_is_swallowed(store: InMemoryCallStateStore) -> None:
    service = SmsPinOfferService(
        sender=_RaisingSender(),
        state_store=store,
        from_number="+15005550006",
        web_base_url="https://voice.sentinel.in",
    )
    state = _make_state(current_node=NodeId.TIMEOUT_EXIT)
    await store.save(state)
    # Must not raise — Twilio would retry the status webhook and re-fire
    # the whole post-call chain if we let this bubble.
    sent = await service.maybe_send(call_sid=state.call_sid, to_number="+919000000000")
    assert sent is False


# ─── report_id sanity (regression) ──────────────────────────────────


@pytest.mark.asyncio
async def test_unique_short_ref_per_call(
    service: SmsPinOfferService, sender: RecordingSmsSender, store: InMemoryCallStateStore
) -> None:
    """Two calls with different short_refs get their own URLs."""
    for ref in ["FG-AAAA", "FG-BBBB"]:
        state = _make_state(current_node=NodeId.TIMEOUT_EXIT, short_ref=ref)
        state.call_sid = f"CA_{ref}"
        state.report_id = uuid4()
        await store.save(state)
        await service.maybe_send(call_sid=state.call_sid, to_number="+919000000000")
    assert len(sender.sent) == 2
    urls = [msg["body"] for msg in sender.sent]
    assert any("FG-AAAA" in u for u in urls)
    assert any("FG-BBBB" in u for u in urls)
