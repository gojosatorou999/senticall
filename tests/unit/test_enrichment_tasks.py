"""Per-task unit tests for the P6 enrichment DAG.

One test file covers all five in-memory tasks (assemble, extract,
geocode, dedupe, score) — they're small enough that separate files
would be more scroll than signal. `persist` gets its own integration
test in `tests/integration/test_enrichment_flow.py` because it needs
a real session + row round-trip."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from fg_voice.enrichment.errors import PermanentEnrichmentError
from fg_voice.enrichment.models import (
    EnrichmentContext,
    EnrichmentResult,
    ReportSnapshot,
)
from fg_voice.enrichment.tasks.assemble import assemble
from fg_voice.enrichment.tasks.dedupe import NoDedupeStrategy, dedupe
from fg_voice.enrichment.tasks.extract import (
    NoOpExtractor,
    RevisedSlots,
    deep_extract,
)
from fg_voice.enrichment.tasks.geocode import NoOpGeocoder, geocode
from fg_voice.enrichment.tasks.score import score
from fg_voice.persistence.db import (
    Base,
    get_session_maker,
    override_engine,
    reset_engine,
)
from fg_voice.persistence.models import Report


def _snapshot(**overrides) -> ReportSnapshot:
    """Build a snapshot with sensible defaults, override specific fields."""
    base = {
        "report_id": uuid4(),
        "short_ref": "FG-TEST",
        "source": "voice",
        "call_sid": "CA_TEST",
        "caller_hash": "hash",
        "hazard_type": "storm",
        "severity": "moderate",
        "water_depth_cm": 40,
        "description": "waves crashing onto the road at RK Beach",
        "description_clean": "waves crashing onto the road at RK Beach",
        "location_raw": "RK Beach",
        "flags": {},
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return ReportSnapshot(**base)


def _ctx(**overrides) -> EnrichmentContext:
    return EnrichmentContext(snapshot=_snapshot(**overrides))


# ─── assemble ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_assemble_projects_row_to_snapshot():
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
                    short_ref="FG-AS01",
                    source="voice",
                    call_sid="CA_AS",
                    caller_hash="h",
                    hazard_type="flood",
                    severity="extreme",
                    water_depth_cm=120,
                    description="raw with 9876543210",
                    description_clean="raw with [phone]",
                    location_raw="Vijayawada",
                    flags={"life_safety": True},
                )
            )
        async with get_session_maker()() as session:
            snap = await assemble(session, report_id)
        assert snap.report_id == report_id
        assert snap.short_ref == "FG-AS01"
        assert snap.severity == "extreme"
        assert snap.water_depth_cm == 120
        assert snap.description_clean == "raw with [phone]"
        assert snap.flags == {"life_safety": True}
    finally:
        await eng.dispose()
        reset_engine()


@pytest.mark.asyncio
async def test_assemble_missing_row_raises_permanent():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", pool_pre_ping=False)
    override_engine(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with get_session_maker()() as session:
            with pytest.raises(PermanentEnrichmentError, match="not found"):
                await assemble(session, uuid4())
    finally:
        await eng.dispose()
        reset_engine()


# ─── deep_extract ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_noop_extractor_yields_empty_revisions():
    ctx = _ctx()
    await deep_extract(ctx, extractor=NoOpExtractor())
    assert ctx.result.revised_slots == {}
    assert ctx.result.notes == []


@pytest.mark.asyncio
async def test_deep_extract_stashes_revised_slots():
    class FakeExtractor:
        async def extract(self, description):
            return RevisedSlots(
                values={"hazard_type": "flood", "severity": "extreme"},
                confidence=0.87,
                notes="deep pass revised via LLM",
            )

    ctx = _ctx(hazard_type="storm", severity="moderate")
    await deep_extract(ctx, extractor=FakeExtractor())
    assert ctx.result.revised_slots == {"hazard_type": "flood", "severity": "extreme"}
    assert any("deep_extract proposed" in n for n in ctx.result.notes)


@pytest.mark.asyncio
async def test_deep_extract_empty_revision_no_notes():
    class FakeExtractor:
        async def extract(self, description):
            return RevisedSlots(values={}, confidence=0.0)

    ctx = _ctx()
    await deep_extract(ctx, extractor=FakeExtractor())
    assert ctx.result.notes == []


# ─── geocode ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_noop_geocoder_leaves_resolved_none():
    ctx = _ctx()
    await geocode(ctx, geocoder=NoOpGeocoder())
    assert ctx.result.location_resolved is None


@pytest.mark.asyncio
async def test_geocode_stashes_resolved_when_available():
    class FakeGeocoder:
        async def resolve(self, raw):
            return f"Resolved: {raw}"

    ctx = _ctx(location_raw="RK Beach")
    await geocode(ctx, geocoder=FakeGeocoder())
    assert ctx.result.location_resolved == "Resolved: RK Beach"


@pytest.mark.asyncio
async def test_geocode_skips_when_no_raw_location():
    class ExplodingGeocoder:
        async def resolve(self, raw):
            raise AssertionError("must not be called")

    ctx = _ctx(location_raw=None)
    await geocode(ctx, geocoder=ExplodingGeocoder())
    assert ctx.result.location_resolved is None


# ─── dedupe ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_dedupe_leaves_group_none():
    class DummySession:
        pass

    ctx = _ctx()
    await dedupe(ctx, DummySession(), strategy=NoDedupeStrategy())  # type: ignore[arg-type]
    assert ctx.result.dedupe_group_id is None


@pytest.mark.asyncio
async def test_dedupe_stashes_group_when_matched():
    class FakeDedupe:
        async def group_for(self, ctx, session):
            return "grp_42"

    class DummySession:
        pass

    ctx = _ctx()
    await dedupe(ctx, DummySession(), strategy=FakeDedupe())  # type: ignore[arg-type]
    assert ctx.result.dedupe_group_id == "grp_42"


# ─── score ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_score_full_slots_high_confidence():
    ctx = _ctx()  # all four slots filled + water_depth
    await score(ctx)
    assert ctx.result.confidence_score is not None
    assert ctx.result.confidence_score >= 80
    assert ctx.result.priority_score == 60  # moderate


@pytest.mark.asyncio
async def test_score_empty_slots_low_confidence():
    ctx = _ctx(
        hazard_type=None,
        severity=None,
        location_raw=None,
        description=None,
        water_depth_cm=None,
    )
    await score(ctx)
    assert ctx.result.confidence_score is not None
    assert ctx.result.confidence_score <= 30
    assert ctx.result.priority_score == 40  # unknown → default


@pytest.mark.parametrize(
    ("severity", "expected"),
    [("extreme", 90), ("moderate", 60), ("light", 25), ("garbage", 40), (None, 40)],
)
@pytest.mark.asyncio
async def test_score_priority_ladder(severity, expected):
    ctx = _ctx(severity=severity)
    await score(ctx)
    assert ctx.result.priority_score == expected


@pytest.mark.asyncio
async def test_score_life_safety_flag_pins_priority_to_max():
    ctx = _ctx(severity="light", flags={"life_safety": True})
    await score(ctx)
    assert ctx.result.priority_score == 100


@pytest.mark.asyncio
async def test_score_revision_penalises_confidence():
    ctx_clean = _ctx()
    await score(ctx_clean)

    ctx_revised = _ctx()
    ctx_revised.result = EnrichmentResult(revised_slots={"hazard_type": "flood"})
    await score(ctx_revised)

    assert ctx_revised.result.confidence_score is not None
    assert ctx_clean.result.confidence_score is not None
    assert ctx_revised.result.confidence_score < ctx_clean.result.confidence_score
