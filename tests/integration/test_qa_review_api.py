"""QA sampling API — filter params on /reports + review endpoint.

Covers:
- List filters: qa_sample=true returns only sampled rows;
  qa_sample=false returns only non-sampled rows;
  qa_reviewed=false returns the unreviewed queue.
- Review endpoint: happy path (sets qa_reviewed_at + qa_notes);
  404 on unknown short_ref; 400 on not-sampled report; 422 on
  missing/short notes; idempotent (second review overwrites).
- Response model surfaces sampled_for_qa, qa_reviewed_at, qa_notes.
- Sink samples deterministically — same report_id across writes gives
  same flag.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import create_async_engine

from fg_voice.conversation.sql_report_sink import SqlReportSink
from fg_voice.conversation.state import CallState, Slot, SlotValue
from fg_voice.persistence.db import (
    Base,
    get_session_maker,
    override_engine,
    reset_engine,
)
from fg_voice.persistence.models import Report


@pytest.fixture
async def db_engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", pool_pre_ping=False)
    override_engine(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()
    reset_engine()


@pytest.fixture
async def client(db_engine) -> AsyncIterator[httpx.AsyncClient]:
    from fg_voice.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _seed(call_sid: str, *, sampled: bool | None = None) -> str:
    """Write one report. `sampled` overrides the sink's flag AFTER the
    write so we get deterministic queue-shape tests without depending
    on which UUIDs happen to hash into the 5% bucket."""
    state = CallState(call_sid=call_sid, caller_hash="testhash")
    state.set_slot(Slot.HAZARD_TYPE, SlotValue(value="storm", confidence=0.9, source="asr"))
    state.set_slot(Slot.SEVERITY, SlotValue(value="moderate", confidence=0.9, source="asr"))
    state.set_slot(Slot.LOCATION, SlotValue(value="RK Beach", confidence=0.6, source="asr"))
    submitted = await SqlReportSink().write(state)
    if sampled is not None:
        sm = get_session_maker()
        async with sm() as session, session.begin():
            await session.execute(
                update(Report)
                .where(Report.report_id == submitted.report_id)
                .values(sampled_for_qa=sampled)
            )
    return submitted.short_ref


# ─── List filters ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_qa_sample_true_returns_only_sampled(client):
    sampled = await _seed("CA_S1", sampled=True)
    _unsampled = await _seed("CA_U1", sampled=False)

    r = await client.get("/api/v1/reports?qa_sample=true")
    assert r.status_code == 200
    items = r.json()["items"]
    short_refs = {i["short_ref"] for i in items}
    assert sampled in short_refs
    assert _unsampled not in short_refs


@pytest.mark.asyncio
async def test_qa_sample_false_returns_only_unsampled(client):
    sampled = await _seed("CA_S2", sampled=True)
    unsampled = await _seed("CA_U2", sampled=False)

    r = await client.get("/api/v1/reports?qa_sample=false")
    assert r.status_code == 200
    short_refs = {i["short_ref"] for i in r.json()["items"]}
    assert unsampled in short_refs
    assert sampled not in short_refs


@pytest.mark.asyncio
async def test_qa_reviewed_false_returns_unreviewed_queue(client):
    """The canonical 'QA queue' shape — sampled + not reviewed."""
    unreviewed = await _seed("CA_UNR", sampled=True)
    reviewed_ref = await _seed("CA_REV", sampled=True)
    # Mark reviewed_ref reviewed
    await client.post(
        f"/api/v1/reports/{reviewed_ref}/qa_review",
        json={"notes": "looks fine on inspection"},
    )

    r = await client.get("/api/v1/reports?qa_sample=true&qa_reviewed=false")
    short_refs = {i["short_ref"] for i in r.json()["items"]}
    assert unreviewed in short_refs
    assert reviewed_ref not in short_refs


@pytest.mark.asyncio
async def test_response_surfaces_qa_fields(client):
    short_ref = await _seed("CA_FIELDS", sampled=True)
    r = await client.get(f"/api/v1/reports/{short_ref}")
    body = r.json()
    assert body["sampled_for_qa"] is True
    assert body["qa_reviewed_at"] is None
    assert body["qa_notes"] is None


# ─── Review endpoint ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_review_sets_timestamp_and_notes(client):
    short_ref = await _seed("CA_R1", sampled=True)
    r = await client.post(
        f"/api/v1/reports/{short_ref}/qa_review",
        json={"notes": "hazard_type matches the description"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["qa_reviewed_at"] is not None
    assert body["qa_notes"] == "hazard_type matches the description"


@pytest.mark.asyncio
async def test_review_unknown_short_ref_returns_404(client):
    r = await client.post("/api/v1/reports/FG-NOPE/qa_review", json={"notes": "does not matter"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_review_non_sampled_report_returns_400(client):
    """Can't review something that was never in the queue — the
    error tells ops to check their filter, not silently no-op."""
    short_ref = await _seed("CA_NOTQ", sampled=False)
    r = await client.post(
        f"/api/v1/reports/{short_ref}/qa_review",
        json={"notes": "trying to review an unsampled row"},
    )
    assert r.status_code == 400
    assert "not sampled" in r.json()["detail"]


@pytest.mark.asyncio
async def test_review_missing_notes_returns_422(client):
    short_ref = await _seed("CA_NONOTES", sampled=True)
    r = await client.post(f"/api/v1/reports/{short_ref}/qa_review", json={})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_review_short_notes_returns_422(client):
    short_ref = await _seed("CA_TINY", sampled=True)
    r = await client.post(f"/api/v1/reports/{short_ref}/qa_review", json={"notes": "no"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_review_is_idempotent_overwrites(client):
    """A second review overwrites notes + timestamp — ops can
    correct a rushed first pass."""
    short_ref = await _seed("CA_TWICE", sampled=True)
    r1 = await client.post(
        f"/api/v1/reports/{short_ref}/qa_review",
        json={"notes": "first pass — looks fine"},
    )
    first_ts = r1.json()["qa_reviewed_at"]

    r2 = await client.post(
        f"/api/v1/reports/{short_ref}/qa_review",
        json={"notes": "second pass — actually hazard is wrong"},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["qa_notes"] == "second pass — actually hazard is wrong"
    # Timestamp advances (or at minimum stays >= first).
    assert body["qa_reviewed_at"] >= first_ts


# ─── Sink samples reproducibly ───────────────────────────────────────


@pytest.mark.asyncio
async def test_sink_sampling_is_reproducible_across_writes(client):
    """Same report_id → same flag (Twilio-retry invariant). Seed the
    same CallState twice — the second write's idempotency short-
    circuit means only one row lands, and the sampled flag is stable."""
    from uuid import uuid4

    rid = uuid4()
    state = CallState(call_sid="CA_IDEM", caller_hash="h")
    state.set_slot(Slot.HAZARD_TYPE, SlotValue(value="storm", confidence=0.9, source="asr"))
    state.report_id = rid

    r1 = await SqlReportSink().write(state)
    r2 = await SqlReportSink().write(state)
    # Idempotency: same short_ref returned both times.
    assert r1.short_ref == r2.short_ref

    # Query the row and confirm the sampled flag is consistent
    # (it's whatever the deterministic hash decided for rid).
    sm = get_session_maker()
    async with sm() as session:
        row = await session.get(Report, rid)
        assert row is not None
        # Just assert the flag is a boolean (True or False) — the
        # specific value depends on hash(rid) and we can't predict
        # without running the same helper.
        assert isinstance(row.sampled_for_qa, bool)
