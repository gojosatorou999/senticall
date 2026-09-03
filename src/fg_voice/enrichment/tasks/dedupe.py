"""Task 5 (§11): group near-duplicate reports.

Full spec: candidates = same hazard_class ∧ ST_DWithin(geom, 2 km)
∧ within 3 h, then cosine(description) > 0.82 ⇒ same group. That
needs PostGIS + embeddings + the resolved location — none of which
exist yet.

What we ship here:

- `DedupeStrategy` Protocol — the boundary. Takes an EnrichmentContext
  + a session (so a real impl can query recent reports). Returns a
  group id, or None (this report starts a new group / no grouping).
- `NoDedupeStrategy` default — every report is its own group. Ships
  so the flow runs without PostGIS.

When a real strategy lands, it plugs in at wire-time in `main.py`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession as SqlAsyncSession

from fg_voice.enrichment.models import EnrichmentContext


class DedupeStrategy(Protocol):
    """Compute (or lookup) the dedupe group this report belongs to."""

    async def group_for(self, ctx: EnrichmentContext, session: SqlAsyncSession) -> str | None: ...


@dataclass(slots=True)
class NoDedupeStrategy:
    """Default. Doesn't group anything — leaves `dedupe_group_id`
    NULL. Real deploy swaps for `SpatioTemporalTextDedupe` when
    PostGIS + the embedding model are wired."""

    async def group_for(self, ctx: EnrichmentContext, session: SqlAsyncSession) -> str | None:
        return None


async def dedupe(
    ctx: EnrichmentContext,
    session: SqlAsyncSession,
    *,
    strategy: DedupeStrategy,
) -> None:
    """Ask the strategy for a group id and stash it on the accumulator."""
    group_id = await strategy.group_for(ctx, session)
    if group_id is not None:
        ctx.result.dedupe_group_id = group_id


__all__ = ["DedupeStrategy", "NoDedupeStrategy", "dedupe"]
