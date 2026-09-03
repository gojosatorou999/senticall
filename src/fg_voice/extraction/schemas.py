"""Pydantic slot schemas — the typed interface between extraction and
conversation. The LLM extractor (lands in P4) MUST return one of these,
and the keyword rules in P2 return one of these. That way the graph
never sees an untyped `Any`.

Kept intentionally free of business logic; validation happens on the
value/confidence bounds and the enum literal, and nothing else."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ─── Canonical enum values ───────────────────────────────────────────

IntentValue = Literal["yes", "no", "unclear"]
HazardTypeValue = Literal[
    "storm", "flood", "tsunami", "sludge_oil", "abnormal_tide", "erosion", "other", "unclear"
]
SeverityValue = Literal["light", "moderate", "extreme", "unclear"]
# Named categorical for the DTMF ladder; converted to cm by
# `normalize.depth_to_cm` before being stored on the CallState.
DepthBand = Literal["ankle", "knee", "waist", "above_waist", "unclear"]
ConfirmationValue = Literal["yes", "no", "restart", "unclear"]


class _ExtractionBase(BaseModel):
    """Every extractor emits `value` + `confidence` + `evidence` — the
    `evidence` field is the verbatim caller span that justified the
    classification, kept for audit and for the golden-set builder."""

    model_config = ConfigDict(frozen=True)

    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = Field(default="", max_length=200)


class IntentExtraction(_ExtractionBase):
    value: IntentValue


class HazardTypeExtraction(_ExtractionBase):
    value: HazardTypeValue


class SeverityExtraction(_ExtractionBase):
    value: SeverityValue


class DepthExtraction(_ExtractionBase):
    value: DepthBand


class ConfirmationExtraction(_ExtractionBase):
    value: ConfirmationValue


class FreeTextExtraction(_ExtractionBase):
    """Description / location before the RAG resolver runs. The
    resolver replaces `value` with a canonical form and updates the
    confidence based on the gazetteer match margin."""

    value: str = Field(min_length=1, max_length=500)


__all__ = [
    "ConfirmationExtraction",
    "ConfirmationValue",
    "DepthBand",
    "DepthExtraction",
    "FreeTextExtraction",
    "HazardTypeExtraction",
    "HazardTypeValue",
    "IntentExtraction",
    "IntentValue",
    "SeverityExtraction",
    "SeverityValue",
]
