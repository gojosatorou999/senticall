"""/healthz, /readyz, /metrics.

Health endpoints are load-bearing from P0: ECS uses them for task health
checks and rolling-deploy gates. `/healthz` is cheap liveness — is the
process up? `/readyz` is real readiness — are the dependencies this
process needs actually reachable, and is the pipeline draining?

The distinction matters under load: a task pinned at 100% CPU with a
stuck outbox is technically "alive" (`/healthz` OK) but shouldn't
receive new traffic (`/readyz` 503). ALB routes accordingly.

`/readyz` fans out to per-dependency checks with a hard timeout per
check so a stuck dep can't hang the endpoint. Each check reports its
own status (`ok` / `degraded` / `fail`); the top-level status is the
worst per-check status. `ok` = 200; anything else = 503.

Design notes:
- Checks run concurrently via `asyncio.gather` — the endpoint
  latency is `max(check_timeouts)`, not `sum`.
- Redis check is `skipped` when `REDIS_URL` is empty or the client
  fails to import; a partial deploy that doesn't use Redis stays
  healthy.
- Relay + DLQ checks report `skipped` when the relay is disabled
  (dev worker deploys, tests) — same rationale.
- Outbox + DLQ depth are informational thresholds — `degraded` above
  the threshold, not `fail`. Ops sees the drift without ALB
  immediately black-holing the task.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Final, Literal

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import and_, false, func, select
from sqlalchemy.exc import SQLAlchemyError

from fg_voice import __version__
from fg_voice.config import get_settings
from fg_voice.obs.logging import get_logger
from fg_voice.persistence.db import get_session_maker
from fg_voice.persistence.models import OutboxEntry
from fg_voice.persistence.relay import DEFAULT_MAX_RETRIES

log = get_logger(__name__)
router = APIRouter(tags=["health"])

# Per-check hard timeout. Tuned short enough that a stuck dep can't
# hold up an ALB health poll (default 10s poll interval, 2s timeout).
DEFAULT_CHECK_TIMEOUT_SEC: Final[float] = 1.5

CheckStatus = Literal["ok", "degraded", "fail", "skipped"]


@dataclass(slots=True)
class CheckResult:
    status: CheckStatus
    detail: str = ""


@router.get("/healthz", summary="liveness probe")
async def healthz() -> dict[str, Any]:
    """Liveness — process is up and responding. Cheap; no dependencies."""
    settings = get_settings()
    return {
        "status": "ok",
        "version": __version__,
        "agent_version": settings.fg_agent_version,
        "env": settings.fg_env,
    }


@router.get("/readyz", summary="readiness probe")
async def readyz(request: Request, response: Response) -> dict[str, Any]:
    """Fans out to per-dependency checks concurrently with a hard
    timeout per check. Overall status is the worst per-check status.
    Returns 200 for `ok`, 503 otherwise so ALB / ECS routing behaves."""
    settings = get_settings()
    timeout = settings.readyz_timeout_sec

    # Grab live task references off app.state so we don't depend on
    # main.py's module globals (which are None during unit tests that
    # spin up the app outside the real lifespan).
    relay_task = getattr(request.app.state, "relay_task", None)

    results = await asyncio.gather(
        _bounded_check("database", _check_database(), timeout),
        _bounded_check("redis", _check_redis(settings.redis_url), timeout),
        _bounded_check(
            "outbox_depth",
            _check_outbox_depth(settings.readyz_outbox_max_depth),
            timeout,
        ),
        _bounded_check(
            "dlq_depth",
            _check_dlq_depth(settings.readyz_dlq_max_depth),
            timeout,
        ),
        _bounded_check("relay_task", _check_relay_task(relay_task), timeout),
    )
    check_names = ("database", "redis", "outbox_depth", "dlq_depth", "relay_task")
    checks: dict[str, dict[str, str]] = {
        name: {"status": r.status, "detail": r.detail}
        for name, r in zip(check_names, results, strict=True)
    }
    overall = _worst_status([r.status for r in results])

    if overall != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {"status": overall, "checks": checks}


# ─── Per-check implementations ───────────────────────────────────────


async def _check_database() -> CheckResult:
    """`SELECT 1`-shaped ping via the session_maker. Any DB error →
    fail; a slow query gets caught upstream by the bounded-check
    timeout."""
    try:
        sm = get_session_maker()
        async with sm() as session:
            # `select(1)` doesn't type-check under SQLAlchemy 2.0's
            # `func.count()` idioms; a `select(func.count())` with a
            # never-true WHERE gives us the same "one row, no I/O"
            # ping cheaply across drivers.
            await session.execute(select(func.count()).where(false()))
        return CheckResult(status="ok")
    except SQLAlchemyError as exc:
        return CheckResult(status="fail", detail=f"{type(exc).__name__}: {exc}"[:200])
    except Exception as exc:
        return CheckResult(status="fail", detail=f"{type(exc).__name__}: {exc}"[:200])


async def _check_redis(redis_url: str) -> CheckResult:
    """PING via a fresh client. Redis is optional at some deploys;
    an empty URL reports `skipped` (still counts as healthy)."""
    if not redis_url:
        return CheckResult(status="skipped", detail="REDIS_URL empty")
    try:
        import redis.asyncio as redis
    except ImportError:
        return CheckResult(status="skipped", detail="redis client not installed")
    client = redis.from_url(redis_url, decode_responses=True)
    try:
        pong = await client.ping()
        if pong:
            return CheckResult(status="ok")
        return CheckResult(status="fail", detail="PING returned falsy")
    except Exception as exc:
        return CheckResult(status="fail", detail=f"{type(exc).__name__}: {exc}"[:200])
    finally:
        with contextlib.suppress(Exception):
            # `aclose()` is the async-preferred close; `close()` is
            # deprecated per redis-py 5.0.1. Type-ignore because the
            # bundled stubs (redis-py >=5) don't yet advertise it as a
            # method on `Redis`, only via the mixin.
            await client.aclose()  # type: ignore[attr-defined]


async def _check_outbox_depth(threshold: int) -> CheckResult:
    """Pending (unclaimed, still-retriable) rows. A growing pool is
    the "relay is falling behind" signal; report `degraded` above the
    threshold so ALB doesn't immediately drop the task but ops sees
    the drift."""
    try:
        sm = get_session_maker()
        async with sm() as session:
            depth = await session.scalar(
                select(func.count())
                .select_from(OutboxEntry)
                .where(
                    and_(
                        OutboxEntry.dispatched_at.is_(None),
                        OutboxEntry.retry_count < DEFAULT_MAX_RETRIES,
                    )
                )
            )
        depth = int(depth or 0)
        if depth > threshold:
            return CheckResult(
                status="degraded",
                detail=f"depth={depth} > threshold={threshold}",
            )
        return CheckResult(status="ok", detail=f"depth={depth}")
    except Exception as exc:
        return CheckResult(status="fail", detail=f"{type(exc).__name__}: {exc}"[:200])


async def _check_dlq_depth(threshold: int) -> CheckResult:
    """Rows past max_retries with `dispatched_at IS NULL`. Same
    predicate the DLQ admin API + DLQ monitor use."""
    try:
        sm = get_session_maker()
        async with sm() as session:
            depth = await session.scalar(
                select(func.count())
                .select_from(OutboxEntry)
                .where(
                    and_(
                        OutboxEntry.dispatched_at.is_(None),
                        OutboxEntry.retry_count >= DEFAULT_MAX_RETRIES,
                    )
                )
            )
        depth = int(depth or 0)
        if depth > threshold:
            return CheckResult(
                status="degraded",
                detail=f"depth={depth} > threshold={threshold}",
            )
        return CheckResult(status="ok", detail=f"depth={depth}")
    except Exception as exc:
        return CheckResult(status="fail", detail=f"{type(exc).__name__}: {exc}"[:200])


async def _check_relay_task(relay_task: asyncio.Task[None] | None) -> CheckResult:
    """Relay must be alive for the pipeline to drain. `None` = relay
    disabled (dev / test); reports `skipped`. `.done()` = the task
    exited (crash or shutdown mid-request); reports `fail` so ALB
    stops routing to this instance."""
    if relay_task is None:
        return CheckResult(status="skipped", detail="relay disabled")
    if relay_task.done():
        # The task crashed or was cancelled unexpectedly. Surface the
        # exception (if any) so ops sees WHY it's dead.
        exc = _task_exception(relay_task)
        detail = f"crashed: {type(exc).__name__}: {exc}"[:200] if exc else "task done"
        return CheckResult(status="fail", detail=detail)
    return CheckResult(status="ok")


def _task_exception(task: asyncio.Task[None]) -> BaseException | None:
    """`.exception()` raises if the task was cancelled; guard against
    both paths so the readyz endpoint never itself throws."""
    try:
        return task.exception()
    except (asyncio.CancelledError, asyncio.InvalidStateError):
        return None


# ─── Helpers ─────────────────────────────────────────────────────────


async def _bounded_check(
    name: str,
    coro: Awaitable[CheckResult],
    budget_sec: float,
) -> CheckResult:
    """Wraps one check coroutine in an `asyncio.wait_for` with a hard
    time budget. Timeouts report `fail` (dep is unreachable within
    the budget). Parameter renamed from `timeout` to sidestep the
    ASYNC109 lint (which flags async defs shadowing `asyncio.timeout`)."""
    try:
        return await asyncio.wait_for(coro, timeout=budget_sec)
    except TimeoutError:
        log.warning("readyz.check_timeout", check=name, timeout=budget_sec)
        return CheckResult(status="fail", detail=f"timeout after {budget_sec}s")
    except Exception as exc:
        log.exception("readyz.check_crashed", check=name, error=str(exc))
        return CheckResult(status="fail", detail=f"{type(exc).__name__}: {exc}"[:200])


# `ok`/`skipped` = 0 (healthy), `degraded` = 1, `fail` = 2. Worst wins.
_STATUS_RANK: Final[dict[CheckStatus, int]] = {
    "ok": 0,
    "skipped": 0,
    "degraded": 1,
    "fail": 2,
}


def _worst_status(statuses: list[CheckStatus]) -> CheckStatus:
    """Aggregate: if any check fails, overall fails; if any is
    degraded (but none fail), overall is degraded; otherwise ok.
    `skipped` counts as `ok` — dep isn't wired, that's not unhealthy."""
    worst = max(statuses, key=lambda s: _STATUS_RANK[s])
    return "ok" if worst == "skipped" else worst
