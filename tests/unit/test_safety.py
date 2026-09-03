"""Life-safety tripwire (§2.4). Must trigger on injury/entrapment,
must not trigger on ordinary hazard reporting."""

from __future__ import annotations

import pytest

from fg_voice.conversation.safety import check_transcript


@pytest.mark.parametrize(
    "transcript",
    [
        "my brother is trapped in the water",
        "someone is bleeding on the road",
        "he's unconscious",
        "we are stuck on the roof, please help me",
        "she was swept away by the waves",
        "the man is drowning",
        "cannot move, everything collapsed",
        "help me, we're pinned under the wall",
        "he had a heart attack",
    ],
)
def test_tripwire_triggers_on_life_safety_phrases(transcript):
    verdict = check_transcript(transcript)
    assert verdict.triggered
    assert verdict.matched_terms


@pytest.mark.parametrize(
    "transcript",
    [
        "water is up to my knees at RK beach",
        "sludge is coming ashore near Kakinada",
        "tides are unusually high today",
        "the wind knocked over a small tree",
        "moderate flooding at the main road",
        "no damage yet just watching",
        "",
        "   ",
    ],
)
def test_tripwire_quiet_on_ordinary_reports(transcript):
    verdict = check_transcript(transcript)
    assert not verdict.triggered


def test_matched_terms_returned_sorted_and_unique():
    v = check_transcript("trapped and bleeding, someone is trapped")
    assert v.triggered
    # sorted uniqueness — "trapped" once, "bleeding" once
    assert list(v.matched_terms) == sorted(set(v.matched_terms))


def test_tripwire_is_deterministic():
    # Same input → same result. No RNG, no clock, no I/O.
    for _ in range(5):
        v = check_transcript("he is trapped")
        assert v.triggered
        assert "trapped" in v.matched_terms
