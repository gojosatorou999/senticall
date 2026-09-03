"""SqlReportSink — writes Report + Outbox in one transaction (§2.3).

Uses an in-memory async SQLite so the tests don't need Postgres."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from fg_voice.conversation.sql_report_sink import SqlReportSink
from fg_voice.conversation.state import CallState, Slot, SlotValue
from fg_voice.persistence.db import (
    Base,
    get_session_maker,
    override_engine,
    reset_engine,
)
from fg_voice.persistence.models import OutboxEntry, Report
from fg_voice.persistence.outbox import OutboxEventType


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", pool_pre_ping=False)
    override_engine(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()
    reset_engine()


def _state_with_slots(call_sid: str = "CA_SQL") -> CallState:
    state = CallState(call_sid=call_sid, caller_hash="testhash")
    state.set_slot(Slot.HAZARD_TYPE, SlotValue(value="storm", confidence=0.9, source="asr"))
    state.set_slot(Slot.SEVERITY, SlotValue(value="extreme", confidence=0.9, source="asr"))
    state.set_slot(
        Slot.WATER_DEPTH_CM,
        SlotValue(value=90, confidence=1.0, source="dtmf"),
    )
    state.set_slot(
        Slot.DESCRIPTION,
        SlotValue(value="waves crashed onto the road", confidence=0.6, source="asr"),
    )
    state.set_slot(
        Slot.LOCATION, SlotValue(value="RK Beach near Vizag", confidence=0.6, source="asr")
    )
    state.add_flag("life_safety")
    return state


@pytest.mark.asyncio
async def test_write_creates_report_and_outbox_atomically(engine):
    sink = SqlReportSink()
    state = _state_with_slots()

    submitted = await sink.write(state)
    assert submitted.short_ref.startswith("FG-")

    async with get_session_maker()() as session:
        # Report row was written with slot values projected onto columns.
        row = await session.get(Report, state.report_id)
        assert row is not None
        assert row.short_ref == submitted.short_ref
        assert row.source == "voice"
        assert row.call_sid == state.call_sid
        assert row.hazard_type == "storm"
        assert row.severity == "extreme"
        assert row.water_depth_cm == 90
        assert row.description == "waves crashed onto the road"
        assert row.location_raw == "RK Beach near Vizag"
        assert row.status == "pending_enrichment"
        assert row.flags == {"life_safety": True}

        # Outbox row landed with the report reference + event type.
        outbox_rows = (
            await session.scalars(
                select(OutboxEntry).where(OutboxEntry.report_id == state.report_id)
            )
        ).all()
        assert len(outbox_rows) == 1
        oe = outbox_rows[0]
        assert oe.event_type == OutboxEventType.REPORT_SUBMITTED
        assert oe.dispatched_at is None
        assert oe.retry_count == 0
        assert oe.payload["short_ref"] == submitted.short_ref
        assert oe.payload["hazard_type"] == "storm"
        assert "life_safety" in oe.payload["flags"]


@pytest.mark.asyncio
async def test_write_is_idempotent_on_report_id(engine):
    """Retried Twilio POST that lands twice on SUBMIT reuses the same
    Report row and returns the original short_ref — the caller hears
    the same reference either way."""
    sink = SqlReportSink()
    state = _state_with_slots("CA_RETRY")

    first = await sink.write(state)
    second = await sink.write(state)

    assert first.short_ref == second.short_ref
    assert first.report_id == second.report_id

    async with get_session_maker()() as session:
        report_count = (
            await session.scalars(select(Report).where(Report.report_id == state.report_id))
        ).all()
        assert len(report_count) == 1
        outbox_count = (
            await session.scalars(
                select(OutboxEntry).where(OutboxEntry.report_id == state.report_id)
            )
        ).all()
        # Only one outbox row — idempotency prevents double dispatch too.
        assert len(outbox_count) == 1


@pytest.mark.asyncio
async def test_write_missing_slots_leaves_columns_null(engine):
    """Terminal-only calls (safety tripwire, intent=no with life_safety
    flag) may have unfilled slots. Those must persist as NULL, not as
    fabricated defaults, so P6 knows they were never collected."""
    sink = SqlReportSink()
    state = CallState(call_sid="CA_partial", caller_hash="h")
    state.set_slot(Slot.INTENT, SlotValue(value="yes", confidence=1.0, source="dtmf"))
    submitted = await sink.write(state)

    async with get_session_maker()() as session:
        row = await session.get(Report, state.report_id)
        assert row is not None
        assert row.hazard_type is None
        assert row.severity is None
        assert row.water_depth_cm is None
        assert row.location_raw is None
        assert row.short_ref == submitted.short_ref
