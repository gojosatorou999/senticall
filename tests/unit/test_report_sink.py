"""InMemoryReportSink round-trip + short_ref format contract."""

from __future__ import annotations

import pytest

from fg_voice.conversation.report_sink import (
    SHORT_REF_ALPHABET,
    SHORT_REF_BODY_LEN,
    SHORT_REF_PREFIX,
    InMemoryReportSink,
    NoopReportSink,
    format_short_ref,
)
from fg_voice.conversation.state import CallState


def test_short_ref_uses_unambiguous_alphabet_and_prefix():
    ref = format_short_ref(42)
    assert ref.startswith(SHORT_REF_PREFIX)
    body = ref[len(SHORT_REF_PREFIX) :]
    assert len(body) == SHORT_REF_BODY_LEN
    # No 0 / O / 1 / I in the body — that's the whole point of the alphabet.
    assert not (set(body) & set("01OI"))
    assert set(body) <= set(SHORT_REF_ALPHABET)


def test_short_ref_deterministic():
    """Same input → same output. Callers can quote it back."""
    assert format_short_ref(1234567) == format_short_ref(1234567)


@pytest.mark.asyncio
async def test_in_memory_sink_records_and_returns_short_ref():
    sink = InMemoryReportSink()
    state = CallState(call_sid="CA1", caller_hash="h")
    submitted = await sink.write(state)
    assert submitted.report_id == state.report_id
    assert submitted.short_ref.startswith("FG-")
    assert sink.latest() is not None
    _stored_state, stored = sink.latest()  # type: ignore[misc]
    assert stored.short_ref == submitted.short_ref


@pytest.mark.asyncio
async def test_in_memory_sink_deep_copies_state():
    """A later mutation of the CallState should not rewrite the sink's
    recorded snapshot."""
    sink = InMemoryReportSink()
    state = CallState(call_sid="CA_snap", caller_hash="h")
    state.add_flag("foo")
    await sink.write(state)
    state.add_flag("bar")

    latest = sink.latest()
    assert latest is not None
    stored_state, _rep = latest
    assert "foo" in stored_state.flags
    assert "bar" not in stored_state.flags


@pytest.mark.asyncio
async def test_in_memory_sink_returns_distinct_short_refs_per_call():
    sink = InMemoryReportSink()
    s1 = await sink.write(CallState(call_sid="A", caller_hash="h"))
    s2 = await sink.write(CallState(call_sid="B", caller_hash="h"))
    assert s1.short_ref != s2.short_ref


@pytest.mark.asyncio
async def test_noop_sink_returns_ref_without_recording():
    sink = NoopReportSink()
    state = CallState(call_sid="CA_noop", caller_hash="h")
    submitted = await sink.write(state)
    assert submitted.short_ref.startswith("FG-")
