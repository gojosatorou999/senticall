"""Confidence gating thresholds and helpers — spec §9.4.

Two families of gate live here:

- **STT-level** (`stt_confidence_gate`): before the runner even runs
  the extractor, drop a transcript whose per-turn confidence sits
  below `STT_MIN_CONFIDENCE`. This is the "please repeat" fast path
  — no LLM call, no rule-extractor call, cheaper than either.

- **Extraction-level** (`extraction_confidence_gate`): after the
  extractor produced a `SlotValue`, drop it if the `SlotValue.
  confidence` sits below `EXTRACTION_MIN_CONFIDENCE`. Reason: the
  keyword rules or LLM produced *a* value, but didn't produce it
  with enough weight to commit. Advancing the reprompt ladder is
  the safer play than persisting a low-confidence guess.

The gazetteer-side gates (top-1 score, top-1/top-2 margin) live in
`rag/` and drive the graph's `RESOLVE_LOCATION` machine-node guards.
This module owns only the STT + extraction thresholds — the two
that fire on every prompted-node turn.

Thresholds mirror §9.4 exactly. Kept as module-level constants so a
single change here propagates to both runner and driver. The
gazetteer thresholds (0.60 / 0.10) already live on `Settings` as
`geo_confirm_threshold` / `geo_margin_threshold`; that duplication is
intentional because they cross the layered-contract boundary between
`conversation/` and `rag/`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from fg_voice.conversation.state import SlotValue

# ─── §9.4 constants ──────────────────────────────────────────────────

# STT turn confidence: Flux emits a per-turn confidence in [0, 1].
# Below this, don't even attempt extraction — the transcript is too
# uncertain to be worth an extractor cycle. Value from spec §9.4.
STT_MIN_CONFIDENCE: Final[float] = 0.55

# Extractor-emitted SlotValue confidence. Below this, treat the
# turn as unclear (advance the reprompt ladder).
EXTRACTION_MIN_CONFIDENCE: Final[float] = 0.60


# ─── Result types ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class GateResult:
    """Outcome of one confidence check. `pass_` = keep going; `reject`
    = drop the value and advance the reprompt ladder. `reason` is for
    logs + metrics — never shown to the caller."""

    pass_: bool
    reason: str | None = None

    @classmethod
    def ok(cls) -> GateResult:
        return cls(pass_=True, reason=None)

    @classmethod
    def failed(cls, reason: str) -> GateResult:
        return cls(pass_=False, reason=reason)


# ─── Gates ───────────────────────────────────────────────────────────


def stt_confidence_gate(confidence: float | None) -> GateResult:
    """Reject transcripts whose Flux-reported confidence is below
    `STT_MIN_CONFIDENCE`. `None` (Flux didn't emit a confidence) is
    treated as pass — we have no basis to reject, and Flux only omits
    confidence on control frames the runner filters out separately.

    Example wiring in the runner:

        gate = stt_confidence_gate(flux_event.confidence)
        if not gate.pass_:
            log.info("gate.stt_dropped", reason=gate.reason,
                     confidence=flux_event.confidence)
            return _NO_ANSWER   # advance the reprompt ladder
    """
    if confidence is None:
        return GateResult.ok()
    if confidence < STT_MIN_CONFIDENCE:
        return GateResult.failed(f"stt_confidence<{STT_MIN_CONFIDENCE}")
    return GateResult.ok()


def extraction_confidence_gate(slot_value: SlotValue | None) -> GateResult:
    """Reject SlotValues below `EXTRACTION_MIN_CONFIDENCE`. `None`
    (extractor returned no value at all) is treated as fail — the
    caller path already advances the ladder, but the gate result
    lets logging attribute the drop to extraction rather than to
    an upstream empty transcript.

    Example wiring in the runner:

        slot_value = run_extractor(node.extractor, transcript)
        gate = extraction_confidence_gate(slot_value)
        if not gate.pass_:
            self._advance_ladder(node)
            return
    """
    if slot_value is None:
        return GateResult.failed("extractor_returned_none")
    if slot_value.confidence < EXTRACTION_MIN_CONFIDENCE:
        return GateResult.failed(f"extraction_confidence<{EXTRACTION_MIN_CONFIDENCE}")
    return GateResult.ok()


__all__ = [
    "EXTRACTION_MIN_CONFIDENCE",
    "STT_MIN_CONFIDENCE",
    "GateResult",
    "extraction_confidence_gate",
    "stt_confidence_gate",
]
