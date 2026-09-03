"""Task 4 (§11): resolve `location_raw` to a canonical place.

Real implementation needs the gazetteer index (P4) plus an external
geocoder (Nominatim / Google) plus PostGIS coastline snap for marine
hazards. All three land in later phases; here we define the boundary
and ship a No-Op default.

The `Geocoder` Protocol takes a raw location string and returns
either a resolved string or None (couldn't confidently resolve).
A real implementation would return a richer object with lat/lng and
a confidence — v1 keeps the return shape simple so the DB schema
doesn't need lat/lng columns yet (adds in P4 when the RAG index and
PostGIS extension are wired)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fg_voice.enrichment.models import EnrichmentContext


class Geocoder(Protocol):
    """Resolve a raw caller location to a canonical place name."""

    async def resolve(self, raw: str) -> str | None: ...


@dataclass(slots=True)
class NoOpGeocoder:
    """Default. Returns None for everything — resolved location stays
    NULL on the row. Ships so the flow runs without RAG configured;
    swapped for `rag.gazetteer.GazetteerGeocoder` in P4."""

    async def resolve(self, raw: str) -> str | None:
        return None


async def geocode(ctx: EnrichmentContext, *, geocoder: Geocoder) -> None:
    """Resolve the raw location and stash the canonical name on the
    accumulator. Missing raw location → nothing to do."""
    raw = ctx.snapshot.location_raw
    if not raw:
        return
    resolved = await geocoder.resolve(raw)
    if resolved is not None:
        ctx.result.location_resolved = resolved


__all__ = ["Geocoder", "NoOpGeocoder", "geocode"]
