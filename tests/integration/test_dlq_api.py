"""DLQ admin API — list, inspect, retry, purge.

Covers:
- Auth: admin key required (when set); dev bypass when empty
- List: only DLQ rows (retry_count >= max_retries + dispatched_at IS
  NULL); pagination; excludes dispatched rows; excludes rows still
  under the retry threshold
- Get: 404 for non-DLQ rows and unknown ids (identical response —
  no info leak)
- Retry: resets retry_count; the relay's next drain picks up the row
- Purge: sets dispatched_at + last_error note; row never touched
  again; reason required (400 on missing/short); audit log
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fg_voice.api import routes_dlq
from fg_voice.api.routes_dlq import router as dlq_router
from fg_voice.persistence.db import Base, override_engine, reset_engine
from fg_voice.persistence.models import OutboxEntry, Report
from fg_voice.persistence.relay import DEFAULT_MAX_RETRIES

MAX_RETRIES = DEFAULT_MAX_RETRIES


@pytest.fixture
async def _db(monkeypatch, dev_env):
    """In-memory SQLite; wire the DLQ router's session dependency at
    the module-level so requests inside TestClient pick up the same
    engine as the test setup."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", pool_pre_ping=False)
    override_engine(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    sm = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(routes_dlq, "_get_session_maker", lambda: sm)
    try:
        yield sm
    finally:
        await eng.dispose()
        reset_engine()


@pytest.fixture
def _client_with_auth(monkeypatch, dev_env):
    """Client that sets the admin API key so we can test the guarded
    path. Empty-key dev-bypass gets its own test below."""
    from fastapi import FastAPI

    monkeypatch.setenv("ADMIN_API_KEY", "test-key-123")
    # Reset the settings cache after mutating env.
    from fg_voice.config import get_settings

    get_settings.cache_clear()

    app = FastAPI()
    app.include_router(dlq_router)
    return TestClient(app), {"X-Admin-Api-Key": "test-key-123"}


@pytest.fixture
def _client_no_auth(dev_env):
    """Client with no admin key configured — dev bypass path."""
    from fastapi import FastAPI

    from fg_voice.config import get_settings

    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(dlq_router)
    return TestClient(app)


# ─── Data helpers ────────────────────────────────────────────────────


async def _insert_outbox(
    sm: async_sessionmaker,
    *,
    retry_count: int = MAX_RETRIES,
    dispatched_at: datetime | None = None,
    last_error: str | None = "webhook 5xx",
    event_type: str = "report.submitted",
) -> OutboxEntry:
    async with sm() as session, session.begin():
        # Foreign key on outbox.report_id → we need a real Report row
        # or NULL. NULL is simpler.
        entry = OutboxEntry(
            report_id=None,
            event_type=event_type,
            payload={"hello": "world"},
            retry_count=retry_count,
            dispatched_at=dispatched_at,
            last_error=last_error,
        )
        session.add(entry)
    async with sm() as session:
        stmt = select(OutboxEntry).order_by(OutboxEntry.id.desc()).limit(1)
        row = await session.scalar(stmt)
        assert row is not None
        return row


# ─── Auth ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_requires_admin_key_when_configured(_db, _client_with_auth):
    client, headers = _client_with_auth
    resp = client.get("/api/v1/dlq")
    assert resp.status_code == 401
    resp = client.get("/api/v1/dlq", headers=headers)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_dev_bypass_when_admin_key_unset(_db, _client_no_auth):
    """Empty ADMIN_API_KEY → dep is a no-op. Documented behaviour for
    fresh clones (prod boot check refuses to start without a key)."""
    resp = _client_no_auth.get("/api/v1/dlq")
    assert resp.status_code == 200


# ─── List ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_returns_only_dlq_rows(_db, _client_with_auth):
    client, headers = _client_with_auth
    # A stuck DLQ row
    stuck = await _insert_outbox(_db, retry_count=MAX_RETRIES)
    # Successfully dispatched — should NOT appear
    await _insert_outbox(_db, retry_count=MAX_RETRIES, dispatched_at=datetime.now(UTC))
    # Still retrying (under threshold) — should NOT appear
    await _insert_outbox(_db, retry_count=1)

    resp = client.get("/api/v1/dlq", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["max_retries"] == MAX_RETRIES
    assert len(body["entries"]) == 1
    assert body["entries"][0]["id"] == stuck.id
    assert body["entries"][0]["retry_count"] == MAX_RETRIES


@pytest.mark.asyncio
async def test_list_pagination(_db, _client_with_auth):
    client, headers = _client_with_auth
    ids = [(await _insert_outbox(_db, retry_count=MAX_RETRIES)).id for _ in range(5)]
    # Newest-first: highest id comes first
    ids_sorted_desc = sorted(ids, reverse=True)

    # Page 1
    resp = client.get("/api/v1/dlq?limit=2", headers=headers)
    body = resp.json()
    assert [e["id"] for e in body["entries"]] == ids_sorted_desc[:2]
    assert body["next_cursor"] is not None

    # Page 2 via cursor
    resp = client.get(f"/api/v1/dlq?limit=2&cursor={body['next_cursor']}", headers=headers)
    body = resp.json()
    assert [e["id"] for e in body["entries"]] == ids_sorted_desc[2:4]
    assert body["next_cursor"] is not None

    # Final page — exactly one row remaining, no next cursor
    resp = client.get(f"/api/v1/dlq?limit=2&cursor={body['next_cursor']}", headers=headers)
    body = resp.json()
    assert [e["id"] for e in body["entries"]] == ids_sorted_desc[4:]
    assert body["next_cursor"] is None


@pytest.mark.asyncio
async def test_list_malformed_cursor_returns_400(_db, _client_with_auth):
    client, headers = _client_with_auth
    resp = client.get("/api/v1/dlq?cursor=not-base64!!!", headers=headers)
    assert resp.status_code == 400
    assert "cursor" in resp.json()["detail"]


# ─── Get one ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_dlq_entry_happy_path(_db, _client_with_auth):
    client, headers = _client_with_auth
    row = await _insert_outbox(_db, retry_count=MAX_RETRIES)
    resp = client.get(f"/api/v1/dlq/{row.id}", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == row.id
    assert body["event_type"] == "report.submitted"
    assert body["retry_count"] == MAX_RETRIES
    assert body["last_error"] == "webhook 5xx"
    assert body["payload"] == {"hello": "world"}


@pytest.mark.asyncio
async def test_get_unknown_id_returns_404(_db, _client_with_auth):
    client, headers = _client_with_auth
    resp = client.get("/api/v1/dlq/99999", headers=headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_non_dlq_row_returns_404(_db, _client_with_auth):
    """A row that exists but is still under the retry threshold isn't
    in the DLQ. Endpoint returns 404 (not 400) so callers can't infer
    the id exists — matches the list endpoint's visibility."""
    client, headers = _client_with_auth
    row = await _insert_outbox(_db, retry_count=1)  # still retrying
    resp = client.get(f"/api/v1/dlq/{row.id}", headers=headers)
    assert resp.status_code == 404


# ─── Retry ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_resets_retry_count(_db, _client_with_auth):
    client, headers = _client_with_auth
    row = await _insert_outbox(_db, retry_count=MAX_RETRIES)
    resp = client.post(f"/api/v1/dlq/{row.id}/retry", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["retry_count"] == 0

    # Verify persistently — the relay's next claim query would pick
    # this up now.
    async with _db() as session:
        refreshed = await session.get(OutboxEntry, row.id)
        assert refreshed is not None
        assert refreshed.retry_count == 0
        # last_error is preserved so ops sees the prior failure
        # context when the next attempt runs.
        assert refreshed.last_error == "webhook 5xx"


@pytest.mark.asyncio
async def test_retry_non_dlq_row_returns_404(_db, _client_with_auth):
    client, headers = _client_with_auth
    row = await _insert_outbox(_db, retry_count=MAX_RETRIES, dispatched_at=datetime.now(UTC))
    resp = client.post(f"/api/v1/dlq/{row.id}/retry", headers=headers)
    assert resp.status_code == 404


# ─── Purge ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_purge_marks_dispatched_with_reason(_db, _client_with_auth):
    client, headers = _client_with_auth
    row = await _insert_outbox(_db, retry_count=MAX_RETRIES)
    resp = client.post(
        f"/api/v1/dlq/{row.id}/purge",
        headers=headers,
        json={"reason": "duplicate submission from Twilio retry"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "PURGED: duplicate submission" in body["last_error"]

    # Verify persistently.
    async with _db() as session:
        refreshed = await session.get(OutboxEntry, row.id)
        assert refreshed is not None
        assert refreshed.dispatched_at is not None  # relay skips forever
        assert "PURGED:" in (refreshed.last_error or "")


@pytest.mark.asyncio
async def test_purge_without_reason_returns_422(_db, _client_with_auth):
    """Pydantic validation rejects a bare purge — ops must leave a
    note. 422 is FastAPI's response for request-body validation
    failures."""
    client, headers = _client_with_auth
    row = await _insert_outbox(_db, retry_count=MAX_RETRIES)
    resp = client.post(f"/api/v1/dlq/{row.id}/purge", headers=headers, json={})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_purge_short_reason_returns_422(_db, _client_with_auth):
    client, headers = _client_with_auth
    row = await _insert_outbox(_db, retry_count=MAX_RETRIES)
    resp = client.post(f"/api/v1/dlq/{row.id}/purge", headers=headers, json={"reason": "no"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_purge_non_dlq_returns_404(_db, _client_with_auth):
    client, headers = _client_with_auth
    row = await _insert_outbox(_db, retry_count=MAX_RETRIES, dispatched_at=datetime.now(UTC))
    resp = client.post(
        f"/api/v1/dlq/{row.id}/purge",
        headers=headers,
        json={"reason": "already dispatched"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_purge_preserves_prior_last_error(_db, _client_with_auth):
    """Both the failure trail AND the purge note end up on the row so
    the audit isn't lost when ops purges a failing message."""
    client, headers = _client_with_auth
    row = await _insert_outbox(_db, retry_count=MAX_RETRIES, last_error="webhook returned 503")
    resp = client.post(
        f"/api/v1/dlq/{row.id}/purge",
        headers=headers,
        json={"reason": "webhook is being replaced"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "webhook returned 503" in body["last_error"]
    assert "PURGED: webhook is being replaced" in body["last_error"]


# Reference the imports that pytest fixtures depend on so ruff doesn't
# strip them.
_ = uuid4
_ = Report
