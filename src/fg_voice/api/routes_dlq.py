"""Dead Letter Queue admin API.

When the outbox relay hits `max_retries` on a row, it logs
`outbox.dead_lettered` and leaves the row in place — `dispatched_at
IS NULL` but `retry_count >= max_retries` means the relay's claim
query filters it out. Without an admin surface, those rows silently
accumulate until someone reads the logs.

Endpoints (all guarded by `require_admin_api_key`):

- `GET  /api/v1/dlq`               — list stuck rows (paginated)
- `GET  /api/v1/dlq/{outbox_id}`   — inspect one (payload + last_error)
- `POST /api/v1/dlq/{outbox_id}/retry` — reset retry_count to 0 so
  the relay picks it up on the next poll. Idempotent; on success
  returns the reset row.
- `POST /api/v1/dlq/{outbox_id}/purge` — mark `dispatched_at=now()`
  with a purge reason on `last_error`. The row is skipped forever;
  the audit trail persists. Requires a `reason` in the request body
  so ops can't purge without leaving a note.

Retry ordering: the relay claims stuck rows by `created_at` ascending,
so a retried row goes to the head of the queue on its next poll.

Explicitly NOT covered here (P7 concern):
- Bulk retry / purge (needed at real-incident scale, not MVP)
- DLQ webhooks / paging (routes through the alert dispatcher; add
  when the log-scan monitor turns out to be too passive)
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from fg_voice.api.auth import AdminApiKey
from fg_voice.obs.logging import get_logger
from fg_voice.persistence.db import get_session_maker
from fg_voice.persistence.models import OutboxEntry
from fg_voice.persistence.relay import DEFAULT_MAX_RETRIES

log = get_logger(__name__)
router = APIRouter(prefix="/api/v1/dlq", tags=["dlq"])


# ─── DB session dependency ───────────────────────────────────────────


async def _session_dep() -> AsyncIterator[AsyncSession]:
    sm = _get_session_maker()
    async with sm() as session:
        yield session


def _get_session_maker() -> async_sessionmaker[AsyncSession]:
    """Wrapped so tests can monkeypatch this attribute."""
    return get_session_maker()


# ─── DLQ threshold ───────────────────────────────────────────────────


def _max_retries() -> int:
    """The relay's max_retries threshold. Wrapped so tests can override
    it without reaching into relay internals; production reads the
    same default."""
    return DEFAULT_MAX_RETRIES


# ─── Response models ─────────────────────────────────────────────────


class DlqEntry(BaseModel):
    """One DLQ row. Mirrors OutboxEntry fields with the payload
    JSON-decoded so it renders cleanly in the admin console."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    event_type: str
    report_id: str | None
    retry_count: int
    last_error: str | None
    payload: dict[str, Any]
    created_at: datetime


class DlqListOut(BaseModel):
    entries: list[DlqEntry]
    next_cursor: str | None
    max_retries: int


class DlqPurgeIn(BaseModel):
    """Purge requires a reason so the audit trail records WHY the row
    was skipped. Bare purges (no reason) would let a stuck row
    disappear without ops accountability."""

    reason: str = Field(min_length=3, max_length=500)


# ─── Cursor helpers (keyset pagination on id DESC) ────────────────────


def _encode_cursor(entry: OutboxEntry) -> str:
    return base64.urlsafe_b64encode(json.dumps({"id": entry.id}).encode()).decode()


def _decode_cursor(cursor: str) -> int:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return int(payload["id"])
    except (ValueError, TypeError, KeyError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid cursor: {exc}") from exc


# ─── Common query builder ────────────────────────────────────────────


def _dlq_where() -> ColumnElement[bool]:
    """The condition for "this row is in the DLQ": undelivered AND
    retry-count past the threshold. Kept as a helper so list, get,
    and mutations all agree on what "DLQ" means."""
    return and_(
        OutboxEntry.dispatched_at.is_(None),
        OutboxEntry.retry_count >= _max_retries(),
    )


# ─── GET /api/v1/dlq ─────────────────────────────────────────────────


@router.get(
    "",
    response_model=DlqListOut,
    dependencies=[AdminApiKey],
)
async def list_dlq(
    session: AsyncSession = Depends(_session_dep),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> DlqListOut:
    """Newest-first keyset pagination on `id DESC`. Stable under
    concurrent inserts because the newly-dead rows always land at
    higher ids than the cursor."""
    where = _dlq_where()
    if cursor is not None:
        where = and_(where, OutboxEntry.id < _decode_cursor(cursor))

    # Peek one past `limit` so we know if there's a next page without
    # a follow-up COUNT.
    rows = list(
        (
            await session.scalars(
                select(OutboxEntry).where(where).order_by(OutboxEntry.id.desc()).limit(limit + 1)
            )
        ).all()
    )
    page = rows[:limit]
    next_cursor = _encode_cursor(page[-1]) if len(rows) > limit and page else None
    return DlqListOut(
        entries=[_to_entry(r) for r in page],
        next_cursor=next_cursor,
        max_retries=_max_retries(),
    )


# ─── GET /api/v1/dlq/{outbox_id} ─────────────────────────────────────


@router.get(
    "/{outbox_id}",
    response_model=DlqEntry,
    dependencies=[AdminApiKey],
)
async def get_dlq_entry(
    outbox_id: int,
    session: AsyncSession = Depends(_session_dep),
) -> DlqEntry:
    row = await session.scalar(
        select(OutboxEntry).where(
            and_(OutboxEntry.id == outbox_id, _dlq_where()),
        )
    )
    if row is None:
        # 404 for both "no such row" and "row exists but isn't DLQ"
        # so callers can't infer whether an id exists.
        raise HTTPException(status_code=404, detail="DLQ entry not found")
    return _to_entry(row)


# ─── POST /api/v1/dlq/{outbox_id}/retry ──────────────────────────────


@router.post(
    "/{outbox_id}/retry",
    response_model=DlqEntry,
    dependencies=[AdminApiKey],
)
async def retry_dlq_entry(
    outbox_id: int,
    session: AsyncSession = Depends(_session_dep),
) -> DlqEntry:
    """Reset retry_count to 0. The relay's next poll picks the row up
    naturally — no separate re-dispatch needed."""
    async with session.begin():
        row = await session.scalar(
            select(OutboxEntry).where(
                and_(OutboxEntry.id == outbox_id, _dlq_where()),
            )
        )
        if row is None:
            raise HTTPException(status_code=404, detail="DLQ entry not found or already dispatched")
        row.retry_count = 0
        # Preserve the last error so ops sees the context on the next
        # attempt's row (if it fails again, `last_error` gets overwritten
        # by the fresh failure — normal semantics).
        log.info(
            "dlq.retry",
            outbox_id=row.id,
            event_type=row.event_type,
            report_id=str(row.report_id) if row.report_id else None,
        )
    # Refresh outside the write transaction so the response reflects
    # the committed state.
    async with session.begin():
        refreshed = await session.get(OutboxEntry, outbox_id)
        assert refreshed is not None
        return _to_entry(refreshed)


# ─── POST /api/v1/dlq/{outbox_id}/purge ──────────────────────────────


@router.post(
    "/{outbox_id}/purge",
    response_model=DlqEntry,
    dependencies=[AdminApiKey],
)
async def purge_dlq_entry(
    outbox_id: int,
    body: DlqPurgeIn,
    session: AsyncSession = Depends(_session_dep),
) -> DlqEntry:
    """Mark the row dispatched with a purge note. The row stays for
    audit but the relay never sees it again."""
    async with session.begin():
        row = await session.scalar(
            select(OutboxEntry).where(
                and_(OutboxEntry.id == outbox_id, _dlq_where()),
            )
        )
        if row is None:
            raise HTTPException(status_code=404, detail="DLQ entry not found or already dispatched")
        row.dispatched_at = datetime.now(UTC)
        # Append the purge reason to last_error so both the failure
        # trail AND the human decision are preserved on one row.
        note = f"PURGED: {body.reason.strip()}"
        row.last_error = (f"{row.last_error}\n{note}" if row.last_error else note)[:1000]
        log.warning(
            "dlq.purge",
            outbox_id=row.id,
            event_type=row.event_type,
            report_id=str(row.report_id) if row.report_id else None,
            reason=body.reason,
        )
    async with session.begin():
        refreshed = await session.get(OutboxEntry, outbox_id)
        assert refreshed is not None
        return _to_entry(refreshed)


# ─── Helpers ─────────────────────────────────────────────────────────


def _to_entry(row: OutboxEntry) -> DlqEntry:
    return DlqEntry(
        id=row.id,
        event_type=row.event_type,
        report_id=str(row.report_id) if row.report_id else None,
        retry_count=row.retry_count,
        last_error=row.last_error,
        payload=dict(row.payload) if row.payload else {},
        created_at=row.created_at,
    )


# Include the OR clause helper for future extension (dispatched-but-
# failed variants, deferred). Referenced but not currently used.
_ = or_

__all__ = ["router"]
