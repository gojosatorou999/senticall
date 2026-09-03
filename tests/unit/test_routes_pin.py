"""GET/POST /pin/{short_ref} — the SMS pin-drop landing page.

The endpoint is deliberately UN-gated (no admin key) because the
short_ref is delivered directly to the caller via SMS and acts as
its own capability token — same design decision as password-reset
links. See the docstring on `routes_pin` for the threat-model note."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fg_voice.persistence.db import Base, override_engine, reset_engine
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
async def client(
    db_engine, dev_env: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("CALLER_HASH_PEPPER", "test-pepper")
    monkeypatch.setenv("RELAY_ENABLED", "false")
    monkeypatch.setenv("SMS_PIN_OFFER_ENABLED", "false")
    from fg_voice.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _seed_report(engine, *, short_ref: str) -> None:
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        session.add(
            Report(
                short_ref=short_ref,
                source="voice",
                call_sid=f"CA_{short_ref}",
                caller_hash="h",
                status="pending_enrichment",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )


# ─── GET /pin/{short_ref} ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_pin_form_renders(client, db_engine):
    await _seed_report(db_engine, short_ref="FG-ABCD")
    r = await client.get("/pin/FG-ABCD")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # The rendered page must include the short_ref (so the caller
    # knows they landed on the right report) + a leaflet dep.
    assert "FG-ABCD" in r.text
    assert "leaflet" in r.text.lower()


@pytest.mark.asyncio
async def test_get_pin_form_unknown_ref_returns_404(client):
    r = await client.get("/pin/FG-XXXX")
    assert r.status_code == 404


# ─── POST /pin/{short_ref} ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_pin_writes_location_resolved(client, db_engine):
    await _seed_report(db_engine, short_ref="FG-1111")
    r = await client.post("/pin/FG-1111", data={"lat": 17.6868, "lng": 83.2185})
    assert r.status_code == 200
    assert "Pin saved" in r.text

    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as session:
        row = await session.scalar(select(Report).where(Report.short_ref == "FG-1111"))
        assert row is not None
        assert row.location_resolved is not None
        assert row.location_resolved.startswith("pin:")
        # 6-dp round-trip so the ops query is stable.
        assert "17.686800" in row.location_resolved
        assert "83.218500" in row.location_resolved


@pytest.mark.asyncio
async def test_post_pin_overwrites_previous(client, db_engine):
    await _seed_report(db_engine, short_ref="FG-2222")
    await client.post("/pin/FG-2222", data={"lat": 17.0, "lng": 82.0})
    r = await client.post("/pin/FG-2222", data={"lat": 18.5, "lng": 83.5})
    assert r.status_code == 200

    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as session:
        row = await session.scalar(select(Report).where(Report.short_ref == "FG-2222"))
        assert row is not None
        assert "18.500000" in row.location_resolved
        assert "17.000000" not in row.location_resolved


@pytest.mark.asyncio
async def test_post_pin_rejects_out_of_bounds_coordinates(client, db_engine):
    await _seed_report(db_engine, short_ref="FG-3333")
    r = await client.post("/pin/FG-3333", data={"lat": 0.0, "lng": 0.0})  # Atlantic
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_post_pin_unknown_ref_returns_404(client):
    r = await client.post("/pin/FG-NOPE", data={"lat": 17.0, "lng": 83.0})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_post_pin_missing_fields_returns_422(client, db_engine):
    await _seed_report(db_engine, short_ref="FG-4444")
    r = await client.post("/pin/FG-4444", data={"lat": 17.0})  # no lng
    assert r.status_code == 422
