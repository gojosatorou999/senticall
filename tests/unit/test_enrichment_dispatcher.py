"""EnrichmentDispatcher — the relay adapter for the enrichment flow.

Covers:
- non-`report.submitted` events are ignored
- `report.submitted` triggers the flow
- malformed report_id raises DispatchError (relay bumps retry_count)
- missing report_id logs + returns (unrecoverable but not raising —
  matches the rest of the dispatcher family's tolerance for missing keys)
- EnrichmentError propagates as DispatchError so the relay retries
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from fg_voice.enrichment.dispatcher import EnrichmentDispatcher
from fg_voice.enrichment.errors import TransientEnrichmentError
from fg_voice.persistence.models import OutboxEntry
from fg_voice.persistence.outbox import OutboxEventType
from fg_voice.persistence.relay import DispatchError


class _RecordingFlow:
    """Test double that records every call to `run` and can be flipped
    to raise. Matches the shape of EnrichmentFlow's public surface."""

    def __init__(self, raise_next: bool = False):
        self.ran_for: list[UUID] = []
        self.raise_next = raise_next

    async def run(self, report_id: UUID):
        self.ran_for.append(report_id)
        if self.raise_next:
            self.raise_next = False
            raise TransientEnrichmentError("nope")
        return None


def _entry(event_type: str, payload: dict) -> OutboxEntry:
    """Build a bare OutboxEntry (no session, no id) suitable for
    dispatcher-level tests."""
    entry = OutboxEntry(event_type=event_type, payload=payload)
    return entry


@pytest.mark.asyncio
async def test_ignores_non_submitted_events():
    flow = _RecordingFlow()
    dispatcher = EnrichmentDispatcher(flow=flow)  # type: ignore[arg-type]
    entry = _entry(OutboxEventType.REPORT_ENRICHED, {"report_id": str(uuid4())})
    await dispatcher.dispatch(entry)
    assert flow.ran_for == []


@pytest.mark.asyncio
async def test_runs_flow_on_report_submitted():
    flow = _RecordingFlow()
    dispatcher = EnrichmentDispatcher(flow=flow)  # type: ignore[arg-type]
    rid = uuid4()
    entry = _entry(OutboxEventType.REPORT_SUBMITTED, {"report_id": str(rid)})
    await dispatcher.dispatch(entry)
    assert flow.ran_for == [rid]


@pytest.mark.asyncio
async def test_malformed_report_id_raises_dispatch_error():
    flow = _RecordingFlow()
    dispatcher = EnrichmentDispatcher(flow=flow)  # type: ignore[arg-type]
    entry = _entry(OutboxEventType.REPORT_SUBMITTED, {"report_id": "not-a-uuid"})
    with pytest.raises(DispatchError, match="invalid report_id"):
        await dispatcher.dispatch(entry)
    assert flow.ran_for == []


@pytest.mark.asyncio
async def test_missing_report_id_returns_without_running():
    flow = _RecordingFlow()
    dispatcher = EnrichmentDispatcher(flow=flow)  # type: ignore[arg-type]
    entry = _entry(OutboxEventType.REPORT_SUBMITTED, {})
    await dispatcher.dispatch(entry)  # must not raise
    assert flow.ran_for == []


@pytest.mark.asyncio
async def test_enrichment_error_bubbles_as_dispatch_error():
    """Enrichment failures must surface as DispatchError so the
    relay's retry-count machinery kicks in — the caller has already
    heard success; enrichment is best-effort background work."""
    flow = _RecordingFlow(raise_next=True)
    dispatcher = EnrichmentDispatcher(flow=flow)  # type: ignore[arg-type]
    entry = _entry(OutboxEventType.REPORT_SUBMITTED, {"report_id": str(uuid4())})
    with pytest.raises(DispatchError, match="enrichment failed"):
        await dispatcher.dispatch(entry)
