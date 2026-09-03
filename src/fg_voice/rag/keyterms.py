"""Static keyterm vocabulary for the STT (Deepgram Flux) — spec §9.3.

Keyterm prompting is the single highest-leverage accuracy knob on
telephony-quality speech. Deepgram Flux accepts a list of biasing
terms; when it sees an audio segment that could plausibly be one of
them, it's more likely to transcribe it correctly. This matters most
for:

- **Regional English + Indian accents** — "cyclone" and "sludge" are
  common domain terms callers use but rare in Deepgram's default
  training bias.
- **Domain-specific vocabulary** — "storm surge", "king tide", and
  "abnormal tide" don't appear in a general-English model's top-N
  weights.
- **Ambiguous phonetic pairs** — "flood" vs. "flute", "slick" vs.
  "sleek", etc.

This module ships the STATIC vocabulary (hazard + severity terms +
common misspellings + regional variants) that's known at deploy time.
The DYNAMIC gazetteer-based keyterm list (place names from the
resolved-district's neighbourhood, per §10.1) is a P4 concern and
lives in `rag/place_keyterms.py` when it lands.

**Import-layer note.** The layers contract in `.importlinter` places
`fg_voice.extraction | fg_voice.rag | fg_voice.persistence` at the
same tier — siblings that cannot import from each other. So the
canonical hazard/severity synonym maps in `extraction/normalize.py`
are duplicated here as PLAIN string lists rather than imported.

`tests/unit/test_keyterms.py` includes a consistency test that fails
if the two drift — the "one place to add a synonym" invariant is
preserved by the test suite, not by cross-imports.
"""

from __future__ import annotations

from typing import Final

# Cap on keyterm count so a runaway synonym expansion doesn't blow the
# STT connection setup. 200 is well under Deepgram's practical limit;
# the hazard + severity + coastal static set fits in ~90.
MAX_KEYTERMS: Final[int] = 200

# Hazard-type surface forms callers use. Every entry here MUST also
# appear as a key in `extraction/normalize.py::_HAZARD_SYNONYMS` — a
# consistency test enforces this.
_HAZARD_KEYTERMS: Final[tuple[str, ...]] = (
    "storm",
    "cyclone",
    "gale",
    "hurricane",
    "high wind",
    "strong wind",
    "tree fell",
    "tree down",
    "roof",
    "power line",
    "lightning",
    "thunderstorm",
    "sludge",
    "slick",
    "oil",
    "spill",
    "petroleum",
    "diesel",
    "chemical",
    "black water",
    "tide",
    "tides",
    "king tide",
    "surge",
    "storm surge",
    "swell",
    "high water",
    "flooding",
    "flood",
    "flooded",
    "water logging",
    "water logged",
    "inundated",
    "inundation",
    "submerged",
    "knee deep water",
    "water entered",
    "water in the house",
    "nala overflow",
    "drain overflow",
    "overflowing",
    "tsunami",
    "tidal wave",
    "giant wave",
    "huge wave",
    "massive wave",
    "sea wall breach",
    "erosion",
    "cliff",
    "collapsed bank",
    "beach loss",
    "other",
    "something else",
    "not sure",
)

# Severity terms callers use.
_SEVERITY_KEYTERMS: Final[tuple[str, ...]] = (
    "light",
    "small",
    "minor",
    "mild",
    "slight",
    "little",
    "not too bad",
    "not that bad",
    "moderate",
    "medium",
    "quite bad",
    "pretty bad",
    "fairly bad",
    "middling",
    "average",
    "extreme",
    "severe",
    "very bad",
    "very severe",
    "extremely bad",
    "extremely severe",
    "terrible",
    "horrible",
    "awful",
    "worst",
    "life-threatening",
    "dangerous",
)

# Yes/no response vocabulary. Three nodes ask a closed question — the
# opening ASK_INTENT, the low-confidence location confirm, and the final
# submit confirm — and without these the recogniser has no bias toward
# the two words that actually answer them. On en-IN calls a bare "yes"
# was coming back as "yas", which matches no rule and re-prompts until
# the caller is timed out on the very first question.
_RESPONSE_KEYTERMS: Final[tuple[str, ...]] = (
    "yes",
    "no",
    "yeah",
    "yep",
    "yup",
    "nope",
    "correct",
    "right",
    "that's right",
    "wrong",
    "not right",
    "sure",
    "okay",
    "go ahead",
    "submit",
    "start over",
    "change it",
    "haan",
    "nahi",
)

# Coastal + safety context — not slot values themselves, but words
# that appear alongside them and benefit from STT bias. Kept separate
# from `_HAZARD_KEYTERMS` / `_SEVERITY_KEYTERMS` so the extractor's
# synonym map isn't polluted with context words that would fire
# false-positive slot matches.
_COASTAL_CONTEXT_TERMS: Final[tuple[str, ...]] = (
    # Coastal geography
    "beach",
    "shore",
    "coastline",
    "jetty",
    "harbour",
    "harbor",
    "port",
    "estuary",
    "creek",
    "backwater",
    "lagoon",
    "mangrove",
    # Tide / water level vocabulary
    "high tide",
    "low tide",
    "spring tide",
    "neap tide",
    "water level",
    "waves",
    "wave",
    "current",
    "rip current",
    "undertow",
    # Common regional flood descriptors
    "waterlogged",
    "waterlogging",
    "inundation",
    "inundated",
    "submerged",
    "washed away",
    "washed out",
    "cut off",
    "marooned",
    # Body-part depth references (spec §11.3 water_depth extraction)
    "ankle deep",
    "knee deep",
    "waist deep",
    "chest deep",
    "shoulder deep",
    "head high",
    # Danger / life-safety words — feed the safety tripwire; also
    # ensure the STT doesn't drop these on noisy audio.
    "trapped",
    "stuck",
    "drowning",
    "injured",
    "hurt",
    "unconscious",
    "child",
    "elderly",
    "help",
    "emergency",
    "rescue",
    # Common Indian-English hazard vocabulary
    "bandh",  # closure due to protest / flood
    "nala",  # drain
    "nallah",
    "gully",
    "culvert",
)


def dedupe_case_insensitive(terms: list[str]) -> list[str]:
    """Case-insensitive dedup while preserving first-seen order.
    Deepgram treats keyterms case-insensitively so sending both
    "Storm" and "storm" wastes the budget. First-seen ordering
    matters — Deepgram's docs note that the model's attention to
    keyterms is slightly biased toward early entries, so the
    high-value hazard verbs come first."""
    seen: set[str] = set()
    out: list[str] = []
    for term in terms:
        key = term.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(term.strip())
    return out


def build_keyterms() -> list[str]:
    """Assemble the full static keyterm list.

    Order: hazard vocabulary first (highest-value biasing), then
    severity terms, then coastal-context vocabulary. Dedup applied
    across the whole list; capped at MAX_KEYTERMS."""
    ordered: list[str] = []
    # Response vocabulary first: it is the shortest, most frequently
    # spoken, and most easily misheard set, and the opening question
    # cannot be answered without it.
    ordered.extend(_RESPONSE_KEYTERMS)
    ordered.extend(_HAZARD_KEYTERMS)
    ordered.extend(_SEVERITY_KEYTERMS)
    ordered.extend(_COASTAL_CONTEXT_TERMS)
    deduped = dedupe_case_insensitive(ordered)
    if len(deduped) > MAX_KEYTERMS:
        # If this ever fires in practice, tighten the coastal-context
        # list rather than raising MAX_KEYTERMS — the hazard/severity
        # set is the core value and shouldn't be truncated.
        deduped = deduped[:MAX_KEYTERMS]
    return deduped


def build_keyterms_prompt() -> dict[str, list[str]]:
    """Deepgram Flux `keyterms` payload shape. Wired into the WS
    connection URL by the transport (P3 continuation) via:

        params = {"keyterm": [...]}  # per Flux docs

    Wrapping in a dict here lets callers pass `**build_keyterms_prompt()`
    or serialise directly to JSON."""
    return {"keyterms": build_keyterms()}


# ─── Dynamic per-call keyterms (spec §9.3) ───────────────────────────


def build_dynamic_keyterms(
    *,
    gazetteer: object | None = None,
    prior_district: str | None = None,
    prior_state: str | None = None,
    top_k_places: int = 50,
) -> list[str]:
    """Assemble a per-call keyterm list: STATIC hazard/severity/coastal
    vocabulary + top-K place names biased to the caller's district
    or state.

    `gazetteer` is typed as `object` to keep this module free of a
    hard rag/gazetteer import (that module imports rag/phonetic
    which imports pip-optional `metaphone` — we don't want a
    keyterms build to trigger that dep chain when the caller only
    wants the static set). Runtime type is a `Gazetteer` instance
    with a `by_district` and `by_state` method.

    `prior_district` / `prior_state` are the caller's inferred geo
    location (from msisdn state/circle, prior reports, or a running
    incident). When both are None, this returns exactly the static
    keyterms.

    Returned list is capped at `MAX_KEYTERMS` — static + top-K places
    fits comfortably under 200 in practice."""
    parts: list[str] = []
    parts.extend(_HAZARD_KEYTERMS)
    parts.extend(_SEVERITY_KEYTERMS)
    parts.extend(_COASTAL_CONTEXT_TERMS)

    if gazetteer is not None and (prior_district or prior_state):
        # District entries first — tighter geographic prior than state.
        seen: set[str] = set()
        candidates: list[str] = []
        if prior_district and hasattr(gazetteer, "by_district"):
            for entry in gazetteer.by_district(prior_district):
                name = getattr(entry, "canonical_name", "")
                if name and name.lower() not in seen:
                    seen.add(name.lower())
                    candidates.append(name)
                    for v in getattr(entry, "variants", ()):
                        if v.lower() not in seen:
                            seen.add(v.lower())
                            candidates.append(v)
                if len(candidates) >= top_k_places:
                    break
        if len(candidates) < top_k_places and prior_state and hasattr(gazetteer, "by_state"):
            for entry in gazetteer.by_state(prior_state):
                if len(candidates) >= top_k_places:
                    break
                name = getattr(entry, "canonical_name", "")
                if name and name.lower() not in seen:
                    seen.add(name.lower())
                    candidates.append(name)
        parts.extend(candidates[:top_k_places])

    deduped = dedupe_case_insensitive(parts)
    if len(deduped) > MAX_KEYTERMS:
        deduped = deduped[:MAX_KEYTERMS]
    return deduped


__all__ = [
    "MAX_KEYTERMS",
    "build_dynamic_keyterms",
    "build_keyterms",
    "build_keyterms_prompt",
    "dedupe_case_insensitive",
]
