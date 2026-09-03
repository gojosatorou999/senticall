"""Deepgram Flux keyterm vocabulary.

Coverage:
- All hazard synonyms from `extraction/normalize.py` are present in
  `build_keyterms()` (consistency invariant — the two files can't
  cross-import due to layer contract, so a test guards drift).
- All severity synonyms likewise.
- Case-insensitive dedup preserves first-seen order.
- Result is bounded by MAX_KEYTERMS.
- Every returned term is non-empty stripped.
- Payload shape matches Deepgram's `{"keyterms": [...]}` contract.
"""

from __future__ import annotations

from fg_voice.extraction.normalize import _HAZARD_SYNONYMS, _SEVERITY_SYNONYMS
from fg_voice.rag.keyterms import (
    MAX_KEYTERMS,
    build_keyterms,
    build_keyterms_prompt,
    dedupe_case_insensitive,
)

# ─── Consistency with the extractor's synonym maps ───────────────────


def test_all_hazard_synonyms_are_keyterms():
    """The extractor's canonical hazard synonyms MUST all appear in
    the STT bias set. A new synonym added to `normalize.py` without
    a corresponding entry here means the STT won't bias toward it —
    the extractor could still catch it if the STT happens to
    transcribe correctly, but the whole point of keyterm prompting
    is to nudge that transcription in the first place."""
    keyterms_lower = {t.lower() for t in build_keyterms()}
    missing = {phrase for phrase in _HAZARD_SYNONYMS if phrase.lower() not in keyterms_lower}
    assert not missing, (
        f"hazard synonyms missing from keyterms: {sorted(missing)} — "
        "add to `_HAZARD_KEYTERMS` in `rag/keyterms.py`"
    )


def test_all_severity_synonyms_are_keyterms():
    """Same invariant for severity vocab."""
    keyterms_lower = {t.lower() for t in build_keyterms()}
    missing = {phrase for phrase in _SEVERITY_SYNONYMS if phrase.lower() not in keyterms_lower}
    assert not missing, f"severity synonyms missing from keyterms: {sorted(missing)}"


# ─── Dedup + ordering ───────────────────────────────────────────────


def test_dedupe_case_insensitive_preserves_first_seen_order():
    """Deepgram biases toward early entries in the keyterm list. So
    dedup must keep first-seen (not last-seen) and must not sort."""
    result = dedupe_case_insensitive(["Storm", "flood", "STORM", "surge", "Flood"])
    assert result == ["Storm", "flood", "surge"]


def test_dedupe_strips_whitespace():
    result = dedupe_case_insensitive(["  storm  ", "storm", " flood"])
    assert result == ["storm", "flood"]


def test_dedupe_ignores_empty_strings():
    result = dedupe_case_insensitive(["", "   ", "storm", ""])
    assert result == ["storm"]


# ─── Bounds + shape ─────────────────────────────────────────────────


def test_build_keyterms_under_cap():
    """The current vocabulary should comfortably fit under the cap;
    if this fails, tighten the coastal-context list before raising
    the cap."""
    terms = build_keyterms()
    assert len(terms) <= MAX_KEYTERMS
    assert len(terms) > 30  # sanity — should have hazard + severity + context


def test_all_keyterms_are_non_empty_stripped():
    """No leading/trailing whitespace, no empty strings — Deepgram
    would reject either."""
    for t in build_keyterms():
        assert t
        assert t == t.strip()


def test_hazard_terms_come_first():
    """Hazard vocab is the highest-signal biasing — must be at the
    head of the list per Deepgram's early-entry bias."""
    terms = build_keyterms()
    # "storm" is the first hazard term in `_HAZARD_KEYTERMS`.
    assert terms[0] == "storm"


def test_build_keyterms_prompt_shape():
    """`{"keyterms": [...]}` — the payload shape Deepgram Flux
    accepts on its WS connection."""
    prompt = build_keyterms_prompt()
    assert list(prompt.keys()) == ["keyterms"]
    assert isinstance(prompt["keyterms"], list)
    assert prompt["keyterms"] == build_keyterms()


def test_no_duplicate_keyterms_in_output():
    terms = build_keyterms()
    lowered = [t.lower() for t in terms]
    assert len(lowered) == len(set(lowered))
