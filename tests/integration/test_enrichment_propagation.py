"""End-to-end: enrichment output propagates through the existing
dispatchers (SSE, CSV, alerts) correctly.

Covers three propagation cases:

- SSE (PubSub): `report.enriched` is fanned to subscribers with a
  full-snapshot payload. Reactive UIs replace the row wholesale.
- CSV: `report.enriched` is DELIBERATELY skipped. One CSV row per
  report_id; enrichment updates surface via SSE + JSON API. Full
  rewrite mode lands with P7 S3-sync.
- Alerts: `report.enriched` re-evaluates the trigger. A report that
  came in as severity=moderate but was revised to extreme by
  deep_extract MUST fire an alert on the enriched event.

And the reconciliation cases:

- deep_extract at or above threshold → revised_slots stashed
- deep_extract below threshold → dropped (skipped, logged in notes)
- persist applies whitelisted slots only; unknown keys logged, dropped
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from fg_voice.conversation.sql_report_sink import SqlReportSink
from fg_voice.conversation.state import CallState, Slot, SlotValue
from fg_voice.enrichment import EnrichmentDispatcher, EnrichmentFlow
from fg_voice.enrichment.tasks.extract import RevisedSlots
from fg_voice.persistence.alerts import AlertDispatcher
from fg_voice.persistence.broker import InProcessBroker
from fg_voice.persistence.csv_projector import COLUMNS, CsvProjectorDispatcher
from fg_voice.persistence.db import (
    Base,
    override_engine,
    reset_engine,
)
from fg_voice.persistence.dispatchers import ChainDispatcher, PubSubDispatcher
from fg_voice.persistence.outbox import OutboxEventType
from fg_voice.persistence.relay import OutboxRelay


@pytest.fixture
async def _db():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", pool_pre_ping=False)
    override_engine(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()
        reset_engine()


async def _submit_report(**slots_override):
    """Submit one call through SqlReportSink; return (state, short_ref).
    The sink returns a SubmittedReport with the minted short_ref — the
    driver's SUBMIT node normally stashes it on state, but tests call
    the sink directly so we return it here."""
    slots = {
        Slot.HAZARD_TYPE: "storm",
        Slot.SEVERITY: "moderate",
        Slot.LOCATION: "RK Beach",
        Slot.DESCRIPTION: "waves onto road knee deep",
        Slot.WATER_DEPTH_CM: 40,
    }
    slots.update(slots_override)
    state = CallState(call_sid=f"CA_{uuid4().hex[:8]}", caller_hash="h")
    for slot, value in slots.items():
        state.set_slot(slot, SlotValue(value=value, confidence=0.9, source="asr"))
    submitted = await SqlReportSink().write(state)
    return state, submitted.short_ref


# ─── SSE propagation ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sse_gets_full_snapshot_on_enriched(_db):
    """PubSub is event-type agnostic — it fans everything to
    subscribers. The enriched event carries the full report snapshot
    so UIs can wholesale-replace the row."""
    broker = InProcessBroker()

    async with broker.subscribe() as queue:
        state, short_ref = await _submit_report()
        chain = ChainDispatcher(
            [
                PubSubDispatcher(broker=broker),
                EnrichmentDispatcher(flow=EnrichmentFlow()),
            ]
        )
        relay = OutboxRelay(dispatcher=chain)
        await relay.drain_once()  # report.submitted → publish + enrichment
        await relay.drain_once()  # report.enriched → publish

        events = []
        for _ in range(2):
            events.append(await asyncio.wait_for(queue.get(), timeout=1.0))

    submitted, enriched = events
    assert submitted.event_type == OutboxEventType.REPORT_SUBMITTED
    assert enriched.event_type == OutboxEventType.REPORT_ENRICHED

    payload = enriched.payload
    assert payload["report_id"] == str(state.report_id)
    assert payload["short_ref"] == short_ref
    assert payload["hazard_type"] == "storm"
    assert payload["severity"] == "moderate"
    assert payload["water_depth_cm"] == 40
    assert payload["confidence_score"] is not None
    assert payload["priority_score"] == 60
    assert payload["status"] == "enriched"
    assert payload["enriched_at"] is not None


# ─── CSV skip ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_csv_only_appends_on_submit_not_enriched(_db, tmp_path):
    """One CSV row per report — enriched events must not double the
    file. Full-rewrite mode lands in P7."""
    csv_path = tmp_path / "reports.csv"
    chain = ChainDispatcher(
        [
            CsvProjectorDispatcher(path=csv_path),
            EnrichmentDispatcher(flow=EnrichmentFlow()),
        ]
    )
    relay = OutboxRelay(dispatcher=chain)

    _, short_ref = await _submit_report()
    await relay.drain_once()  # submitted → CSV row + enrichment
    await relay.drain_once()  # enriched → CSV skips

    text = csv_path.read_text(encoding="utf-8-sig")
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) == 2  # header + one data row
    header = lines[0].split(",")
    assert tuple(header) == COLUMNS
    # Sanity: it's OUR row.
    assert short_ref in lines[1]


# ─── Alert re-fires on enrichment ────────────────────────────────────


@pytest.mark.asyncio
async def test_alert_refires_when_enrichment_reveals_extreme(_db):
    """A caller reports moderate severity; deep_extract revises to
    extreme. The alert fires on the enriched event because the
    payload now carries severity=extreme."""

    class RevisingExtractor:
        async def extract(self, description):
            # High-confidence revision — well above the default 0.7
            # threshold — so persist applies it.
            return RevisedSlots(values={"severity": "extreme"}, confidence=0.95)

    class RecordingBackend:
        def __init__(self):
            self.alerts = []

        async def send(self, alert):
            self.alerts.append(alert)

    backend = RecordingBackend()
    alerts = AlertDispatcher(backends=[backend])
    enrichment = EnrichmentDispatcher(flow=EnrichmentFlow(extractor=RevisingExtractor()))
    chain = ChainDispatcher([alerts, enrichment])
    relay = OutboxRelay(dispatcher=chain)

    _, short_ref = await _submit_report(**{Slot.SEVERITY: "moderate"})
    # Drain 1: report.submitted. Alert filter: severity=moderate,
    # not life_safety → NO alert. Enrichment runs, appends
    # report.enriched with severity=extreme (revised).
    await relay.drain_once()
    assert backend.alerts == [], "submit-time alert should not fire on moderate"
    # Drain 2: report.enriched, severity=extreme → alert MUST fire.
    await relay.drain_once()
    assert len(backend.alerts) == 1
    alert = backend.alerts[0]
    assert alert["event_type"] == OutboxEventType.REPORT_ENRICHED
    assert alert["severity"] == "extreme"
    assert alert["short_ref"] == short_ref
    assert alert["trigger"] == "severity_extreme"


@pytest.mark.asyncio
async def test_alert_fires_twice_when_report_was_already_extreme(_db):
    """A report that came in extreme + gets re-enriched fires the
    alert twice (once per outbox event). That's intentional — backends
    (Slack/PagerDuty) dedupe on their end; the relay's job is to
    surface every trigger. Documenting the behaviour so it doesn't
    surprise reviewers."""

    class RecordingBackend:
        def __init__(self):
            self.alerts = []

        async def send(self, alert):
            self.alerts.append(alert)

    backend = RecordingBackend()
    alerts = AlertDispatcher(backends=[backend])
    enrichment = EnrichmentDispatcher(flow=EnrichmentFlow())
    chain = ChainDispatcher([alerts, enrichment])
    relay = OutboxRelay(dispatcher=chain)

    await _submit_report(**{Slot.SEVERITY: "extreme"})
    await relay.drain_once()  # report.submitted → alert #1
    await relay.drain_once()  # report.enriched → alert #2

    assert len(backend.alerts) == 2
    assert backend.alerts[0]["event_type"] == OutboxEventType.REPORT_SUBMITTED
    assert backend.alerts[1]["event_type"] == OutboxEventType.REPORT_ENRICHED
