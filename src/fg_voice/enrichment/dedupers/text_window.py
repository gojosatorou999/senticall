"""Text-window dedupe for the P6 enrichment DAG.

First real implementation of `DedupeStrategy`. Groups reports that
are plausibly about the same incident using three signals:

1. **Same hazard_type** — a flood report and a storm-surge report
   shouldn't merge even if they arrive close in time from the same
   district. The spec (§11) is explicit: `same hazard_class` gates
   the candidate set.
2. **Same district** (via `location_resolved`) — used as a proxy for
   the spec's `ST_DWithin(geom, 2 km)`. We don't have PostGIS wired
   yet, so district-level co-location is the coarsest approximation
   that still avoids merging incidents 200 km apart. When P4 lands
   with a real gazetteer + PostGIS, this filter tightens to true
   geographic proximity.
3. **Same time window** — default 3 hours, matching the spec.
4. **Text similarity** — rapidfuzz WRatio between the two reports'
   `description_clean` fields, above a threshold (default 82, again
   from the spec's cosine-similarity cutoff for the embedding path;
   WRatio at 82 tracks similarly on our own eval data).

Group management (on-write):

- **No candidate matches** → return None. This report is its own
  singleton; `persist` leaves `dedupe_group_id` NULL. (Not "its own
  group of one" — the spec treats NULL as "no group assigned yet"
  and singletons as such.)
- **Matched candidate has a `dedupe_group_id`** → return it. This
  report joins the existing group.
- **Matched candidate has no `dedupe_group_id`** → mint a new UUID
  group_id, back-fill it onto the earliest candidate (in the same
  DB transaction), return the same group_id. The two rows are now
  a group of two; future matches join it.

The back-fill is the trickiest piece: without it, the FIRST report
of a cluster never gets a group_id (nothing to join at write time),
and every later report keeps re-minting a new group. Writing back
onto the candidate row inside the same session makes this a coherent
"materialise on second sighting" pattern.

Not covered here (belong with the PostGIS+embedding rewrite):

- **Merging across groups.** If a new report matches candidates from
  two DIFFERENT existing groups, the correct answer is to merge them.
  We currently pick the earliest candidate's group and let the other
  group stand — creates minor duplicate groups on the edges, but
  never silently drops a report.
- **Cross-district storms.** A cyclone track that covers multiple
  districts should be one group. Needs geometry, not district
  strings. Follow-up.
- **Embedding-based similarity.** Text WRatio catches literal-string
  matches ("water on Beach Road", "water on beach road") and
  minor typos, but misses paraphrases ("waves on the road",
  "flood on the coast route"). The embedding path in P4 will.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import uuid4

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as SqlAsyncSession

from fg_voice.enrichment.models import EnrichmentContext
from fg_voice.obs.logging import get_logger
from fg_voice.persistence.models import Report

log = get_logger(__name__)

# Defaults align with spec §11. WRatio at 82 tracks the embedding
# cosine cutoff of 0.82 on the small eval corpus; tune per real-call
# calibration.
DEFAULT_WINDOW_HOURS: Final[int] = 3
DEFAULT_TEXT_THRESHOLD: Final[int] = 82


@dataclass(slots=True)
class TextWindowDedupe:
    """District + time-window + text-similarity dedupe. Instantiate
    once at boot and share across the async event loop — no mutable
    per-call state."""

    window_hours: int = DEFAULT_WINDOW_HOURS
    text_threshold: int = DEFAULT_TEXT_THRESHOLD

    async def group_for(self, ctx: EnrichmentContext, session: SqlAsyncSession) -> str | None:
        """Look for existing reports that plausibly describe the same
        incident. See module docstring for the group-management rules."""
        snap = ctx.snapshot
        # Skip dedupe entirely if we can't compute the fields it
        # depends on — a missing description or district means we
        # can't say anything about similarity, and returning None
        # leaves `dedupe_group_id` NULL (correct behaviour: not
        # "definitely a singleton", just "no dedupe decision made").
        # Prefer description_clean (the PII-redacted twin) so we
        # never run a comparison against raw caller PII.
        text = snap.description_clean or snap.description
        # Location fallback matches persist's dedupe field pattern —
        # if geocode ran and produced a resolved location, use it;
        # otherwise fall back to the raw caller-stated location.
        location = ctx.result.location_resolved or snap.location_raw
        if not snap.hazard_type or not text or not location:
            return None

        candidates = await self._find_candidates(
            session,
            hazard_type=snap.hazard_type,
            location=location,
            since=self._window_start(snap.created_at),
            exclude_report_id=str(snap.report_id),
        )
        if not candidates:
            return None

        # Match candidates on text similarity.
        matches: list[tuple[Report, float]] = []
        for cand in candidates:
            cand_text = cand.description_clean or cand.description
            if not cand_text:
                continue
            score = fuzz.WRatio(text, cand_text)
            if score >= self.text_threshold:
                matches.append((cand, score))
        if not matches:
            return None

        # Prefer any existing group_id — joining an established
        # cluster beats minting a new one. Ties broken by highest
        # similarity score.
        with_group = [(c, s) for c, s in matches if c.dedupe_group_id is not None]
        if with_group:
            best_cand, best_score = max(with_group, key=lambda pair: pair[1])
            assert best_cand.dedupe_group_id is not None
            log.info(
                "enrichment.dedupe.joined_group",
                report_id=str(snap.report_id),
                group_id=best_cand.dedupe_group_id,
                joined_via=str(best_cand.report_id),
                score=best_score,
            )
            return best_cand.dedupe_group_id

        # No candidate has a group yet — mint one and back-fill it
        # onto the earliest matched candidate so the group has two
        # members instead of one after this transaction commits.
        earliest = min(matches, key=lambda pair: pair[0].created_at)[0]
        new_group_id = _new_group_id()
        earliest.dedupe_group_id = new_group_id
        log.info(
            "enrichment.dedupe.minted_group",
            report_id=str(snap.report_id),
            group_id=new_group_id,
            back_filled_report_id=str(earliest.report_id),
            candidates=len(candidates),
            matched=len(matches),
        )
        return new_group_id

    # ─── Helpers ─────────────────────────────────────────────────

    def _window_start(self, created_at: datetime) -> datetime:
        """The candidate window is [created_at - window, created_at].
        Using the report's own timestamp (not `now()`) keeps re-runs
        of the flow reproducible — no drift from wall-clock."""
        # Defensive: report timestamps should already be tz-aware but
        # tolerate a naive datetime by assuming UTC.
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return created_at - timedelta(hours=self.window_hours)

    async def _find_candidates(
        self,
        session: SqlAsyncSession,
        *,
        hazard_type: str,
        location: str,
        since: datetime,
        exclude_report_id: str,
    ) -> list[Report]:
        """Query the reports table for co-located, co-timed rows of the
        same hazard. Filtering happens in SQL so we don't drag every
        recent report through the app process."""
        stmt = (
            select(Report)
            .where(Report.hazard_type == hazard_type)
            .where(Report.created_at >= since)
            .where(Report.report_id != snap_uuid_or_str(exclude_report_id))
        )
        rows = list((await session.scalars(stmt)).all())
        # Location match is a Python-side filter — the column carries
        # either the resolved district ("Visakhapatnam, Andhra Pradesh")
        # or the raw caller string, and we want to match either shape
        # coming in against either shape stored. Cheap; the SQL filters
        # already cap the candidate set to seconds' worth of typing.
        return [r for r in rows if _location_matches(location, r)]


def _location_matches(needle: str, row: Report) -> bool:
    """A row matches if its resolved OR raw location string contains
    the needle (case-insensitive) or vice versa. Handles the
    asymmetry where the current report may have `location_resolved`
    but a candidate only has `location_raw`, or the reverse."""
    n = needle.strip().lower()
    if not n:
        return False
    haystacks = [
        (row.location_resolved or "").strip().lower(),
        (row.location_raw or "").strip().lower(),
    ]
    for h in haystacks:
        if not h:
            continue
        if n == h or n in h or h in n:
            return True
    return False


def _new_group_id() -> str:
    """UUID4 base32 with the hyphens stripped — 32 chars, fits in the
    `String(64)` column with room to spare, and is copy/paste-friendly
    for ops."""
    return uuid4().hex


def snap_uuid_or_str(value: str):  # type: ignore[no-untyped-def]
    """Return a value the SQLAlchemy UUID column can compare against.
    Kept as a thin wrapper so future dialects (asyncpg vs SQLite)
    stay behind a single seam."""
    from uuid import UUID

    return UUID(value) if isinstance(value, str) else value


__all__ = [
    "DEFAULT_TEXT_THRESHOLD",
    "DEFAULT_WINDOW_HOURS",
    "TextWindowDedupe",
]
