"""Task 1 (§11): assemble the immutable snapshot the rest of the flow
reads from.

Simplest possible: load the Report row by id and shape it into a
`ReportSnapshot`. Deliberately does not:

- fetch the CallState from Redis (that data is already projected onto
  the Report row by SqlReportSink; the CallState is a short-lived
  in-memory object we don't need after the call ends)
- fetch the recording (P6-deep only — the MVP flow doesn't consume
  audio; the recording lives in Twilio's storage keyed on call_sid
  and can be pulled on demand by the QA console)
- fetch the transcript (§18.4 will add this once Deepgram / batch STT
  is wired; for the MVP the transcript is what `description` and the
  slot values already capture)"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession as SqlAsyncSession

from fg_voice.enrichment.errors import PermanentEnrichmentError
from fg_voice.enrichment.models import ReportSnapshot
from fg_voice.persistence.models import Report


async def assemble(session: SqlAsyncSession, report_id: UUID) -> ReportSnapshot:
    """Load the Report row and return an immutable snapshot. Raises
    `PermanentEnrichmentError` if the row disappeared (deleted between
    the outbox event and the enrichment dispatch — should never happen
    under normal ops)."""
    row = await session.get(Report, report_id)
    if row is None:
        raise PermanentEnrichmentError(f"Report {report_id} not found; nothing to enrich")
    return ReportSnapshot(
        report_id=row.report_id,
        short_ref=row.short_ref,
        source=row.source,
        call_sid=row.call_sid,
        caller_hash=row.caller_hash,
        hazard_type=row.hazard_type,
        severity=row.severity,
        water_depth_cm=row.water_depth_cm,
        description=row.description,
        description_clean=row.description_clean,
        location_raw=row.location_raw,
        flags=dict(row.flags) if row.flags else {},
        created_at=row.created_at,
    )


__all__ = ["assemble"]
