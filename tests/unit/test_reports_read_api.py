"""GET /api/v1/reports + GET /api/v1/reports/{short_ref}.

In-memory SQLite via `override_engine`; SqlReportSink seeds real rows
so we prove the read path against the same schema production uses."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from fg_voice.conversation.sql_report_sink import SqlReportSink
from fg_voice.conversation.state import CallState, Slot, SlotValue
from fg_voice.persistence.db import (
    Base,
    get_session_maker,
    override_engine,
    reset_engine,
)


@pytest.fixture
async def db_engine():
    """Fresh in-memory SQLite per test. Uses a shared-cache URL so
    the AsyncClient + the seed helper hit the same schema."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", pool_pre_ping=False)
    override_engine(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()
    reset_engine()


@pytest.fixture
async def client(db_engine) -> AsyncIterator[httpx.AsyncClient]:
    """AsyncClient bound to the FastAPI app. `override_engine` above
    has already patched the module's session_maker."""
    from fg_voice.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _seed(
    call_sid: str,
    *,
    hazard: str = "storm",
    severity: str = "moderate",
    life_safety: bool = False,
    depth_cm: int | None = None,
    location: str = "RK Beach",
    created_at: datetime | None = None,
) -> str:
    """Write one report; return its short_ref. `created_at` override
    is stamped onto the row after the sink writes so the DESC ordering
    tests have deterministic timestamps."""
    state = CallState(call_sid=call_sid, caller_hash="testhash")
    state.set_slot(Slot.HAZARD_TYPE, SlotValue(value=hazard, confidence=0.9, source="asr"))
    state.set_slot(Slot.SEVERITY, SlotValue(value=severity, confidence=0.9, source="asr"))
    state.set_slot(Slot.LOCATION, SlotValue(value=location, confidence=0.6, source="asr"))
    if depth_cm is not None:
        state.set_slot(
            Slot.WATER_DEPTH_CM, SlotValue(value=depth_cm, confidence=1.0, source="dtmf")
        )
    if life_safety:
        state.add_flag("life_safety")
    submitted = await SqlReportSink().write(state)

    if created_at is not None:
        # Stamp a deterministic created_at so ordering + cursor tests
        # aren't at the mercy of `datetime.now(UTC)` timing.
        from sqlalchemy import update

        from fg_voice.persistence.models import Report

        sm = get_session_maker()
        async with sm() as session, session.begin():
            await session.execute(
                update(Report)
                .where(Report.report_id == submitted.report_id)
                .values(created_at=created_at, updated_at=created_at)
            )
    return submitted.short_ref


# ─── Single lookup ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_report_by_short_ref_returns_row(client):
    short_ref = await _seed("CA_ONE", hazard="abnormal_tide", severity="extreme")

    r = await client.get(f"/api/v1/reports/{short_ref}")
    assert r.status_code == 200
    body = r.json()
    assert body["short_ref"] == short_ref
    assert body["hazard_type"] == "abnormal_tide"
    assert body["severity"] == "extreme"
    assert body["source"] == "voice"
    assert body["status"] == "pending_enrichment"


@pytest.mark.asyncio
async def test_get_report_returns_404_when_missing(client):
    r = await client.get("/api/v1/reports/FG-NOPE")
    assert r.status_code == 404
    assert "FG-NOPE" in r.json()["detail"]


# ─── List: ordering + pagination ─────────────────────────────────────


@pytest.mark.asyncio
async def test_list_orders_newest_first(client):
    now = datetime.now(UTC)
    ref_old = await _seed("CA_OLD", created_at=now - timedelta(minutes=10))
    ref_mid = await _seed("CA_MID", created_at=now - timedelta(minutes=5))
    ref_new = await _seed("CA_NEW", created_at=now)

    r = await client.get("/api/v1/reports")
    assert r.status_code == 200
    items = r.json()["items"]
    order = [i["short_ref"] for i in items]
    assert order == [ref_new, ref_mid, ref_old]


@pytest.mark.asyncio
async def test_list_pagination_cursor_walks_pages(client):
    now = datetime.now(UTC)
    refs: list[str] = []
    for i in range(5):
        refs.append(await _seed(f"CA_{i:03d}", created_at=now - timedelta(minutes=i)))
    # Newest first — refs[0] is the newest.

    r1 = await client.get("/api/v1/reports?limit=2")
    body1 = r1.json()
    assert len(body1["items"]) == 2
    assert body1["next_cursor"] is not None
    assert [i["short_ref"] for i in body1["items"]] == [refs[0], refs[1]]

    r2 = await client.get(f"/api/v1/reports?limit=2&cursor={body1['next_cursor']}")
    body2 = r2.json()
    assert len(body2["items"]) == 2
    assert [i["short_ref"] for i in body2["items"]] == [refs[2], refs[3]]

    r3 = await client.get(f"/api/v1/reports?limit=2&cursor={body2['next_cursor']}")
    body3 = r3.json()
    assert len(body3["items"]) == 1
    assert body3["items"][0]["short_ref"] == refs[4]
    assert body3["next_cursor"] is None  # end of list


@pytest.mark.asyncio
async def test_list_invalid_cursor_returns_400(client):
    r = await client.get("/api/v1/reports?cursor=not-base64!!!")
    assert r.status_code == 400
    assert "cursor" in r.json()["detail"].lower()


# ─── List: filters ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_filter_by_hazard_type(client):
    await _seed("CA_STORM_A", hazard="storm")
    await _seed("CA_STORM_B", hazard="storm")
    await _seed("CA_OIL", hazard="sludge_oil")

    r = await client.get("/api/v1/reports?hazard_type=storm")
    items = r.json()["items"]
    assert len(items) == 2
    for item in items:
        assert item["hazard_type"] == "storm"


@pytest.mark.asyncio
async def test_list_filter_by_severity(client):
    await _seed("CA_LO", severity="light")
    await _seed("CA_MED", severity="moderate")
    await _seed("CA_HI", severity="extreme")

    r = await client.get("/api/v1/reports?severity=extreme")
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["severity"] == "extreme"


@pytest.mark.asyncio
async def test_list_filter_by_life_safety_flag(client):
    await _seed("CA_OK", life_safety=False)
    await _seed("CA_TRIPWIRE_1", life_safety=True)
    await _seed("CA_TRIPWIRE_2", life_safety=True)

    r_true = await client.get("/api/v1/reports?life_safety=true")
    items_true = r_true.json()["items"]
    assert len(items_true) == 2
    for item in items_true:
        assert item["flags"] and item["flags"].get("life_safety") is True

    r_false = await client.get("/api/v1/reports?life_safety=false")
    items_false = r_false.json()["items"]
    assert len(items_false) == 1
    assert not (items_false[0]["flags"] or {}).get("life_safety")


@pytest.mark.asyncio
async def test_list_filter_by_date_range(client):
    now = datetime.now(UTC)
    await _seed("CA_PAST", created_at=now - timedelta(hours=2))
    ref_mid = await _seed("CA_MID", created_at=now - timedelta(minutes=30))
    ref_new = await _seed("CA_NEW", created_at=now)

    from_ = (now - timedelta(hours=1)).isoformat()
    # `params=` handles URL-encoding of the `+` in the ISO timestamp;
    # passing raw into the path leaves the `+` interpreted as a space.
    r = await client.get("/api/v1/reports", params={"from": from_})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    refs = [i["short_ref"] for i in items]
    assert set(refs) == {ref_mid, ref_new}


@pytest.mark.asyncio
async def test_list_filter_by_source(client):
    """Only 'voice' rows exist today, but the endpoint has to accept
    other values without erroring — future app/web/whatsapp sources
    share the same table (§13.1)."""
    await _seed("CA_V")
    r = await client.get("/api/v1/reports?source=voice")
    assert len(r.json()["items"]) == 1
    r_empty = await client.get("/api/v1/reports?source=web")
    assert r_empty.json()["items"] == []


# ─── Validation ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_limit_out_of_range_rejected(client):
    r_low = await client.get("/api/v1/reports?limit=0")
    assert r_low.status_code == 422
    r_high = await client.get("/api/v1/reports?limit=999")
    assert r_high.status_code == 422


@pytest.mark.asyncio
async def test_list_empty_db_returns_empty_page(client):
    r = await client.get("/api/v1/reports")
    body = r.json()
    assert body["items"] == []
    assert body["next_cursor"] is None
