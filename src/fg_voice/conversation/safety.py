"""Life-safety tripwire (spec §2.4).

Runs on every committed caller turn BEFORE the extractor sees it. If the
transcript looks like it contains injury, entrapment, or immediate danger,
the graph short-circuits to EMERGENCY_REDIRECT — which plays a fixed
prompt directing to 112 and sets `life_safety_flag`. The agent never
generates safety advice.

Enforced as a pure function so the tripwire cannot itself become an
LLM-driven decision. Keyword matching first; a proper kNN classifier
lands in P4 alongside the taxonomy embeddings."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

# Word-boundary matched so "injured" catches but "gingerly" doesn't.
# Multi-word phrases are matched as substrings after collapsing spaces.
_TRIPWIRE_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        # injury / medical
        "injured",
        "bleeding",
        "wounded",
        "hurt",
        "unconscious",
        "dying",
        "dead",
        "died",
        "drowning",
        "drowned",
        "cardiac",
        "heart attack",
        # entrapment
        "trapped",
        "stuck",
        "cannot move",
        "can't move",
        "cant move",
        "buried",
        "pinned",
        # collapse / immediate danger
        "collapsed",
        "collapsing",
        "help me",
        "save me",
        "save us",
        "swept away",
        "washed away",
        # explicit escalation
        "emergency",
        "ambulance",
    }
)

_WORD_KEYWORDS: Final[frozenset[str]] = frozenset(
    {kw for kw in _TRIPWIRE_KEYWORDS if " " not in kw}
)
_PHRASE_KEYWORDS: Final[frozenset[str]] = frozenset({kw for kw in _TRIPWIRE_KEYWORDS if " " in kw})

_WORDS_RE: Final[re.Pattern[str]] = re.compile(r"[a-z']+")


@dataclass(frozen=True, slots=True)
class TripwireVerdict:
    """`triggered=True` diverts the graph to EMERGENCY_REDIRECT and
    sets `life_safety_flag`. `matched_terms` is retained for audit —
    the caller never hears the matched words back."""

    triggered: bool
    matched_terms: tuple[str, ...]


def _normalise(transcript: str) -> str:
    """Lowercase + collapse whitespace. Anything more clever (stemming,
    accent folding) belongs in the P4 classifier, not here."""
    return " ".join(transcript.lower().split())


def check_transcript(transcript: str) -> TripwireVerdict:
    """Pure. Deterministic. No I/O. Safe to call from unit tests."""
    if not transcript.strip():
        return TripwireVerdict(triggered=False, matched_terms=())
    normalised = _normalise(transcript)
    words = set(_WORDS_RE.findall(normalised))
    hits: list[str] = sorted(w for w in words if w in _WORD_KEYWORDS)
    hits.extend(sorted(p for p in _PHRASE_KEYWORDS if p in normalised))
    return TripwireVerdict(triggered=bool(hits), matched_terms=tuple(hits))


__all__ = ["TripwireVerdict", "check_transcript"]
