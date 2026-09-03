"""Task 7 (§11): flush the accumulated enrichment back to the row.

Only fields that were *explicitly set* on the accumulator overwrite
the row (additive semantics — a task that couldn't run leaves its
field unset and the row keeps whatever it had). This is important
under retry: a partially-successful enrichment shouldn't null out
fields that a previous attempt already populated.

Also appends a `report.enriched` outbox event carrying a FULL report
snapshot (same shape as the submit payload plus enrichment columns),
so downstream dispatchers can react without re-querying:

- SSE consumers can replace the row wholesale in reactive UIs.
- AlertDispatcher re-fires if the enrichment revised severity to
  extreme (or added life_safety), naturally — its filter runs off
  the payload.
- CSV projector INTENTIONALLY skips this event (see
  `csv_projector.py`) — the CSV is at-submit-time only; full-rewrite
  mode with enrichment updates lands with the S3-sync work in P7.

Revised-slot reconciliation is deliberately narrow: only whitelisted
slots (`_REVISABLE_SLOTS`) can be overwritten. The description stays
raw+clean untouchable (that's the redaction contract); location goes
through the geocode task. Confidence-gating happens upstream in
`deep_extract` — if a revision made it into `ctx.result.revised_slots`,
it's cleared for landing."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession as SqlAsyncSession

from fg_voice.enrichment.errors import PermanentEnrichmentError
from fg_voice.enrichment.models import EnrichmentContext
from fg_voice.persistence.models import Report
from fg_voice.persistence.outbox import OutboxEventType
from fg_voice.persistence.outbox import append as append_outbox

# Status transitions.
_STATUS_ENRICHED = "enriched"

# Slots the deep-extract task is permitted to overwrite. Everything
# outside this set is either owned by another task (location → geocode)
# or an invariant (description raw+clean is the redaction contract;
# report_id + short_ref + call_sid + caller_hash are identity).
_REVISABLE_SLOTS: Final[frozenset[str]] = frozenset(
    {
        "hazard_type",
        "severity",
        "water_depth_cm",
    }
)


async def persist(ctx: EnrichmentContext, session: SqlAsyncSession) -> None:
    """UPDATE the row + append the outbox event. Session commit is
    the caller's responsibility (the dispatcher owns the transaction
    boundary so retries are atomic)."""
    row = await session.get(Report, ctx.snapshot.report_id)
    if row is None:
        raise PermanentEnrichmentError(
            f"Report {ctx.snapshot.report_id} disappeared between assemble and persist"
        )
    result = ctx.result
    _apply_revised_slots(row, result.revised_slots, ctx)
    if result.location_resolved is not None:
        row.location_resolved = result.location_resolved
    if result.dedupe_group_id is not None:
        row.dedupe_group_id = result.dedupe_group_id
    if result.confidence_score is not None:
        row.confidence_score = result.confidence_score
    if result.priority_score is not None:
        row.priority_score = result.priority_score
    row.status = _STATUS_ENRICHED
    row.enriched_at = datetime.now(UTC)

    await append_outbox(
        session,
        event_type=OutboxEventType.REPORT_ENRICHED,
        report_id=row.report_id,
        payload=_build_enriched_payload(row, result.notes),
    )


def _apply_revised_slots(
    row: Report,
    revised: dict[str, Any],
    ctx: EnrichmentContext,
) -> None:
    """Apply the deep-extract revisions to whitelisted columns only.
    Anything outside the whitelist is dropped with a note — the
    upstream extractor shouldn't be proposing it, and silently
    accepting it would let the LLM boundary leak into columns it
    doesn't own."""
    for key, value in revised.items():
        if key not in _REVISABLE_SLOTS:
            ctx.result.notes.append(
                f"persist dropped non-revisable slot {key!r} (whitelist: "
                f"{sorted(_REVISABLE_SLOTS)})"
            )
            continue
        setattr(row, key, value)


def _build_enriched_payload(row: Report, notes: list[str]) -> dict[str, Any]:
    """Full-snapshot payload — same shape as the `report.submitted`
    payload plus the enrichment columns. Downstream dispatchers
    (SSE, alerts) can consume it without a DB re-read."""
    return {
        "report_id": str(row.report_id),
        "short_ref": row.short_ref,
        "source": row.source,
        "call_sid": row.call_sid,
        "caller_hash": row.caller_hash,
        "hazard_type": row.hazard_type,
        "severity": row.severity,
        "water_depth_cm": row.water_depth_cm,
        "description": row.description,
        "description_clean": row.description_clean,
        "location_raw": row.location_raw,
        "location_resolved": row.location_resolved,
        "dedupe_group_id": row.dedupe_group_id,
        "confidence_score": row.confidence_score,
        "priority_score": row.priority_score,
        "flags": dict(row.flags) if row.flags else {},
        "status": row.status,
        "enriched_at": row.enriched_at.isoformat() if row.enriched_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "notes": list(notes),
    }


__all__ = ["persist"]
