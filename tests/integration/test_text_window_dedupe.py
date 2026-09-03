"""TextWindowDedupe — real DB, fake time.

Covers every branch in `group_for`:

- No candidates → None
- Same hazard + district + window, but text below threshold → None
- Same everything, text above threshold, candidate has group_id →
  joins that group
- Same everything, text above threshold, candidate has NO group_id →
  mints a new group AND back-fills the earliest candidate
- Different hazard_type → not a candidate
- Different district → not a candidate
- Outside time window → not a candidate
- Own row (identity guard) → never a candidate against itself
- Empty description / no location → skips (returns None)
- Multiple matches with a mix of grouped/ungrouped — grouped wins
- Multiple grouped matches → ties broken by highest similarity

Uses an in-memory SQLite so tests never touch a real Postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from fg_voice.enrichment.dedupers.text_window import TextWindowDedupe
from fg_voice.enrichment.models import EnrichmentContext, ReportSnapshot
from fg_voice.persistence.db import (
    Base,
    get_session_maker,
    override_engine,
    reset_engine,
)
from fg_voice.persistence.models import Report


@pytest.fixture
async def _db():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", pool_pre_ping=False)
    override_engine(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()
        reset_engine()


def _snapshot(
    *,
    report_id: UUID | None = None,
    hazard_type: str | None = "flood",
    location: str | None = "Visakhapatnam, Andhra Pradesh",
    description: str = "waves crashing over the road knee deep",
    created_at: datetime | None = None,
) -> ReportSnapshot:
    return ReportSnapshot(
        report_id=report_id or uuid4(),
        short_ref="FG-DDX",
        source="voice",
        call_sid="CA_TEST",
        caller_hash="h",
        hazard_type=hazard_type,
        severity="moderate",
        water_depth_cm=40,
        description=description,
        description_clean=description,
        location_raw=location,
        flags={},
        created_at=created_at or datetime.now(UTC),
    )


def _ctx_with_resolved(snap: ReportSnapshot, resolved: str | None = None) -> EnrichmentContext:
    ctx = EnrichmentContext(snapshot=snap)
    if resolved:
        ctx.result.location_resolved = resolved
    return ctx


async def _insert_report(
    *,
    report_id: UUID | None = None,
    hazard_type: str = "flood",
    location_raw: str | None = "Visakhapatnam, Andhra Pradesh",
    location_resolved: str | None = None,
    description: str = "waves crashing over the road knee deep",
    dedupe_group_id: str | None = None,
    created_at: datetime | None = None,
    short_ref: str | None = None,
) -> Report:
    report_id = report_id or uuid4()
    row = Report(
        report_id=report_id,
        short_ref=short_ref or f"FG-{report_id.hex[:5].upper()}",
        source="voice",
        call_sid=f"CA_{report_id.hex[:6]}",
        caller_hash="h",
        hazard_type=hazard_type,
        severity="moderate",
        water_depth_cm=40,
        description=description,
        description_clean=description,
        location_raw=location_raw,
        location_resolved=location_resolved,
        dedupe_group_id=dedupe_group_id,
        flags={},
        created_at=created_at or datetime.now(UTC),
    )
    async with get_session_maker()() as session, session.begin():
        session.add(row)
    return row


# ─── No candidates / short-circuit ───────────────────────────────────


@pytest.mark.asyncio
async def test_returns_none_when_no_candidates(_db):
    dedupe = TextWindowDedupe()
    snap = _snapshot()
    async with get_session_maker()() as session:
        result = await dedupe.group_for(_ctx_with_resolved(snap), session)
    assert result is None


@pytest.mark.asyncio
async def test_missing_hazard_type_short_circuits(_db):
    await _insert_report()  # populate a matching candidate
    dedupe = TextWindowDedupe()
    snap = _snapshot(hazard_type=None)
    async with get_session_maker()() as session:
        result = await dedupe.group_for(_ctx_with_resolved(snap), session)
    assert result is None


@pytest.mark.asyncio
async def test_missing_location_short_circuits(_db):
    await _insert_report()
    dedupe = TextWindowDedupe()
    snap = _snapshot(location=None)
    async with get_session_maker()() as session:
        result = await dedupe.group_for(_ctx_with_resolved(snap), session)
    assert result is None


@pytest.mark.asyncio
async def test_empty_description_short_circuits(_db):
    await _insert_report()
    dedupe = TextWindowDedupe()
    snap = _snapshot(description="")
    async with get_session_maker()() as session:
        result = await dedupe.group_for(_ctx_with_resolved(snap), session)
    assert result is None


# ─── Filtering: not a candidate ──────────────────────────────────────


@pytest.mark.asyncio
async def test_different_hazard_type_not_matched(_db):
    await _insert_report(hazard_type="storm_surge")
    dedupe = TextWindowDedupe()
    snap = _snapshot(hazard_type="flood")
    async with get_session_maker()() as session:
        result = await dedupe.group_for(_ctx_with_resolved(snap), session)
    assert result is None


@pytest.mark.asyncio
async def test_different_district_not_matched(_db):
    await _insert_report(location_raw="Hyderabad, Telangana")
    dedupe = TextWindowDedupe()
    snap = _snapshot(location="Visakhapatnam, Andhra Pradesh")
    async with get_session_maker()() as session:
        result = await dedupe.group_for(_ctx_with_resolved(snap), session)
    assert result is None


@pytest.mark.asyncio
async def test_outside_time_window_not_matched(_db):
    # 24h old — outside the default 3h window
    old = datetime.now(UTC) - timedelta(hours=24)
    await _insert_report(created_at=old)
    dedupe = TextWindowDedupe(window_hours=3)
    snap = _snapshot(created_at=datetime.now(UTC))
    async with get_session_maker()() as session:
        result = await dedupe.group_for(_ctx_with_resolved(snap), session)
    assert result is None


@pytest.mark.asyncio
async def test_identity_guard_ignores_own_row(_db):
    """The current report's own row is in the DB by the time enrichment
    runs (SqlReportSink wrote it in the outbox transaction). Dedupe
    must not match a report against itself."""
    dedupe = TextWindowDedupe()
    my_id = uuid4()
    await _insert_report(report_id=my_id, description="waves over the road")
    snap = _snapshot(report_id=my_id, description="waves over the road")
    async with get_session_maker()() as session:
        result = await dedupe.group_for(_ctx_with_resolved(snap), session)
    assert result is None


# ─── Text similarity below threshold ─────────────────────────────────


@pytest.mark.asyncio
async def test_below_text_threshold_not_matched(_db):
    """Same district + hazard + window but completely different
    description → no dedupe. Prevents spurious grouping of unrelated
    incidents that just happened to share a district."""
    await _insert_report(description="power pole down blocking the street")
    dedupe = TextWindowDedupe()
    snap = _snapshot(description="tsunami wave washing everything away")
    async with get_session_maker()() as session:
        result = await dedupe.group_for(_ctx_with_resolved(snap), session)
    assert result is None


# ─── Happy paths ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_joins_existing_group(_db):
    """Candidate matches AND already has a group_id — this report
    joins the existing group."""
    existing_group = "existing-group-xyz"
    text = "water is up to my knees on the road"
    await _insert_report(description=text, dedupe_group_id=existing_group)

    dedupe = TextWindowDedupe()
    snap = _snapshot(description=text)
    async with get_session_maker()() as session:
        result = await dedupe.group_for(_ctx_with_resolved(snap), session)
    assert result == existing_group


@pytest.mark.asyncio
async def test_mints_group_and_backfills_earliest_candidate(_db):
    """First-of-its-cluster case: candidate matches but has no group.
    Dedupe mints a new group_id AND writes it back onto the earliest
    candidate row so the group has two members after the flow commits."""
    text = "waves crashing over the coastal road"
    earliest_id = uuid4()
    earlier = datetime.now(UTC) - timedelta(minutes=30)
    await _insert_report(report_id=earliest_id, description=text, created_at=earlier)

    dedupe = TextWindowDedupe()
    snap = _snapshot(description=text)
    async with get_session_maker()() as session, session.begin():
        result = await dedupe.group_for(_ctx_with_resolved(snap), session)
        assert result is not None
        assert len(result) == 32  # UUID4 hex

        # Verify the back-fill landed on the earliest candidate row
        # inside the same transaction.
        earliest = await session.get(Report, earliest_id)
        assert earliest is not None
        assert earliest.dedupe_group_id == result


@pytest.mark.asyncio
async def test_prefers_grouped_candidate_over_ungrouped(_db):
    """When some matches have a group_id and some don't, we join the
    grouped one (rather than mint a third)."""
    text = "water on the road going up"
    await _insert_report(
        description=text,
        dedupe_group_id="cluster-A",
        created_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    # An ungrouped candidate that also matches
    await _insert_report(
        description=text,
        created_at=datetime.now(UTC) - timedelta(minutes=20),
    )

    dedupe = TextWindowDedupe()
    snap = _snapshot(description=text)
    async with get_session_maker()() as session:
        result = await dedupe.group_for(_ctx_with_resolved(snap), session)
    assert result == "cluster-A"


@pytest.mark.asyncio
async def test_grouped_candidates_tiebreak_by_similarity(_db):
    """Two candidates in different existing groups; the one with the
    higher text similarity wins."""
    my_text = "water rising on Beach Road near the pier"
    # Close-match candidate in group-A
    await _insert_report(
        description="water rising on Beach Road near the pier",
        dedupe_group_id="group-A",
    )
    # Weaker-match candidate in group-B (still above threshold)
    await _insert_report(
        description="water rising on Beach Road area",
        dedupe_group_id="group-B",
    )

    dedupe = TextWindowDedupe()
    snap = _snapshot(description=my_text)
    async with get_session_maker()() as session:
        result = await dedupe.group_for(_ctx_with_resolved(snap), session)
    assert result == "group-A"


@pytest.mark.asyncio
async def test_uses_resolved_location_when_raw_absent(_db):
    """The current snapshot has no `location_raw` but the flow's
    accumulator has `location_resolved` from the geocode step.
    Dedupe must fall back to the resolved value."""
    await _insert_report(
        location_raw=None,
        location_resolved="Visakhapatnam, Andhra Pradesh",
        description="waves onto the road",
    )
    dedupe = TextWindowDedupe()
    snap = _snapshot(location=None, description="waves onto the road")
    ctx = _ctx_with_resolved(snap, resolved="Visakhapatnam, Andhra Pradesh")
    async with get_session_maker()() as session:
        result = await dedupe.group_for(ctx, session)
    assert result is not None  # matched
