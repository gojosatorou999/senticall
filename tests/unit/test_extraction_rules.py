"""Keyword-rule extractors + normaliser coverage."""

from __future__ import annotations

import pytest

from fg_voice.extraction import keyword_rules
from fg_voice.extraction.normalize import (
    depth_to_cm,
    normalize_confirmation,
    normalize_depth,
    normalize_hazard_type,
    normalize_severity,
    normalize_yes_no,
)


@pytest.mark.parametrize(
    "utt,expected",
    [
        ("yes I'm reporting", "yes"),
        ("Yeah", "yes"),
        ("please do go ahead", "yes"),
        ("no thanks", "no"),
        ("cancel", "no"),
        ("wrong number", "no"),
        ("uhh maybe", None),
        ("", None),
    ],
)
def test_normalize_yes_no(utt, expected):
    assert normalize_yes_no(utt) == expected


@pytest.mark.parametrize(
    "utt,expected",
    [
        ("this is extremely severe", "extreme"),
        ("very bad flooding", "extreme"),
        ("moderate right now", "moderate"),
        ("quite bad honestly", "moderate"),
        ("just light rain damage", "light"),
        ("not that bad", "light"),
        ("random text", None),
    ],
)
def test_normalize_severity_longest_match_wins(utt, expected):
    # "extremely severe" beats "severe" alone
    assert normalize_severity(utt) == expected


@pytest.mark.parametrize(
    "utt,expected",
    [
        ("king tide flooding the road", "abnormal_tide"),
        ("storm surge coming in", "abnormal_tide"),
        ("cyclone knocked down trees", "storm"),
        ("thunderstorm and lightning", "storm"),
        ("oil spill on the beach", "sludge_oil"),
        ("black water and diesel smell", "sludge_oil"),
        ("cliff collapsed at the shore", "erosion"),
        ("something else", "other"),
        ("nothing to report", None),
    ],
)
def test_normalize_hazard_type(utt, expected):
    assert normalize_hazard_type(utt) == expected


@pytest.mark.parametrize(
    "utt,band",
    [
        ("water is at my ankle", "ankle"),
        ("up to the knees", "knee"),
        ("waist deep", "waist"),
        ("above the waist now", "above_waist"),
        ("chest high", "above_waist"),
        ("over my head almost", "above_waist"),
        ("higher than waist", "above_waist"),
        ("some water", None),
    ],
)
def test_normalize_depth(utt, band):
    assert normalize_depth(utt) == band


def test_depth_to_cm_monotonic():
    assert (
        depth_to_cm("ankle")
        < depth_to_cm("knee")
        < depth_to_cm("waist")
        < depth_to_cm("above_waist")
    )


@pytest.mark.parametrize(
    "utt,expected",
    [
        ("yes please", "yes"),
        ("no wait", "no"),
        ("restart it", "restart"),
        ("start over", "restart"),
        ("that's wrong", "restart"),
        ("change it", "restart"),
    ],
)
def test_normalize_confirmation(utt, expected):
    assert normalize_confirmation(utt) == expected


# ─── Extractor wrappers ──────────────────────────────────────────────


def test_extract_intent_yes():
    ex = keyword_rules.extract_intent("yes reporting a hazard")
    assert ex.value == "yes"
    assert ex.confidence > 0
    assert ex.evidence


def test_extract_intent_unclear():
    ex = keyword_rules.extract_intent("hmm ummm hello")
    assert ex.value == "unclear"
    assert ex.confidence == 0.0


def test_extract_intent_not_sure_reads_as_no():
    """Ambiguous "not sure" is treated as a rejection, not as
    accidental yes matching on "sure". Regression for the normalize
    longest-across-both-lists rule."""
    ex = keyword_rules.extract_intent("hmm not sure yet")
    assert ex.value == "no"


def test_extract_hazard_type_returns_canonical():
    ex = keyword_rules.extract_hazard_type("we have a diesel oil spill")
    assert ex.value == "sludge_oil"


def test_extract_severity_extreme():
    ex = keyword_rules.extract_severity("this is very severe")
    assert ex.value == "extreme"


def test_extract_depth_waist():
    ex = keyword_rules.extract_depth("water is waist deep")
    assert ex.value == "waist"


def test_extract_confirmation_yes_no_restart():
    assert keyword_rules.extract_confirmation("yes").value == "yes"
    assert keyword_rules.extract_confirmation("no").value == "no"
    assert keyword_rules.extract_confirmation("start over").value == "restart"
    assert keyword_rules.extract_confirmation("mumble").value == "unclear"


def test_extract_free_text_empty_returns_none():
    assert keyword_rules.extract_free_text("   ") is None


def test_extract_free_text_clips_at_500():
    long = "a" * 800
    ex = keyword_rules.extract_free_text(long)
    assert ex is not None
    assert len(ex.value) == 500
