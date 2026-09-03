"""Admin API-key auth on the /reports* endpoints.

Also verifies the guard is NOT applied to Twilio webhooks (they use
signature validation) or health probes (public)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from fg_voice.persistence.db import Base, override_engine, reset_engine

TEST_API_KEY = "test-admin-key-please-rotate"


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
async def authed_client(
    db_engine, dev_env: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    """Auth ENABLED — a real API key is set."""
    monkeypatch.setenv("ADMIN_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("CALLER_HASH_PEPPER", "test-pepper")
    monkeypatch.setenv("RELAY_ENABLED", "false")  # no background task

    from fg_voice.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.fixture
async def open_client(
    db_engine, dev_env: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    """Auth DISABLED — the dev bypass path (ADMIN_API_KEY empty)."""
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    monkeypatch.setenv("CALLER_HASH_PEPPER", "test-pepper")
    monkeypatch.setenv("RELAY_ENABLED", "false")

    from fg_voice.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


# ─── 401 cases ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_reports_401_when_key_missing(authed_client):
    r = await authed_client.get("/api/v1/reports")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate", "").startswith("X-Admin-Api-Key")


@pytest.mark.asyncio
async def test_list_reports_401_when_key_wrong(authed_client):
    r = await authed_client.get("/api/v1/reports", headers={"X-Admin-Api-Key": "not-the-key"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_single_report_401_when_key_missing(authed_client):
    r = await authed_client.get("/api/v1/reports/FG-XXXX")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_export_csv_401_when_key_missing(authed_client):
    r = await authed_client.get("/api/v1/reports/export.csv")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_stream_401_when_key_missing(authed_client):
    r = await authed_client.get("/api/v1/reports/stream")
    assert r.status_code == 401


# ─── 200 with correct key ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_reports_200_with_correct_key(authed_client):
    r = await authed_client.get("/api/v1/reports", headers={"X-Admin-Api-Key": TEST_API_KEY})
    assert r.status_code == 200
    body = r.json()
    assert "items" in body


@pytest.mark.asyncio
async def test_get_missing_report_still_404_with_correct_key(authed_client):
    """Auth passes → normal 404 for a missing short_ref (proves auth
    doesn't shadow the app-level not-found)."""
    r = await authed_client.get(
        "/api/v1/reports/FG-NOPE", headers={"X-Admin-Api-Key": TEST_API_KEY}
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_export_csv_200_with_correct_key(authed_client):
    r = await authed_client.get(
        "/api/v1/reports/export.csv", headers={"X-Admin-Api-Key": TEST_API_KEY}
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")


# ─── Dev bypass: no key set → all endpoints open ─────────────────────


@pytest.mark.asyncio
async def test_dev_bypass_list_open_when_key_setting_empty(open_client):
    r = await open_client.get("/api/v1/reports")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_dev_bypass_export_open_when_key_setting_empty(open_client):
    r = await open_client.get("/api/v1/reports/export.csv")
    assert r.status_code == 200


# ─── Auth NOT applied to unrelated paths ─────────────────────────────


@pytest.mark.asyncio
async def test_healthz_stays_public_even_with_admin_key_set(authed_client):
    """Health probes must never require the admin key — ALB / ECS
    healthchecks don't send it."""
    r = await authed_client.get("/healthz")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_voice_webhook_uses_twilio_signature_not_admin_key(
    dev_env: None, monkeypatch: pytest.MonkeyPatch
):
    """`/voice/inbound` must reject a request lacking the Twilio
    signature (403), NOT respond 401 as if the admin key applied."""
    monkeypatch.setenv("ADMIN_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("CALLER_HASH_PEPPER", "test-pepper")
    monkeypatch.setenv("RELAY_ENABLED", "false")

    from fg_voice.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Send with the admin key (which shouldn't help) and no
        # Twilio signature.
        r = await client.post(
            "/voice/inbound",
            data={"CallSid": "CA_x", "From": "+1"},
            headers={"X-Admin-Api-Key": TEST_API_KEY},
        )
        assert r.status_code == 403
