"""P6 enrichment orchestrator.

Composes the tasks in `enrichment/tasks/` into a linear flow. Not
Prefect — the outbox already gives us retries + idempotency at the
transport layer, and the flow itself is simple enough that a plain
async function is more legible than a workflow DSL. When P7 needs
multi-worker scheduling, wrap `run()` in a Prefect flow decorator;
the task boundaries here are already the natural task boundaries.

Ordering:

    assemble → deep_extract → geocode → dedupe → score → persist

Rationale:
- assemble first (nothing else can run without the snapshot)
- deep_extract before score (revisions feed the confidence heuristic)
- geocode before dedupe (dedupe wants the resolved location once P4
  RAG lands; today NoDedupeStrategy makes this ordering moot but keeps
  the composition future-proof)
- persist last (single write per flow run)

Idempotency: each task is either pure or keyed on `report_id`. Persist
overwrites the row's enrichment fields with the current accumulator,
so re-running the flow just recomputes and re-writes — identical
result if inputs unchanged."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession as SqlAsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from fg_voice.enrichment.models import EnrichmentContext
from fg_voice.enrichment.tasks.assemble import assemble
from fg_voice.enrichment.tasks.dedupe import DedupeStrategy, NoDedupeStrategy, dedupe
from fg_voice.enrichment.tasks.extract import LLMExtractor, NoOpExtractor, deep_extract
from fg_voice.enrichment.tasks.geocode import Geocoder, NoOpGeocoder, geocode
from fg_voice.enrichment.tasks.persist import persist
from fg_voice.enrichment.tasks.score import score
from fg_voice.obs.logging import get_logger
from fg_voice.persistence.db import get_session_maker

log = get_logger(__name__)


@dataclass(slots=True)
class EnrichmentFlow:
    """Composed pipeline of the six enrichment tasks. All boundaries
    are injected — the defaults are No-Op implementations so the flow
    runs end-to-end without an LLM, geocoder, or dedupe backend
    configured. Real deploys inject real impls in `main.py`."""

    extractor: LLMExtractor = field(default_factory=NoOpExtractor)
    geocoder: Geocoder = field(default_factory=NoOpGeocoder)
    dedupe_strategy: DedupeStrategy = field(default_factory=NoDedupeStrategy)
    session_maker: async_sessionmaker[SqlAsyncSession] | None = None

    async def run(self, report_id: UUID) -> EnrichmentContext:
        """Run the flow for one report and return the final context
        (mostly useful in tests + the QA console)."""
        sm = self.session_maker or get_session_maker()
        async with sm() as session, session.begin():
            ctx = await self._run_in_session(session, report_id)
        log.info(
            "enrichment.completed",
            report_id=str(report_id),
            confidence=ctx.result.confidence_score,
            priority=ctx.result.priority_score,
            resolved=bool(ctx.result.location_resolved),
            grouped=bool(ctx.result.dedupe_group_id),
            notes=len(ctx.result.notes),
        )
        return ctx

    async def _run_in_session(self, session: SqlAsyncSession, report_id: UUID) -> EnrichmentContext:
        snapshot = await assemble(session, report_id)
        ctx = EnrichmentContext(snapshot=snapshot)
        await deep_extract(ctx, extractor=self.extractor)
        await geocode(ctx, geocoder=self.geocoder)
        await dedupe(ctx, session, strategy=self.dedupe_strategy)
        await score(ctx)
        await persist(ctx, session)
        return ctx


__all__ = ["EnrichmentFlow"]
