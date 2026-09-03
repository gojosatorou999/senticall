"""Health endpoints are load-bearing — ECS task health checks depend on them.

Coverage:
- /healthz always 200 (liveness only)
- /readyz happy path — DB ok, redis skipped (empty URL), outbox depth
  ok, DLQ depth ok, relay task skipped (not wired in unit tests)
- /readyz degraded — outbox above threshold reports `degraded` +
  overall 503
- /readyz fail — DB unreachable → `fail` + 503
- Redis check: empty URL → skipped (still ok overall); unreachable
  URL → fail (503 overall)
- Relay task check: None → skipped; done-with-exception → fail
- Timeout guard — a check that hangs is caught within budget
- Worst-status aggregation
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from fg_voice.api import routes_health
from fg_voice.persistence.db import Base, get_session_maker, override_engine, reset_engine


@pytest.fixture
def client(dev_env: None) -> Iterator[httpx.Client]:
    """`/healthz` is dep-free; a TestClient works."""
    from fastapi.testclient import TestClient

    from fg_voice.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
async def async_client(dev_env: None) -> AsyncIterator[httpx.AsyncClient]:
    from fg_voice.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.fixture
async def _db():
    """In-memory SQLite — the readyz DB probe hits the same session_maker."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", pool_pre_ping=False)
    override_engine(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()
        reset_engine()


# ─── /healthz ─────────────────────────────────────────────────────────


def test_healthz_returns_200(client: httpx.Client) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert body["env"] == "dev"


# ─── /readyz happy path ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_readyz_happy_path_all_ok_or_skipped(
    async_client: httpx.AsyncClient, _db, monkeypatch
) -> None:
    monkeypatch.setenv("REDIS_URL", "")
    from fg_voice.config import get_settings

    get_settings.cache_clear()

    r = await async_client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    checks = body["checks"]
    assert checks["database"]["status"] == "ok"
    assert checks["redis"]["status"] == "skipped"
    assert checks["outbox_depth"]["status"] == "ok"
    assert checks["dlq_depth"]["status"] == "ok"
    # Relay task is not wired in unit-test app.state; `skipped`.
    assert checks["relay_task"]["status"] == "skipped"


# ─── /readyz degraded ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_readyz_degraded_when_outbox_above_threshold(
    async_client: httpx.AsyncClient, _db, monkeypatch
) -> None:
    """Outbox above threshold → `degraded` for that check; overall
    `degraded` + 503 so ops sees the drift."""
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("READYZ_OUTBOX_MAX_DEPTH", "1")
    from fg_voice.config import get_settings

    get_settings.cache_clear()

    from fg_voice.persistence.models import OutboxEntry

    sm = get_session_maker()
    async with sm() as session, session.begin():
        for _ in range(2):
            session.add(
                OutboxEntry(
                    report_id=None,
                    event_type="report.submitted",
                    payload={},
                    retry_count=0,
                )
            )

    r = await async_client.get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["checks"]["outbox_depth"]["status"] == "degraded"
    assert "depth=2" in body["checks"]["outbox_depth"]["detail"]


# ─── /readyz fail ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_readyz_fails_when_db_unreachable(
    async_client: httpx.AsyncClient, monkeypatch
) -> None:
    """No _db fixture → session_maker builds against a nonexistent DB."""
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pw@127.0.0.1:1/nowhere")
    from fg_voice.config import get_settings

    reset_engine()
    get_settings.cache_clear()

    r = await async_client.get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "fail"
    assert body["checks"]["database"]["status"] == "fail"


@pytest.mark.asyncio
async def test_readyz_redis_fail_trips_503(
    async_client: httpx.AsyncClient, _db, monkeypatch
) -> None:
    """A configured-but-unreachable Redis reports `fail`, not
    `skipped` — the operator opted in and the dep isn't there."""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv("READYZ_TIMEOUT_SEC", "0.3")
    from fg_voice.config import get_settings

    get_settings.cache_clear()

    r = await async_client.get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["checks"]["redis"]["status"] == "fail"


# ─── Relay-task check ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_relay_task_none_reports_skipped():
    result = await routes_health._check_relay_task(None)
    assert result.status == "skipped"


@pytest.mark.asyncio
async def test_relay_task_done_with_exception_reports_fail():
    """A task that crashed reports `fail` with the exception type in
    the detail so ops sees WHY the pipeline stopped."""

    async def _boom() -> None:
        raise RuntimeError("relay crashed under load")

    task: asyncio.Task[None] = asyncio.create_task(_boom())
    with pytest.raises(RuntimeError):
        await task

    result = await routes_health._check_relay_task(task)
    assert result.status == "fail"
    assert "RuntimeError" in result.detail
    assert "relay crashed" in result.detail


@pytest.mark.asyncio
async def test_relay_task_alive_reports_ok():
    async def _forever() -> None:
        await asyncio.sleep(60)

    task: asyncio.Task[None] = asyncio.create_task(_forever())
    try:
        result = await routes_health._check_relay_task(task)
        assert result.status == "ok"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ─── Bounded check timeout ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_bounded_check_timeout_reports_fail():
    """A check that hangs past `budget_sec` is caught and reported
    as `fail: timeout after Ns` — the endpoint never itself hangs."""

    async def _hang() -> routes_health.CheckResult:
        await asyncio.sleep(60)
        return routes_health.CheckResult(status="ok")

    result = await routes_health._bounded_check("test", _hang(), budget_sec=0.05)
    assert result.status == "fail"
    assert "timeout" in result.detail


# ─── Worst-status aggregation ────────────────────────────────────────


def test_worst_status_all_ok():
    assert routes_health._worst_status(["ok", "ok", "skipped"]) == "ok"


def test_worst_status_degraded_wins_over_ok():
    assert routes_health._worst_status(["ok", "degraded", "skipped"]) == "degraded"


def test_worst_status_fail_wins_over_degraded():
    assert routes_health._worst_status(["ok", "degraded", "fail", "skipped"]) == "fail"


def test_worst_status_only_skipped_is_ok():
    """A deploy where every optional dep was skipped is still healthy —
    prevents a mis-designed check from black-holing an otherwise-fine
    task."""
    assert routes_health._worst_status(["skipped", "skipped"]) == "ok"
