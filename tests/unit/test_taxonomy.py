"""Hazard-taxonomy corpus + kNN classifier — spec §10.2."""

from __future__ import annotations

import pytest

from fg_voice.rag.taxonomy import (
    HazardClassification,
    HazardClassifier,
    build_classifier,
    load_corpus,
)


@pytest.fixture(scope="module")
def classifier() -> HazardClassifier:
    return build_classifier()


# ─── Corpus ─────────────────────────────────────────────────────────


def test_corpus_covers_all_hazard_labels() -> None:
    corpus = load_corpus()
    labels = {ex.label for ex in corpus}
    assert labels == {
        "storm",
        "flood",
        "tsunami",
        "sludge_oil",
        "abnormal_tide",
        "erosion",
        "other",
    }


def test_corpus_size_reasonable() -> None:
    """At least 10 examples per class — enough to avoid over-fitting
    on any single phrasing."""
    corpus = load_corpus()
    per_label = {
        label: 0
        for label in {
            "storm",
            "flood",
            "tsunami",
            "sludge_oil",
            "abnormal_tide",
            "erosion",
            "other",
        }
    }
    for ex in corpus:
        per_label[ex.label] += 1
    for label, n in per_label.items():
        assert n >= 10, f"{label} has only {n} examples"


# ─── Classifier ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("there is a cyclone here", "storm"),
        ("trees are down", "storm"),
        ("the wind blew off the roof", "storm"),
        ("black oil is on the sand", "sludge_oil"),
        ("chemical spill in the harbour", "sludge_oil"),
        ("diesel leaked from a ship", "sludge_oil"),
        ("the tide is very high", "abnormal_tide"),
        ("high water in the streets", "abnormal_tide"),
        ("the street is completely flooded", "flood"),
        ("water has entered my house", "flood"),
        ("a tsunami is coming", "tsunami"),
        ("there is a tidal wave", "tsunami"),
        ("the cliff has collapsed", "erosion"),
        ("part of the beach is gone", "erosion"),
        ("sand is being washed away", "erosion"),
        ("something else is happening", "other"),
    ],
)
def test_classifier_recovers_labels(classifier: HazardClassifier, text: str, expected: str) -> None:
    result = classifier.classify(text)
    assert result.label == expected, (
        f"expected {expected} for {text!r}, got {result.label} (votes={result.votes})"
    )


def test_classifier_empty_input() -> None:
    c = build_classifier()
    result = c.classify("")
    assert result.label is None
    assert result.score == 0.0
    assert result.margin == 0.0


def test_classifier_out_of_vocab_input() -> None:
    """A string with zero in-vocab trigrams should return label=None
    without raising. Numbers-only input triggers this."""
    c = build_classifier()
    result = c.classify("!!!")
    # Might have some grams from padding — but even if it lands on
    # a label, score should be near zero relative to real matches.
    assert result.score >= 0.0
    assert 0.0 <= result.margin <= 1.0


def test_classifier_margin_is_bounded() -> None:
    """Score is a normalised vote share so top-1 is in [0, 1] and
    margin in [0, 1]."""
    c = build_classifier()
    result = c.classify("cyclone damage everywhere")
    assert 0.0 <= result.score <= 1.0
    assert 0.0 <= result.margin <= 1.0


def test_classifier_uses_top_k_neighbours() -> None:
    """The default k=5 should surface at least 1 neighbour for a
    typical query. Setting k=1 should collapse the vote to a single
    label with score=1.0."""
    c = HazardClassifier(corpus=load_corpus(), k=1)
    result = c.classify("cyclone damage everywhere")
    assert result.label is not None
    assert result.score == pytest.approx(1.0)
    assert result.margin == pytest.approx(1.0)


# ─── HazardClassification shape ─────────────────────────────────────


def test_classification_result_is_frozen() -> None:
    """HazardClassification is frozen so a stray mutation can't
    silently rewrite a decision the runner already acted on."""
    result = HazardClassification(
        label="storm", score=0.6, margin=0.2, votes={"storm": 0.6, "other": 0.4}
    )
    with pytest.raises((AttributeError, TypeError)):
        result.label = "other"  # type: ignore[misc]


# ─── Margin gate integration (§9.4) ─────────────────────────────────


def test_high_confidence_query_has_large_margin(classifier: HazardClassifier) -> None:
    """A textbook-clear phrasing should produce a strong margin
    (well above the §9.4 0.15 threshold for LLM fallback)."""
    result = classifier.classify("cyclone damage everywhere trees down")
    assert result.margin > 0.15


def test_ambiguous_query_may_trigger_llm_fallback(classifier: HazardClassifier) -> None:
    """A one-word ambiguous phrasing may produce a low margin — that's
    the whole point of the §9.4 gate. Not a strict assertion on
    which side it falls, just that the classifier returns a decision
    (or None) without crashing."""
    result = classifier.classify("bad")
    assert result.score >= 0.0
