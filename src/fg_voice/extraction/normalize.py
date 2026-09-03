"""Canonicalisation for spoken slot values.

The rule extractor and the LLM extractor (P4) both feed through here so
the graph never has to know how the caller phrased it — it just sees
the canonical enum value or the depth in cm.

All functions are pure and cheap. Add a synonym here rather than
teaching the graph to accept a new form."""

from __future__ import annotations

from typing import Final

from fg_voice.extraction.schemas import DepthBand, HazardTypeValue, SeverityValue

# ─── Severity ────────────────────────────────────────────────────────

_SEVERITY_SYNONYMS: Final[dict[str, SeverityValue]] = {
    # light
    "light": "light",
    "small": "light",
    "minor": "light",
    "mild": "light",
    "slight": "light",
    "little": "light",
    "not too bad": "light",
    "not that bad": "light",
    # moderate
    "moderate": "moderate",
    "medium": "moderate",
    "quite bad": "moderate",
    "pretty bad": "moderate",
    "fairly bad": "moderate",
    "middling": "moderate",
    "average": "moderate",
    # extreme
    "extreme": "extreme",
    "severe": "extreme",
    "very bad": "extreme",
    "very severe": "extreme",
    "extremely bad": "extreme",
    "extremely severe": "extreme",
    "terrible": "extreme",
    "horrible": "extreme",
    "awful": "extreme",
    "worst": "extreme",
    "life-threatening": "extreme",
    "dangerous": "extreme",
}


def normalize_severity(utterance: str) -> SeverityValue | None:
    """Return canonical severity or None if no synonym matched."""
    text = utterance.lower().strip()
    if not text:
        return None
    # Longest match wins so "extremely severe" beats "severe".
    for phrase in sorted(_SEVERITY_SYNONYMS, key=len, reverse=True):
        if phrase in text:
            return _SEVERITY_SYNONYMS[phrase]
    return None


# ─── Hazard type ─────────────────────────────────────────────────────

_HAZARD_SYNONYMS: Final[dict[str, HazardTypeValue]] = {
    # storm damage
    "storm": "storm",
    "cyclone": "storm",
    "gale": "storm",
    "hurricane": "storm",
    "high wind": "storm",
    "strong wind": "storm",
    "tree fell": "storm",
    "tree down": "storm",
    "roof": "storm",
    "power line": "storm",
    "lightning": "storm",
    "thunderstorm": "storm",
    # sludge / oil
    "sludge": "sludge_oil",
    "slick": "sludge_oil",
    "oil": "sludge_oil",
    "spill": "sludge_oil",
    "petroleum": "sludge_oil",
    "diesel": "sludge_oil",
    "chemical": "sludge_oil",
    "black water": "sludge_oil",
    # abnormal tides / surge / king tide
    "tide": "abnormal_tide",
    "tides": "abnormal_tide",
    "king tide": "abnormal_tide",
    "surge": "abnormal_tide",
    "storm surge": "abnormal_tide",
    "swell": "abnormal_tide",
    "high water": "abnormal_tide",
    # flood — its own class now, not folded into abnormal_tide. A caller
    # saying "the street is flooded" is reporting inland inundation, which
    # is a different response than a tide anomaly.
    "flood": "flood",
    "flooded": "flood",
    "flooding": "flood",
    "water logging": "flood",
    "waterlogging": "flood",
    "water logged": "flood",
    "inundated": "flood",
    "inundation": "flood",
    "submerged": "flood",
    "knee deep water": "flood",
    "water entered": "flood",
    "water in the house": "flood",
    "nala overflow": "flood",
    "drain overflow": "flood",
    "overflowing": "flood",
    # tsunami — kept distinct from surge/tide because the escalation path
    # differs entirely.
    "tsunami": "tsunami",
    "tidal wave": "tsunami",
    "giant wave": "tsunami",
    "huge wave": "tsunami",
    "massive wave": "tsunami",
    "sea wall breach": "tsunami",
    # erosion (unlisted in DTMF but the LLM may still classify it)
    "erosion": "erosion",
    "cliff": "erosion",
    "collapsed bank": "erosion",
    "beach loss": "erosion",
    # explicit "other"
    "other": "other",
    "something else": "other",
    "not sure": "other",
}


def normalize_hazard_type(utterance: str) -> HazardTypeValue | None:
    text = utterance.lower().strip()
    if not text:
        return None
    for phrase in sorted(_HAZARD_SYNONYMS, key=len, reverse=True):
        if phrase in text:
            return _HAZARD_SYNONYMS[phrase]
    return None


# ─── Depth ───────────────────────────────────────────────────────────

_DEPTH_SYNONYMS: Final[dict[str, DepthBand]] = {
    "ankle": "ankle",
    "ankles": "ankle",
    "shin": "ankle",
    "shoes": "ankle",
    "shoe": "ankle",
    "knee": "knee",
    "knees": "knee",
    "thigh": "knee",
    "thighs": "knee",
    "waist": "waist",
    "waist deep": "waist",
    "hip": "waist",
    "hips": "waist",
    "above waist": "above_waist",
    "above the waist": "above_waist",
    "chest": "above_waist",
    "shoulder": "above_waist",
    "neck": "above_waist",
    "over head": "above_waist",
    "over my head": "above_waist",
    "higher than waist": "above_waist",
    "higher than the waist": "above_waist",
}

DEPTH_BAND_CM: Final[dict[DepthBand, int]] = {
    "ankle": 15,
    "knee": 50,
    "waist": 90,
    "above_waist": 140,
    "unclear": 0,
}


def normalize_depth(utterance: str) -> DepthBand | None:
    text = utterance.lower().strip()
    if not text:
        return None
    for phrase in sorted(_DEPTH_SYNONYMS, key=len, reverse=True):
        if phrase in text:
            return _DEPTH_SYNONYMS[phrase]
    return None


def depth_to_cm(band: DepthBand) -> int:
    return DEPTH_BAND_CM[band]


# ─── Intent / confirmation (yes/no family) ───────────────────────────

_YES_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "yes",
        "yeah",
        "yep",
        "yup",
        "correct",
        "right",
        "affirmative",
        "sure",
        "ok",
        "okay",
        "please do",
        "go ahead",
        "submit",
        "submit it",
        "reporting",
        "i am reporting",
        "i'm reporting",
        "i want to report",
        "wanted to report",
        "haan",  # tolerated Hindi/Telugu affirmatives
        "aan",
        "avunu",
    }
)

_NO_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "no",
        "nope",
        "nah",
        "not reporting",
        "not now",
        "not sure",
        "unsure",
        "dunno",
        "don't know",
        "dont know",
        "wrong number",
        "cancel",
        "stop",
        "hang up",
        "goodbye",
        "bye",
    }
)

_RESTART_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "restart",
        "start over",
        "start again",
        "redo",
        "redo it",
        "change it",
        "change",
        "wrong",
        "that's wrong",
        "thats wrong",
        "not right",
    }
)


# Near-misses telephony ASR returns for a spoken "yes" / "no" on en-IN
# calls — "yes" comes back as "yas" often enough to strand callers on the
# opening question. Matched against the WHOLE utterance only, never as
# substrings: "ya" occurs inside ordinary words and place names
# ("Vijayawada"), and a substring rule would turn those into a spurious
# confirmation at the submit prompt.
_YES_FUZZY_EXACT: Final[frozenset[str]] = frozenset(
    {"yas", "ya", "yaa", "yah", "yus", "ye", "yeh", "jes", "yes yes", "yas yas"}
)
_NO_FUZZY_EXACT: Final[frozenset[str]] = frozenset({"na", "naa", "nahi", "no no"})

_STRIP_PUNCT: Final = str.maketrans("", "", ".,!?;:'\"")


def normalize_yes_no(utterance: str) -> str | None:
    """Return "yes" / "no" / None. Longest phrase wins *across both
    lists* so "not sure" (no) beats "sure" (yes)."""
    text = utterance.lower().strip()
    if not text:
        return None

    # Whole-utterance ASR near-miss check, ahead of substring matching.
    bare = " ".join(text.translate(_STRIP_PUNCT).split())
    if bare in _YES_FUZZY_EXACT:
        return "yes"
    if bare in _NO_FUZZY_EXACT:
        return "no"

    combined: list[tuple[str, str]] = [(p, "yes") for p in _YES_TOKENS]
    combined.extend((p, "no") for p in _NO_TOKENS)
    combined.sort(key=lambda item: len(item[0]), reverse=True)
    for phrase, verdict in combined:
        if phrase in text:
            return verdict
    return None


def normalize_confirmation(utterance: str) -> str | None:
    """Return "yes" / "no" / "restart" / None."""
    text = utterance.lower().strip()
    if not text:
        return None
    for phrase in sorted(_RESTART_TOKENS, key=len, reverse=True):
        if phrase in text:
            return "restart"
    return normalize_yes_no(text)


__all__ = [
    "DEPTH_BAND_CM",
    "depth_to_cm",
    "normalize_confirmation",
    "normalize_depth",
    "normalize_hazard_type",
    "normalize_severity",
    "normalize_yes_no",
]
