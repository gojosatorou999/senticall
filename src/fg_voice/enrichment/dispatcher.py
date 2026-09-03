"""Relay adapter for the enrichment flow.

The outbox relay drains one row at a time and hands it to a
`Dispatcher` (see `persistence/relay.py`). This module implements
that Protocol by:

- Filtering to `report.submitted` events (everything else is a no-op)
- Extracting the `report_id` from the payload
- Running the `EnrichmentFlow` for that report

The flow owns its own transaction (see `flow.py::run`), so this
dispatcher just needs to await the run and let any error propagate to
the relay's retry machinery.

Wire order in `main.py`: the chain is `[PubSub, Csv?, Alerts?,
Enrichment?]` — enrichment runs LAST so a slow LLM call can't delay
the fast-path SSE / CSV / alert side-effects. This is the opposite of
the naive ordering (run enrichment first, fan out enriched data), but
the P5 write-time projection already gives operators the raw
report immediately; enrichment updates the row and re-fans as a
separate outbox event."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fg_voice.enrichment.errors import EnrichmentError
from fg_voice.enrichment.flow import EnrichmentFlow
from fg_voice.obs.logging import get_logger
from fg_voice.persistence.models import OutboxEntry
from fg_voice.persistence.outbox import OutboxEventType
from fg_voice.persistence.relay import DispatchError

log = get_logger(__name__)


@dataclass(slots=True)
class EnrichmentDispatcher:
    """Runs the enrichment flow for `report.submitted` events."""

    flow: EnrichmentFlow

    async def dispatch(self, entry: OutboxEntry) -> None:
        if entry.event_type != OutboxEventType.REPORT_SUBMITTED:
            return
        report_id_raw = entry.payload.get("report_id")
        if not report_id_raw:
            log.warning(
                "enrichment.dispatch.missing_report_id",
                outbox_id=entry.id,
                event_type=entry.event_type,
            )
            return
        try:
            report_id = UUID(report_id_raw)
        except (ValueError, TypeError) as exc:
            # Deliberately raise DispatchError, not EnrichmentError —
            # a malformed payload isn't going to fix itself on retry,
            # but the relay's uniform retry-count / dead-letter ladder
            # will eventually park it in the DLQ.
            raise DispatchError(f"invalid report_id {report_id_raw!r}: {exc}") from exc
        try:
            await self.flow.run(report_id)
        except EnrichmentError as exc:
            # Bubble up as DispatchError so the relay bumps retry_count
            # and re-attempts on the next poll.
            raise DispatchError(f"enrichment failed for {report_id}: {exc}") from exc


__all__ = ["EnrichmentDispatcher"]
