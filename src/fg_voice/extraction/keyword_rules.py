"""Rule-based slot extractors used in P2.

The LLM extractor (P4) will replace these on categorical slots, but the
rules stay as the P4 fallback path when the LLM fails schema validation
twice. That is why every extractor here returns the same Pydantic
schema the LLM does — the graph does not need to know which one ran."""

from __future__ import annotations

from fg_voice.extraction.normalize import (
    normalize_confirmation,
    normalize_depth,
    normalize_hazard_type,
    normalize_severity,
    normalize_yes_no,
)
from fg_voice.extraction.schemas import (
    ConfirmationExtraction,
    DepthExtraction,
    FreeTextExtraction,
    HazardTypeExtraction,
    IntentExtraction,
    SeverityExtraction,
)

# Confidence assigned to a keyword-rule hit. Deliberately below the LLM
# extractor's ceiling so a low-margin LLM answer still wins when both
# are consulted. Rationale: keyword rules can't tell "not extreme" from
# "extreme"; the LLM can.
KEYWORD_HIT_CONFIDENCE = 0.75
KEYWORD_MISS_CONFIDENCE = 0.0


def _clip_evidence(utterance: str) -> str:
    return utterance.strip()[:200]


def extract_intent(utterance: str) -> IntentExtraction:
    hit = normalize_yes_no(utterance)
    if hit == "yes":
        return IntentExtraction(
            value="yes", confidence=KEYWORD_HIT_CONFIDENCE, evidence=_clip_evidence(utterance)
        )
    if hit == "no":
        return IntentExtraction(
            value="no", confidence=KEYWORD_HIT_CONFIDENCE, evidence=_clip_evidence(utterance)
        )
    return IntentExtraction(
        value="unclear", confidence=KEYWORD_MISS_CONFIDENCE, evidence=_clip_evidence(utterance)
    )


def extract_hazard_type(utterance: str) -> HazardTypeExtraction:
    hit = normalize_hazard_type(utterance)
    if hit is None:
        return HazardTypeExtraction(
            value="unclear",
            confidence=KEYWORD_MISS_CONFIDENCE,
            evidence=_clip_evidence(utterance),
        )
    return HazardTypeExtraction(
        value=hit, confidence=KEYWORD_HIT_CONFIDENCE, evidence=_clip_evidence(utterance)
    )


def extract_severity(utterance: str) -> SeverityExtraction:
    hit = normalize_severity(utterance)
    if hit is None:
        return SeverityExtraction(
            value="unclear",
            confidence=KEYWORD_MISS_CONFIDENCE,
            evidence=_clip_evidence(utterance),
        )
    return SeverityExtraction(
        value=hit, confidence=KEYWORD_HIT_CONFIDENCE, evidence=_clip_evidence(utterance)
    )


def extract_depth(utterance: str) -> DepthExtraction:
    hit = normalize_depth(utterance)
    if hit is None:
        return DepthExtraction(
            value="unclear",
            confidence=KEYWORD_MISS_CONFIDENCE,
            evidence=_clip_evidence(utterance),
        )
    return DepthExtraction(
        value=hit, confidence=KEYWORD_HIT_CONFIDENCE, evidence=_clip_evidence(utterance)
    )


def extract_confirmation(utterance: str) -> ConfirmationExtraction:
    hit = normalize_confirmation(utterance)
    if hit in ("yes", "no", "restart"):
        return ConfirmationExtraction(
            value=hit,
            confidence=KEYWORD_HIT_CONFIDENCE,
            evidence=_clip_evidence(utterance),
        )
    return ConfirmationExtraction(
        value="unclear",
        confidence=KEYWORD_MISS_CONFIDENCE,
        evidence=_clip_evidence(utterance),
    )


def extract_free_text(utterance: str) -> FreeTextExtraction | None:
    """Description / location: pass through, trimmed. Empty → None so
    the graph can distinguish "caller said nothing" from "caller said
    something that didn't classify"."""
    text = utterance.strip()
    if not text:
        return None
    return FreeTextExtraction(
        value=text[:500],
        # Free-text confidence is a proxy for "we got any words at all";
        # the P4 RAG resolver will overwrite this with the gazetteer
        # match margin, which is the number that actually matters.
        confidence=0.6,
        evidence=_clip_evidence(text),
    )


__all__ = [
    "KEYWORD_HIT_CONFIDENCE",
    "KEYWORD_MISS_CONFIDENCE",
    "extract_confirmation",
    "extract_depth",
    "extract_free_text",
    "extract_hazard_type",
    "extract_intent",
    "extract_severity",
]
