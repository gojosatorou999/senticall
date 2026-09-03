"""End-to-end integration for the P6 enrichment DAG.

Submits a real Report via `SqlReportSink`, runs the `EnrichmentFlow`
against it, and asserts:

- The row's enrichment columns are populated.
- A `report.enriched` outbox row landed with the right payload.
- Re-running is idempotent (same row, updated timestamp, no dup row).
- Injected impls (FakeGeocoder, FakeDedupe) get used, not the defaults.
- Missing report row → PermanentEnrichmentError all the way through.

Also exercises the dispatcher via the relay (`OutboxRelay.drain_once`)
so we know the wire path — outbox row → relay → dispatcher → flow →
row updated + new outbox event — works end-to-end.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from fg_voice.conversation.sql_report_sink import SqlReportSink
from fg_voice.conversation.state import CallState, Slot, SlotValue
from fg_voice.enrichment import EnrichmentDispatcher, EnrichmentFlow
from fg_voice.enrichment.errors import PermanentEnrichmentError
from fg_voice.persistence.db import (
    Base,
    get_session_maker,
    override_engine,
    reset_engine,
)
from fg_voice.persistence.models import OutboxEntry, Report
from fg_voice.persistence.outbox import OutboxEventType
from fg_voice.persistence.relay import OutboxRelay


@pytest.fixture
async def _db():
    """Fresh SQLite per test; disposed cleanly."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", pool_pre_ping=False)
    override_engine(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()
        reset_engine()


async def _submit_report(**slot_overrides) -> CallState:
    """Submit one call through SqlReportSink and return the CallState
    (which now has short_ref stamped on it)."""
    slots = {
        Slot.HAZARD_TYPE: "storm",
        Slot.SEVERITY: "moderate",
        Slot.LOCATION: "RK Beach",
        Slot.DESCRIPTION: "waves onto road, water knee deep",
        Slot.WATER_DEPTH_CM: 40,
    }
    slots.update(slot_overrides)
    state = CallState(call_sid=f"CA_{uuid4().hex[:8]}", caller_hash="h")
    for slot, value in slots.items():
        state.set_slot(slot, SlotValue(value=value, confidence=0.9, source="asr"))
    await SqlReportSink().write(state)
    return state


@pytest.mark.asyncio
async def test_flow_updates_row_with_noop_boundaries(_db):
    """With all defaults (No-Op extractor / geocoder / dedupe), the
    flow still populates score fields + timestamp + emits an outbox
    event. Location_resolved / dedupe_group_id stay NULL because the
    defaults return nothing — that's the additive contract."""
    state = await _submit_report()

    result = await EnrichmentFlow().run(state.report_id)
    assert result.result.confidence_score is not None
    assert result.result.priority_score == 60

    async with get_session_maker()() as session:
        row = await session.get(Report, state.report_id)
        assert row is not None
        assert row.status == "enriched"
        assert row.enriched_at is not None
        assert row.confidence_score is not None
        assert row.priority_score == 60
        # No-Op boundaries → these stay NULL.
        assert row.location_resolved is None
        assert row.dedupe_group_id is None


@pytest.mark.asyncio
async def test_flow_emits_report_enriched_outbox_event(_db):
    state = await _submit_report()
    await EnrichmentFlow().run(state.report_id)

    async with get_session_maker()() as session:
        entries = list(
            (
                await session.scalars(
                    select(OutboxEntry).where(
                        OutboxEntry.event_type == OutboxEventType.REPORT_ENRICHED
                    )
                )
            ).all()
        )
    assert len(entries) == 1
    payload = entries[0].payload
    assert payload["report_id"] == str(state.report_id)
    assert payload["confidence_score"] is not None
    assert payload["priority_score"] == 60


@pytest.mark.asyncio
async def test_flow_is_idempotent_and_reruns_cleanly(_db):
    """Re-running the flow doesn't duplicate the row or corrupt state.
    A second `report.enriched` outbox row IS appended — the outbox
    event is the audit trail of enrichment runs, not a dedupe key."""
    state = await _submit_report()
    r1 = await EnrichmentFlow().run(state.report_id)
    r2 = await EnrichmentFlow().run(state.report_id)

    assert r1.result.confidence_score == r2.result.confidence_score
    assert r1.result.priority_score == r2.result.priority_score

    async with get_session_maker()() as session:
        rows = list(
            (await session.scalars(select(Report).where(Report.report_id == state.report_id))).all()
        )
        assert len(rows) == 1  # no dup rows
        enriched_events = list(
            (
                await session.scalars(
                    select(OutboxEntry).where(
                        OutboxEntry.event_type == OutboxEventType.REPORT_ENRICHED
                    )
                )
            ).all()
        )
    assert len(enriched_events) == 2  # one per run


@pytest.mark.asyncio
async def test_flow_uses_injected_boundaries(_db):
    """Real impls swap in via constructor. The flow calls them and
    stashes the results — not the defaults."""

    class FakeGeocoder:
        async def resolve(self, raw):
            return f"canonical:{raw}"

    class FakeDedupe:
        async def group_for(self, ctx, session):
            return "dedupe-grp-99"

    state = await _submit_report()
    flow = EnrichmentFlow(geocoder=FakeGeocoder(), dedupe_strategy=FakeDedupe())
    await flow.run(state.report_id)

    async with get_session_maker()() as session:
        row = await session.get(Report, state.report_id)
        assert row is not None
        assert row.location_resolved == "canonical:RK Beach"
        assert row.dedupe_group_id == "dedupe-grp-99"


@pytest.mark.asyncio
async def test_flow_missing_row_raises_permanent(_db):
    """Assemble step raises PermanentEnrichmentError — the dispatcher
    wraps it in DispatchError; here we test the raw flow surface."""
    with pytest.raises(PermanentEnrichmentError):
        await EnrichmentFlow().run(uuid4())


# ─── dispatcher-through-relay wire path ──────────────────────────────


@pytest.mark.asyncio
async def test_relay_drives_enrichment_end_to_end(_db):
    """One submitted call → drain the outbox with the enrichment
    dispatcher → row is enriched, both submitted + enriched events
    end up dispatched."""
    state = await _submit_report()

    relay = OutboxRelay(dispatcher=EnrichmentDispatcher(flow=EnrichmentFlow()))
    # Drain 1: consumes report.submitted → runs enrichment (which in
    # turn appends report.enriched).
    processed1 = await relay.drain_once()
    assert processed1 == 1
    # Drain 2: consumes the newly-appended report.enriched (dispatcher
    # ignores it — non-submitted event type — so it's marked
    # dispatched with no work done).
    processed2 = await relay.drain_once()
    assert processed2 == 1

    async with get_session_maker()() as session:
        row = await session.get(Report, state.report_id)
        assert row is not None
        assert row.status == "enriched"
        undispatched = list(
            (
                await session.scalars(
                    select(OutboxEntry).where(OutboxEntry.dispatched_at.is_(None))
                )
            ).all()
        )
    assert undispatched == []  # no orphan work
