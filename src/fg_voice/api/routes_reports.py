"""Reports feed for admin + Flutter clients (§13.1).

Two shapes here:

- Live: `GET /api/v1/reports/stream` — Server-Sent Events. Every
  outbox row that lands via the relay's `PubSubDispatcher` fans out
  to every open SSE subscriber as one `data:` frame.
- Read: `GET /api/v1/reports/{short_ref}` and
  `GET /api/v1/reports?...` — the endpoints the alert payloads point
  at ("call this to rehydrate the full report"). Both return JSON.

We DON'T write our own SSE library — just the small subset we need
(`data: {json}\\n\\n` + periodic `:keepalive\\n\\n` comments to stop
proxies dropping the connection)."""

from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fg_voice.api.auth import AdminApiKey
from fg_voice.config import get_settings
from fg_voice.obs.logging import get_logger
from fg_voice.persistence.broker import InProcessBroker, ReportEvent, SubscriberLagged
from fg_voice.persistence.csv_projector import BOM, COLUMNS, row_from_report
from fg_voice.persistence.db import get_session_maker
from fg_voice.persistence.models import Report

log = get_logger(__name__)
router = APIRouter(prefix="/api/v1", tags=["reports"])

# ─── SSE broker singleton ────────────────────────────────────────────

_broker_singleton: InProcessBroker | None = None
KEEPALIVE_INTERVAL_SEC: Final[float] = 15.0


def set_broker(broker: InProcessBroker | None) -> None:
    global _broker_singleton
    _broker_singleton = broker


def _broker_provider() -> InProcessBroker | None:
    return _broker_singleton


# ─── DB session dependency ───────────────────────────────────────────


async def _session_dep() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency — yields one session per request. Tests
    override the module-level `_get_session_maker` to inject an
    in-memory SQLite maker without touching env."""
    sm = _get_session_maker()
    async with sm() as session:
        yield session


def _get_session_maker() -> async_sessionmaker[AsyncSession]:
    """Wrapped so tests can monkeypatch this attribute rather than
    reaching into `persistence.db`'s module state."""
    return get_session_maker()


# ─── Response models ─────────────────────────────────────────────────


class ReportOut(BaseModel):
    """The JSON shape returned by both /reports/{short_ref} and each
    item in the list response. Kept flat + string-first so the Flutter
    app + admin dashboard don't have to interpret nested types."""

    model_config = ConfigDict(from_attributes=True)

    report_id: UUID
    short_ref: str
    source: str
    call_sid: str
    caller_hash: str
    hazard_type: str | None
    severity: str | None
    water_depth_cm: int | None
    description: str | None
    description_clean: str | None
    location_raw: str | None
    location_resolved: str | None
    dedupe_group_id: str | None
    priority_score: int | None
    flags: dict[str, Any] | None
    status: str
    # QA sampling — surfaced so the admin dashboard can show the
    # queue depth + reviewed state without a separate endpoint.
    sampled_for_qa: bool
    qa_reviewed_at: datetime | None
    qa_notes: str | None
    created_at: datetime
    updated_at: datetime


class ReportListOut(BaseModel):
    items: list[ReportOut]
    # Opaque cursor for the next page. `None` when this is the last
    # page. Clients pass it back verbatim as `?cursor=...`.
    next_cursor: str | None


# ─── Endpoints ───────────────────────────────────────────────────────


@router.get("/reports/stream", dependencies=[AdminApiKey])
async def stream_reports(request: Request) -> Response:
    """Long-lived SSE connection. Closes cleanly when the client
    disconnects."""
    broker = _broker_provider()
    if broker is None:
        return Response(status_code=503, content="reports stream not available")
    return StreamingResponse(
        _event_stream(request, broker),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/reports/export.csv", dependencies=[AdminApiKey])
async def export_reports_csv(
    session: AsyncSession = Depends(_session_dep),
    source: str | None = Query(None, max_length=16),
    hazard_type: str | None = Query(None, max_length=32),
    severity: str | None = Query(None, max_length=16),
    life_safety: bool | None = Query(None),
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
) -> Response:
    """Streams a filtered report set as CSV (§12.3). Uses the same
    filters as `/reports`, no pagination — the caller gets everything
    matching their query, one row per report.

    Reuses `row_from_report` from the projector module so a batch
    export renders identical CSV to what the live projector wrote at
    ingestion. If the two ever drift, the row-parity test catches it.

    Not paginated because ops workflows typically want the full slice
    (a district officer downloading yesterday's tide reports). If a
    query returns 100k+ rows, it should already have `from/to` bounds
    — and this is a stream, so memory stays flat regardless of size."""
    agent_version = get_settings().fg_agent_version
    stmt = select(Report).order_by(Report.created_at.desc(), Report.report_id.desc())
    if source:
        stmt = stmt.where(Report.source == source)
    if hazard_type:
        stmt = stmt.where(Report.hazard_type == hazard_type)
    if severity:
        stmt = stmt.where(Report.severity == severity)
    if from_:
        stmt = stmt.where(Report.created_at >= from_)
    if to:
        stmt = stmt.where(Report.created_at <= to)

    async def _iter_csv() -> AsyncIterator[bytes]:
        # Header first, then rows one at a time. `csv.DictWriter` runs
        # against a fresh StringIO per chunk so we never buffer the
        # whole file.
        yield BOM
        yield _csv_line(dict(zip(COLUMNS, COLUMNS, strict=True)))  # header row

        result = await session.stream_scalars(stmt)
        async for report in result:
            # Same post-filter as /reports for the JSON-flag column;
            # streamed rows honour it too.
            if life_safety is not None and _has_life_safety(report.flags) is not life_safety:
                continue
            yield _csv_line(row_from_report(report, agent_version=agent_version))

    headers = {
        "Content-Disposition": 'attachment; filename="reports.csv"',
        "Cache-Control": "no-cache",
    }
    return StreamingResponse(_iter_csv(), media_type="text/csv; charset=utf-8", headers=headers)


def _csv_line(row: dict[str, str]) -> bytes:
    """Serialise one row (or the header dict) as a single CSV line
    with `\\n` terminator + UTF-8 encoding. Kept small — the whole
    export is O(rows) memory, not O(file)."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    writer.writerow(row)
    return buf.getvalue().encode("utf-8")


@router.get("/reports/{short_ref}", response_model=ReportOut, dependencies=[AdminApiKey])
async def get_report(
    short_ref: str,
    session: AsyncSession = Depends(_session_dep),
) -> Report:
    """Rehydrate a single report by its FG-XXXX short_ref. This is
    what the alert payload's `short_ref` field points ops to."""
    row = await session.scalar(select(Report).where(Report.short_ref == short_ref))
    if row is None:
        raise HTTPException(status_code=404, detail=f"report {short_ref!r} not found")
    return row


@router.get("/reports", response_model=ReportListOut, dependencies=[AdminApiKey])
async def list_reports(
    session: AsyncSession = Depends(_session_dep),
    source: str | None = Query(None, max_length=16),
    hazard_type: str | None = Query(None, max_length=32),
    severity: str | None = Query(None, max_length=16),
    life_safety: bool | None = Query(None, description="Filter to life-safety-flagged only"),
    qa_sample: bool | None = Query(
        None,
        description="Filter to QA-sampled reports only (true) or non-sampled (false)",
    ),
    qa_reviewed: bool | None = Query(
        None,
        description=(
            "Filter to reviewed (true) / unreviewed (false) QA samples. "
            "Combine with qa_sample=true for 'unreviewed queue' shape."
        ),
    ),
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    cursor: str | None = Query(None, max_length=128),
    limit: int = Query(50, ge=1, le=200),
) -> ReportListOut:
    """Paginated list, newest first. Cursor is opaque + keyset-based
    so pages stay stable under concurrent inserts."""
    stmt = (
        select(Report)
        .order_by(Report.created_at.desc(), Report.report_id.desc())
        .limit(limit + 1)  # one extra so we know if there's a next page
    )
    if source:
        stmt = stmt.where(Report.source == source)
    if hazard_type:
        stmt = stmt.where(Report.hazard_type == hazard_type)
    if severity:
        stmt = stmt.where(Report.severity == severity)
    if from_:
        stmt = stmt.where(Report.created_at >= from_)
    if to:
        stmt = stmt.where(Report.created_at <= to)
    if qa_sample is not None:
        stmt = stmt.where(Report.sampled_for_qa.is_(qa_sample))
    if qa_reviewed is not None:
        stmt = (
            stmt.where(Report.qa_reviewed_at.is_not(None))
            if qa_reviewed
            else stmt.where(Report.qa_reviewed_at.is_(None))
        )
    if cursor:
        try:
            cursor_ts, cursor_uuid = _decode_cursor(cursor)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid cursor: {exc}") from exc
        # Keyset: rows strictly older than the cursor's (created_at, report_id).
        # Ties on created_at are broken by report_id descending so no row is
        # ever skipped or repeated across pages.
        stmt = stmt.where(
            or_(
                Report.created_at < cursor_ts,
                and_(Report.created_at == cursor_ts, Report.report_id < cursor_uuid),
            )
        )

    rows = list((await session.scalars(stmt)).all())

    # `flags` is JSON — cross-dialect predicates for a JSON-key lookup
    # are messy (Postgres has `->>`, SQLite has `json_extract`). We
    # post-filter in Python; fine for admin/UI listings, revisit if
    # this becomes a hot path.
    if life_safety is not None:
        rows = [r for r in rows if _has_life_safety(r.flags) is life_safety]

    if len(rows) > limit:
        page = rows[:limit]
        # Cursor points at the LAST row of the current page — page N+1's
        # `created_at < cursor` predicate then correctly starts at the
        # row we peeked. Encoding the peek row instead skips it.
        anchor = page[-1]
        next_cursor: str | None = _encode_cursor(anchor.created_at, anchor.report_id)
    else:
        page = rows
        next_cursor = None

    return ReportListOut(
        items=[ReportOut.model_validate(r) for r in page],
        next_cursor=next_cursor,
    )


class QaReviewIn(BaseModel):
    """Body for POST /reports/{short_ref}/qa_review. Notes required so
    the review is auditable — ops can't sign off silently, matches
    the DLQ purge pattern."""

    notes: str = Field(min_length=3, max_length=1000)


@router.post(
    "/reports/{short_ref}/qa_review",
    response_model=ReportOut,
    dependencies=[AdminApiKey],
)
async def review_qa_sample(
    short_ref: str,
    body: QaReviewIn,
    session: AsyncSession = Depends(_session_dep),
) -> Report:
    """Mark a QA-sampled report as reviewed. Idempotent by design —
    a second review overwrites the notes + timestamp so ops can
    correct a rushed first pass. Not-sampled reports return 400
    (there's no queue slot to close)."""
    async with session.begin():
        row = await session.scalar(select(Report).where(Report.short_ref == short_ref))
        if row is None:
            raise HTTPException(status_code=404, detail=f"report {short_ref!r} not found")
        if not row.sampled_for_qa:
            raise HTTPException(
                status_code=400,
                detail=f"report {short_ref!r} was not sampled for QA",
            )
        row.qa_reviewed_at = datetime.now(UTC)
        row.qa_notes = body.notes.strip()
        log.info(
            "qa.reviewed",
            short_ref=short_ref,
            report_id=str(row.report_id),
        )
    # Re-read outside the write tx so the response reflects committed state.
    async with session.begin():
        refreshed = await session.scalar(select(Report).where(Report.short_ref == short_ref))
        assert refreshed is not None
        return refreshed


# ─── SSE plumbing (unchanged from previous commit) ───────────────────


async def _event_stream(request: Request, broker: InProcessBroker) -> AsyncIterator[bytes]:
    async with broker.subscribe() as queue:
        log.info("reports.stream.subscribed", subscribers=broker.subscriber_count)
        yield _sse_comment("connected")
        try:
            while True:
                if await request.is_disconnected():
                    log.info("reports.stream.client_disconnected")
                    return
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_INTERVAL_SEC)
                except TimeoutError:
                    yield _sse_comment("keepalive")
                    continue
                if isinstance(item, SubscriberLagged):
                    yield _sse_event(
                        "lagged",
                        {"note": "subscriber fell behind; some events were dropped"},
                    )
                    continue
                yield _sse_event(item.event_type, item.payload)
        finally:
            log.info(
                "reports.stream.unsubscribed",
                subscribers=max(0, broker.subscriber_count - 1),
            )


def _sse_event(event_type: str, payload: dict[str, object]) -> bytes:
    body = json.dumps(payload, separators=(",", ":"), default=str)
    return f"event: {event_type}\ndata: {body}\n\n".encode()


def _sse_comment(text: str) -> bytes:
    return f": {text}\n\n".encode()


# ─── Cursor helpers ──────────────────────────────────────────────────


def _encode_cursor(created_at: datetime, report_id: UUID) -> str:
    """Opaque base64 blob. Clients treat it as write-only."""
    raw = f"{created_at.isoformat()}|{report_id.hex}"
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("not base64") from exc
    parts = raw.split("|", 1)
    if len(parts) != 2:
        raise ValueError("cursor missing delimiter")
    ts_str, uuid_hex = parts
    try:
        return datetime.fromisoformat(ts_str), UUID(hex=uuid_hex)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


def _has_life_safety(flags: dict[str, Any] | None) -> bool:
    if not flags:
        return False
    return bool(flags.get("life_safety"))


__all__ = [
    "KEEPALIVE_INTERVAL_SEC",
    "ReportListOut",
    "ReportOut",
    "_broker_provider",
    "_event_stream",
    "_get_session_maker",
    "_sse_comment",
    "_sse_event",
    "get_report",
    "list_reports",
    "router",
    "set_broker",
    "stream_reports",
]

# Warm the ReportEvent import for the SSE `data:` documentation.
_ = ReportEvent
