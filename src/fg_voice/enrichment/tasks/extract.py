"""Task 3a (§11): deep extraction over the full call context.

This is the LLM boundary. The design:

- `LLMExtractor` is a Protocol that the flow depends on. Real
  implementations (OpenAI / Bedrock / vLLM) live under `extraction/`
  and get injected at wire-time in `main.py`.
- The default shipped here is `NoOpExtractor` — returns no revised
  slots so the row stays whatever the in-call extractor produced. This
  keeps the P6 flow runnable end-to-end without an API key configured.

Design invariant (CLAUDE.md #1): the extractor NEVER decides the next
node and NEVER produces caller-facing text. Its output here is a
`RevisedSlots` dict — a set of *proposed* slot overrides. `persist`
applies them against a whitelist of safe slots.

Confidence gating lives HERE, not in persist. If the extractor's
self-reported confidence is below the threshold, we don't stash the
revisions at all — persist sees an empty `revised_slots` and leaves
the row alone. Keeping the gate in one place (the boundary itself)
means persist stays a dumb writer.

We deliberately do not put this under `extraction/` — that package is
the *in-call* extractor (keyword rules + normalisers, no LLM). The
LLM-based deep pass runs post-call and is a distinct concern; keeping
it under `enrichment/tasks/` makes the split obvious in imports."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from fg_voice.enrichment.models import EnrichmentContext
from fg_voice.obs.logging import get_logger

log = get_logger(__name__)

# Below this confidence, the deep-extract proposals are dropped on the
# floor. Chosen conservatively — a false-positive slot overwrite
# corrupts the report row, which is worse than leaving the in-call
# keyword-rule extractor's guess in place. Tune per real extractor's
# calibration curve once one is wired.
DEFAULT_CONFIDENCE_THRESHOLD: Final[float] = 0.7


@dataclass(slots=True, frozen=True)
class RevisedSlots:
    """One deep-extract result. Each entry is a proposed override for
    a slot; `persist` applies them subject to the whitelist in
    `enrichment/tasks/persist.py::_REVISABLE_SLOTS`.
    `confidence` is the extractor's self-reported score in [0, 1]."""

    values: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    notes: str = ""


class LLMExtractor(Protocol):
    """Bounded LLM call: transcript in, structured output out. See
    CLAUDE.md invariant #1 — this must NEVER produce free-form
    caller-facing prose, and must NEVER route the conversation."""

    async def extract(self, snapshot_description: str | None) -> RevisedSlots: ...


@dataclass(slots=True)
class NoOpExtractor:
    """Default. Returns an empty RevisedSlots. Ships so the flow can
    run end-to-end without an LLM configured; a real deploy swaps this
    for `extraction.llm.OpenAiExtractor` (lands with the LLM adapter)."""

    async def extract(self, snapshot_description: str | None) -> RevisedSlots:
        return RevisedSlots()


async def deep_extract(
    ctx: EnrichmentContext,
    *,
    extractor: LLMExtractor,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> None:
    """Run the extractor over the raw description. If the extractor's
    confidence clears `confidence_threshold`, stash the proposed slot
    revisions on the accumulator (persist applies them). Below-threshold
    proposals are logged and dropped."""
    revised = await extractor.extract(ctx.snapshot.description)
    if not revised.values:
        return
    if revised.confidence < confidence_threshold:
        log.info(
            "enrichment.deep_extract.dropped_low_confidence",
            report_id=str(ctx.snapshot.report_id),
            proposed=sorted(revised.values.keys()),
            confidence=revised.confidence,
            threshold=confidence_threshold,
        )
        ctx.result.notes.append(
            f"deep_extract dropped {sorted(revised.values.keys())} "
            f"at conf={revised.confidence:.2f} (< {confidence_threshold:.2f})"
        )
        return
    ctx.result.revised_slots.update(revised.values)
    ctx.result.notes.append(
        f"deep_extract proposed {sorted(revised.values.keys())} at conf={revised.confidence:.2f}"
    )


__all__ = [
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "LLMExtractor",
    "NoOpExtractor",
    "RevisedSlots",
    "deep_extract",
]
