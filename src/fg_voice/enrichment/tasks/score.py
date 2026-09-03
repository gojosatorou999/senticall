"""Task 6 (§11): compute confidence + priority scores.

No external boundary needed — these are pure heuristics over the
snapshot and accumulator. Real deploy can swap for a learned model
later, but the shape of what this task produces (two ints in [0, 100])
stays the same, so the swap is contained.

Confidence intuition: how much do we trust the collected slots?
Currently a weighted sum of:
- slot completeness (hazard_type / severity / location / description
  filled) — 60%
- water_depth captured — 15%
- had-any-deep-extract-revision — 25% penalty for now (revisions mean
  the in-call extractor was uncertain); real scoring uses the deep
  extractor's confidence.

Priority intuition: ops attention. Currently:
- severity=extreme                → 90
- life_safety_flag                → 100
- severity=moderate               → 60
- severity=light                  → 25
- unknown severity, no flags      → 40 (default — needs review)"""

from __future__ import annotations

from fg_voice.enrichment.models import EnrichmentContext

_SEVERITY_PRIORITY: dict[str, int] = {
    "extreme": 90,
    "moderate": 60,
    "light": 25,
}
_UNKNOWN_PRIORITY = 40
_LIFE_SAFETY_PRIORITY = 100


def _slot_completeness(ctx: EnrichmentContext) -> float:
    """Fraction of the four headline slots that are filled."""
    snap = ctx.snapshot
    filled = sum(
        1 for v in (snap.hazard_type, snap.severity, snap.location_raw, snap.description) if v
    )
    return filled / 4.0


def _confidence(ctx: EnrichmentContext) -> int:
    completeness = _slot_completeness(ctx)
    water_depth_bonus = 0.15 if ctx.snapshot.water_depth_cm is not None else 0.0
    revision_penalty = 0.25 if ctx.result.revised_slots else 0.0
    score = (0.6 * completeness) + water_depth_bonus + (1 - revision_penalty) * 0.25
    return max(0, min(100, round(score * 100)))


def _priority(ctx: EnrichmentContext) -> int:
    if ctx.snapshot.flags.get("life_safety"):
        return _LIFE_SAFETY_PRIORITY
    severity = (ctx.snapshot.severity or "").lower()
    return _SEVERITY_PRIORITY.get(severity, _UNKNOWN_PRIORITY)


async def score(ctx: EnrichmentContext) -> None:
    """Compute both scores and stash them on the accumulator."""
    ctx.result.confidence_score = _confidence(ctx)
    ctx.result.priority_score = _priority(ctx)


__all__ = ["score"]
