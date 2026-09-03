"""SqlReportSink — the production implementation of `ReportSink`.

Writes one Report row + one Outbox row in the same transaction so that
either both land or neither does (§2.3). Returns a `SubmittedReport`
with the short_ref the caller will hear.

Lives under `conversation/` (not `persistence/`) for the same reason
`state_store.py` does: the import-linter layered rule puts
`conversation` above `persistence`, so a persistence module can't
depend on the CallState type. Downward deps (into persistence.models,
persistence.outbox, persistence.db) are fine.

Idempotency: keyed on `report_id`, which is minted at call start and
stored on `CallState`. A Twilio retry that re-enters SUBMIT reuses the
same UUID; the unique constraint on `report_id` protects the row —
we look up the existing row and return its short_ref rather than
inserting a duplicate."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as SqlAsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from fg_voice.config import get_settings
from fg_voice.conversation.report_sink import (
    ReportSink,
    SubmittedReport,
    format_short_ref,
)
from fg_voice.conversation.state import CallState, Slot
from fg_voice.persistence.db import get_session_maker
from fg_voice.persistence.models import Report
from fg_voice.persistence.outbox import OutboxEventType
from fg_voice.persistence.outbox import append as append_outbox
from fg_voice.utils.redact import redact_pii
from fg_voice.utils.sampling import should_sample_for_qa


class SqlReportSink(ReportSink):
    """Production sink. One sessionmaker per process; one transaction
    per call. Safe to instantiate per-request."""

    def __init__(self, session_maker: async_sessionmaker[SqlAsyncSession] | None = None) -> None:
        self._sm = session_maker or get_session_maker()

    async def write(self, state: CallState) -> SubmittedReport:
        async with self._sm() as session, session.begin():
            # Idempotency check: if a Report row already exists for
            # this report_id (Twilio retry mid-flow), return the
            # existing short_ref. The caller hears the same reference
            # they would have heard on the first pass.
            existing = await session.get(Report, state.report_id)
            if existing is not None:
                return SubmittedReport(
                    report_id=existing.report_id,
                    short_ref=existing.short_ref,
                    written_at=existing.created_at,
                )

            short_ref = await _mint_short_ref(session, state.report_id)
            row = _build_row(state, short_ref)
            session.add(row)
            await session.flush()

            await append_outbox(
                session,
                event_type=OutboxEventType.REPORT_SUBMITTED,
                payload={
                    "report_id": str(row.report_id),
                    "short_ref": row.short_ref,
                    "source": row.source,
                    "call_sid": row.call_sid,
                    "caller_hash": row.caller_hash,
                    "hazard_type": row.hazard_type,
                    "severity": row.severity,
                    "water_depth_cm": row.water_depth_cm,
                    # `description` is the raw caller utterance (admin
                    # eyes only); `description_clean` is what SSE / CSV
                    # / webhook consumers should surface.
                    "description": row.description,
                    "description_clean": row.description_clean,
                    "location_raw": row.location_raw,
                    "flags": list(state.flags),
                    "created_at": row.created_at.isoformat(),
                },
                report_id=row.report_id,
            )

            return SubmittedReport(
                report_id=row.report_id,
                short_ref=row.short_ref,
                written_at=row.created_at,
            )


def _build_row(state: CallState, short_ref: str) -> Report:
    """Project the CallState onto the Report columns. Slots come from
    the driver's collected values; unset slots stay NULL for P6 to
    fill in (never fabricated here).

    `description_clean` is the PII-scrubbed twin of `description` —
    computed synchronously at write time so every outbound artifact
    (CSV, SSE, webhook alert) has a safe field to consume without
    waiting on an async enrichment step."""
    description_raw = _slot_str(state, Slot.DESCRIPTION)
    return Report(
        report_id=state.report_id,
        short_ref=short_ref,
        source="voice",
        call_sid=state.call_sid,
        caller_hash=state.caller_hash,
        hazard_type=_slot_str(state, Slot.HAZARD_TYPE),
        severity=_slot_str(state, Slot.SEVERITY),
        water_depth_cm=_slot_int(state, Slot.WATER_DEPTH_CM),
        description=description_raw,
        description_clean=redact_pii(description_raw) if description_raw else None,
        location_raw=_slot_str(state, Slot.LOCATION),
        flags={f: True for f in state.flags} if state.flags else None,
        status="pending_enrichment",
        # Deterministic per-report sampling — Twilio retries reusing
        # the same report_id land on the same flag value, so the
        # "5% QA sampling" invariant survives at-least-once delivery.
        sampled_for_qa=should_sample_for_qa(state.report_id, get_settings().qa_sampling_rate),
        created_at=datetime.now(UTC),
    )


def _slot_str(state: CallState, slot: Slot) -> str | None:
    v = state.slots.get(slot)
    if v is None:
        return None
    return str(v.value)


def _slot_int(state: CallState, slot: Slot) -> int | None:
    v = state.slots.get(slot)
    if v is None or not isinstance(v.value, int):
        return None
    return v.value


async def _mint_short_ref(session: SqlAsyncSession, report_id: UUID) -> str:
    """Deterministic seed from report_id; if the base-32 form happens
    to collide with an existing row (astronomically rare in FG-4char
    but let's be honest), tack on a numeric suffix.

    We check with a SELECT rather than relying purely on the unique
    constraint so the retry logic stays inside one transaction — the
    unique-violation retry loop would fight the outer session."""
    candidate = format_short_ref(report_id.int)
    row = await session.scalar(select(Report).where(Report.short_ref == candidate))
    if row is None:
        return candidate
    # Collision — extremely unlikely, but if it happens tack on a bump.
    for bump in range(1, 100):
        alt = f"{candidate}-{bump}"
        row = await session.scalar(select(Report).where(Report.short_ref == alt))
        if row is None:
            return alt
    raise RuntimeError(f"could not mint a unique short_ref for {report_id}")


__all__ = ["SqlReportSink"]
