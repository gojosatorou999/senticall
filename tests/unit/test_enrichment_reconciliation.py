"""deep_extract confidence gate + persist slot-reconciliation whitelist.

Unit tests focused on the two guards that keep the LLM boundary from
corrupting the report row:

- confidence gate in `deep_extract` — proposals below threshold get
  logged + dropped BEFORE they land on the accumulator.
- whitelist in `persist` — only `_REVISABLE_SLOTS` (hazard_type,
  severity, water_depth_cm) can be overwritten by revisions.
  Description stays raw+clean untouchable; location goes through
  geocode.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from fg_voice.enrichment.models import (
    EnrichmentContext,
    ReportSnapshot,
)
from fg_voice.enrichment.tasks.extract import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    RevisedSlots,
    deep_extract,
)
from fg_voice.enrichment.tasks.persist import persist
from fg_voice.persistence.db import (
    Base,
    get_session_maker,
    override_engine,
    reset_engine,
)
from fg_voice.persistence.models import OutboxEntry, Report
from fg_voice.persistence.outbox import OutboxEventType


def _ctx(**overrides) -> EnrichmentContext:
    base = {
        "report_id": uuid4(),
        "short_ref": "FG-RECON",
        "source": "voice",
        "call_sid": "CA_R",
        "caller_hash": "h",
        "hazard_type": "storm",
        "severity": "moderate",
        "water_depth_cm": 40,
        "description": "waves onto road",
        "description_clean": "waves onto road",
        "location_raw": "RK Beach",
        "flags": {},
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return EnrichmentContext(snapshot=ReportSnapshot(**base))


# ─── Confidence gate ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deep_extract_below_threshold_drops_revisions():
    class LowConfExtractor:
        async def extract(self, description):
            return RevisedSlots(values={"severity": "extreme"}, confidence=0.5)

    ctx = _ctx()
    await deep_extract(ctx, extractor=LowConfExtractor())
    assert ctx.result.revised_slots == {}
    assert any("dropped" in n for n in ctx.result.notes)


@pytest.mark.asyncio
async def test_deep_extract_at_threshold_stashes_revisions():
    """Exactly at the threshold is accepted (>= comparison)."""

    class BoundaryExtractor:
        async def extract(self, description):
            return RevisedSlots(
                values={"severity": "extreme"}, confidence=DEFAULT_CONFIDENCE_THRESHOLD
            )

    ctx = _ctx()
    await deep_extract(ctx, extractor=BoundaryExtractor())
    assert ctx.result.revised_slots == {"severity": "extreme"}


@pytest.mark.asyncio
async def test_deep_extract_custom_threshold_respected():
    """Callers can pass a stricter threshold — same extractor, same
    result, different acceptance decision."""

    class MidConfExtractor:
        async def extract(self, description):
            return RevisedSlots(values={"severity": "extreme"}, confidence=0.8)

    ctx_strict = _ctx()
    await deep_extract(ctx_strict, extractor=MidConfExtractor(), confidence_threshold=0.95)
    assert ctx_strict.result.revised_slots == {}

    ctx_lax = _ctx()
    await deep_extract(ctx_lax, extractor=MidConfExtractor(), confidence_threshold=0.5)
    assert ctx_lax.result.revised_slots == {"severity": "extreme"}


# ─── Whitelist in persist ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_applies_whitelisted_slot_revisions():
    """A revision to a whitelisted slot (severity) lands on the row.
    Verifies persist actually mutates the column, not just the
    outbox payload."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", pool_pre_ping=False)
    override_engine(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        report_id = uuid4()
        async with get_session_maker()() as session, session.begin():
            session.add(
                Report(
                    report_id=report_id,
                    short_ref="FG-WL1",
                    source="voice",
                    call_sid="CA_WL",
                    caller_hash="h",
                    hazard_type="storm",
                    severity="moderate",
                    water_depth_cm=40,
                    description="raw",
                    description_clean="raw",
                    location_raw="RK",
                    flags={},
                )
            )
        ctx = _ctx(report_id=report_id, severity="moderate")
        ctx.result.revised_slots = {"severity": "extreme", "hazard_type": "flood"}

        async with get_session_maker()() as session, session.begin():
            await persist(ctx, session)

        async with get_session_maker()() as session:
            row = await session.get(Report, report_id)
            assert row is not None
            assert row.severity == "extreme"
            assert row.hazard_type == "flood"
    finally:
        await eng.dispose()
        reset_engine()


@pytest.mark.asyncio
async def test_persist_drops_non_whitelisted_slot_revisions():
    """Revising `description` or `location_raw` (or a made-up column)
    is silently dropped with a note — those slots are owned by other
    tasks or invariants."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", pool_pre_ping=False)
    override_engine(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        report_id = uuid4()
        original_desc = "raw caller words"
        original_loc = "RK Beach"
        async with get_session_maker()() as session, session.begin():
            session.add(
                Report(
                    report_id=report_id,
                    short_ref="FG-WL2",
                    source="voice",
                    call_sid="CA_WL2",
                    caller_hash="h",
                    hazard_type="storm",
                    severity="moderate",
                    description=original_desc,
                    description_clean=original_desc,
                    location_raw=original_loc,
                    flags={},
                )
            )
        ctx = _ctx(report_id=report_id)
        ctx.result.revised_slots = {
            "description": "LLM tried to overwrite this",
            "location_raw": "LLM tried this too",
            "made_up_column": 42,
        }

        async with get_session_maker()() as session, session.begin():
            await persist(ctx, session)

        async with get_session_maker()() as session:
            row = await session.get(Report, report_id)
            assert row is not None
            # Untouched — even though the revision proposed a change.
            assert row.description == original_desc
            assert row.location_raw == original_loc

        # Notes should record every drop.
        dropped = [n for n in ctx.result.notes if n.startswith("persist dropped")]
        assert len(dropped) == 3
        assert any("description" in n for n in dropped)
        assert any("location_raw" in n for n in dropped)
        assert any("made_up_column" in n for n in dropped)
    finally:
        await eng.dispose()
        reset_engine()


# ─── Enriched-payload shape ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_emits_full_snapshot_payload():
    """The report.enriched outbox row carries the full report shape
    (submit-payload columns + enrichment fields). Downstream
    dispatchers (SSE, alerts) rely on this."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", pool_pre_ping=False)
    override_engine(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        report_id = uuid4()
        async with get_session_maker()() as session, session.begin():
            session.add(
                Report(
                    report_id=report_id,
                    short_ref="FG-SNAP",
                    source="voice",
                    call_sid="CA_SNAP",
                    caller_hash="h",
                    hazard_type="storm",
                    severity="extreme",
                    water_depth_cm=120,
                    description="caller words",
                    description_clean="caller words",
                    location_raw="RK",
                    flags={"life_safety": True},
                )
            )
        ctx = _ctx(
            report_id=report_id,
            severity="extreme",
            water_depth_cm=120,
            flags={"life_safety": True},
        )
        ctx.result.confidence_score = 85
        ctx.result.priority_score = 100
        ctx.result.location_resolved = "canonical:RK Beach"
        ctx.result.dedupe_group_id = "grp_1"
        ctx.result.notes = ["deep_extract ran", "geocode resolved"]

        async with get_session_maker()() as session, session.begin():
            await persist(ctx, session)

        async with get_session_maker()() as session:
            entry = await session.scalar(
                OutboxEntry.__table__.select().where(
                    OutboxEntry.event_type == OutboxEventType.REPORT_ENRICHED
                )
            )
        assert entry is not None
        # `session.scalar` on a raw select returns the first column;
        # re-run through the ORM path so the payload is accessible.
        async with get_session_maker()() as session:
            from sqlalchemy import select

            row = await session.scalar(
                select(OutboxEntry).where(OutboxEntry.event_type == OutboxEventType.REPORT_ENRICHED)
            )
        assert row is not None
        payload = row.payload
        # Submit-payload columns are all present…
        assert payload["report_id"] == str(report_id)
        assert payload["short_ref"] == "FG-SNAP"
        assert payload["hazard_type"] == "storm"
        assert payload["severity"] == "extreme"
        assert payload["water_depth_cm"] == 120
        assert payload["description"] == "caller words"
        assert payload["description_clean"] == "caller words"
        assert payload["location_raw"] == "RK"
        assert payload["flags"] == {"life_safety": True}
        # …plus enrichment columns.
        assert payload["confidence_score"] == 85
        assert payload["priority_score"] == 100
        assert payload["location_resolved"] == "canonical:RK Beach"
        assert payload["dedupe_group_id"] == "grp_1"
        assert payload["status"] == "enriched"
        assert payload["enriched_at"] is not None
        assert payload["notes"] == ["deep_extract ran", "geocode resolved"]
    finally:
        await eng.dispose()
        reset_engine()
