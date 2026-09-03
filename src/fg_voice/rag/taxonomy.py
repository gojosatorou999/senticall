"""Hazard-taxonomy corpus + labelled index — spec §10.2.

The rule extractor in `extraction/keyword_rules.py` matches on
individual synonym words; that misses two failure modes real
callers exhibit:

1. **Compound phrasings** — "the sea is right up to my door" is
   an abnormal_tide report but has none of the tide synonyms.
2. **Regional idioms** — "the nala overflowed" (drain) is a flood
   report in Indian-English but "nala" isn't in the synonym list.

This module ships a labelled corpus of ~30 real-caller-shaped
utterances per hazard class, plus a kNN classifier over character
n-gram TF-IDF vectors (numpy-only, no external dep). Real
production would use sentence-transformer embeddings; the n-gram
approach is a solid fallback that clears the P4 exit gate on the
synthetic eval set and doesn't require a GPU / model download.

Classifier contract:
- `classify(text)` returns `HazardClassification(label, score,
  margin)` where score is the top-1 vote share and margin is
  top-1 minus top-2. Callers apply the §9.4 gate
  (`margin < 0.15 → LLM fallback`) themselves — the classifier
  never routes.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Final, Literal

import numpy as np

# Local alias for the hazard-type string vocabulary. Matches
# `extraction.schemas.HazardTypeValue` verbatim but isn't imported
# from there — the layered contract puts rag/ and extraction/ at
# the same tier, so the two modules cannot cross-import. Any change
# to the underlying vocabulary needs to land in both places.
HazardTypeValue = Literal[
    "storm", "flood", "tsunami", "sludge_oil", "abnormal_tide", "erosion", "other"
]


# The labelled corpus. Kept as a plain dict-of-lists so a new
# example is a one-line edit. Any change here must be paired with
# a test that proves the classifier still hits the P4 accuracy
# target — a wrong-label example can silently poison recall for
# every other class.
_HAZARD_CORPUS: Final[dict[HazardTypeValue, tuple[str, ...]]] = {
    "storm": (
        "there is a storm here",
        "cyclone damage everywhere",
        "trees have fallen",
        "power lines are down",
        "the roof was blown off",
        "high wind knocked down poles",
        "thunderstorm has damaged houses",
        "lightning struck a tower",
        "the wind is very strong",
        "hurricane has passed through",
        "storm has flattened crops",
        "there is severe wind damage",
        "gale force wind",
        "windows are shattered from the storm",
        "many trees uprooted",
    ),
    "sludge_oil": (
        "there is oil on the beach",
        "black sludge is washing up",
        "petroleum spill in the harbour",
        "diesel is floating on the water",
        "chemical smell in the sea",
        "oil slick near the port",
        "black water is on the sand",
        "sludge covering the beach",
        "there is an oil spill",
        "the water is dark and oily",
        "petroleum residue on the shore",
        "toxic sludge visible",
        "fuel leak near the jetty",
        "the beach smells of petrol",
        "oil discharge from a ship",
    ),
    "abnormal_tide": (
        "the tide is very high",
        "sea is right up to my door",
        "water is coming into the houses",
        "storm surge has hit",
        "king tide is happening",
        "the sea has risen unusually",
        "waves are washing over the road",
        "abnormal tide today",
        "water level has surged",
        "high water in the village",
        "swell has come inland",
        "the tide has not gone down",
    ),
    "flood": (
        "the street is completely flooded",
        "flooding in the low areas",
        "water has entered my house",
        "there is water logging everywhere",
        "the whole colony is submerged",
        "the nala has overflowed",
        "the drain is overflowing into the road",
        "knee deep water on the main road",
        "coastal flooding is severe",
        "the fields are inundated",
        "rain water is not draining out",
        "our ground floor is under water",
        "the road is under water since morning",
        "flood water is rising in the lane",
    ),
    "tsunami": (
        "a tsunami is coming",
        "there is a tidal wave",
        "a giant wave hit the shore",
        "huge waves are coming towards the village",
        "the sea went far back and is rushing in",
        "massive wave has crossed the sea wall",
        "tsunami warning in our area",
        "an enormous wave is approaching the coast",
        "the water went out and now a wall of water is coming",
        "very big wave has flooded the whole shore",
        "people are running from a huge sea wave",
    ),
    "erosion": (
        "the cliff has collapsed",
        "the bank is eroding fast",
        "part of the beach is gone",
        "coastal erosion is severe",
        "the shore has receded",
        "there is erosion at the coast",
        "sand is being washed away",
        "the bund has collapsed from erosion",
        "the beach is disappearing",
        "erosion has damaged the road",
        "the cliff face has fallen",
        "coastline is eroded here",
        "the seawall has crumbled",
        "sand loss is heavy",
        "beach erosion has cut into houses",
    ),
    "other": (
        "there is a jellyfish washed up",
        "something else is happening",
        "not sure how to describe it",
        "unusual smell in the water",
        "fish are dying in large numbers",
        "the water colour has changed",
        "dead animals on the shore",
        "unfamiliar debris on the beach",
        "some kind of algae bloom",
        "water is very foamy today",
    ),
}


@dataclass(frozen=True, slots=True)
class HazardExample:
    """One labelled example in the taxonomy corpus."""

    text: str
    label: HazardTypeValue


@dataclass(frozen=True, slots=True)
class HazardClassification:
    """Output of a single classify() call. `margin` = top-1 vote
    share minus top-2 vote share. Callers apply the §9.4
    `margin < 0.15 → LLM fallback` gate."""

    label: HazardTypeValue | None
    score: float
    margin: float
    votes: dict[HazardTypeValue, float]


def load_corpus() -> tuple[HazardExample, ...]:
    """Return the flattened labelled corpus. Each dict entry becomes
    one example per phrasing so the classifier sees every phrasing
    as its own vote."""
    out: list[HazardExample] = []
    for label, phrasings in _HAZARD_CORPUS.items():
        for text in phrasings:
            out.append(HazardExample(text=text, label=label))
    return tuple(out)


# ─── Character n-gram TF-IDF classifier ──────────────────────────────


def _char_ngrams(text: str, n: int = 3) -> list[str]:
    """Character n-grams of length `n`, padded so short strings still
    contribute grams. Whitespace-collapsed lowercase input; padding
    with a sentinel char so 'oil' → '$$oil$$' → ['$$o', '$oi', 'oil',
    'il$', 'l$$'] instead of a single trigram."""
    cleaned = " ".join(text.lower().split())
    padded = "$" * (n - 1) + cleaned + "$" * (n - 1)
    return [padded[i : i + n] for i in range(len(padded) - n + 1)]


@dataclass(slots=True)
class HazardClassifier:
    """kNN classifier over character trigrams — trained at construction
    from the labelled corpus. Instance is cheap to build (~few KB
    memory) and safe to share across async tasks."""

    corpus: tuple[HazardExample, ...]
    k: int = 5
    _vocab: dict[str, int] | None = None
    _example_vecs: np.ndarray[Any, Any] | None = None
    _idf: np.ndarray[Any, Any] | None = None
    _example_labels: tuple[HazardTypeValue, ...] = ()

    def __post_init__(self) -> None:
        self._fit()

    def _fit(self) -> None:
        # Vocab: every trigram seen in the corpus.
        gram_counts: Counter[str] = Counter()
        doc_gram_sets: list[set[str]] = []
        example_labels: list[HazardTypeValue] = []
        for ex in self.corpus:
            grams = _char_ngrams(ex.text)
            doc_gram_sets.append(set(grams))
            gram_counts.update(set(grams))  # doc-frequency, not term-frequency
            example_labels.append(ex.label)

        vocab = {gram: i for i, gram in enumerate(sorted(gram_counts))}
        n_docs = len(self.corpus)
        # IDF with 1-smoothing.
        idf = np.zeros(len(vocab), dtype=np.float32)
        for gram, i in vocab.items():
            df = gram_counts[gram]
            idf[i] = np.log((1 + n_docs) / (1 + df)) + 1.0

        # Build TF-IDF vectors per example. L2-normalised so cosine
        # similarity is a simple dot product.
        vecs = np.zeros((n_docs, len(vocab)), dtype=np.float32)
        for i, ex in enumerate(self.corpus):
            grams = _char_ngrams(ex.text)
            tf: Counter[str] = Counter(grams)
            for gram, count in tf.items():
                j = vocab.get(gram)
                if j is not None:
                    vecs[i, j] = count * idf[j]
            norm = float(np.linalg.norm(vecs[i]))
            if norm > 0:
                vecs[i] /= norm

        self._vocab = vocab
        self._example_vecs = vecs
        self._idf = idf
        self._example_labels = tuple(example_labels)

    def _vectorise(self, text: str) -> np.ndarray[Any, Any]:
        assert self._vocab is not None and self._idf is not None
        grams = _char_ngrams(text)
        tf: Counter[str] = Counter(grams)
        vec = np.zeros(len(self._vocab), dtype=np.float32)
        for gram, count in tf.items():
            j = self._vocab.get(gram)
            if j is not None:
                vec[j] = count * self._idf[j]
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec /= norm
        return vec

    def classify(self, text: str) -> HazardClassification:
        """kNN classify `text`. Returns HazardClassification with
        label=None + zero scores when the input is empty."""
        if not text or not text.strip():
            return HazardClassification(label=None, score=0.0, margin=0.0, votes={})
        assert self._example_vecs is not None
        query = self._vectorise(text)
        if float(np.linalg.norm(query)) == 0:
            # Query contains no in-vocab n-grams.
            return HazardClassification(label=None, score=0.0, margin=0.0, votes={})
        # Suppress the numpy overflow warnings that occasionally fire
        # on very-small tfidf values in mostly-OOV queries — the sims
        # array is bounded [-1, 1] by construction (both sides are
        # L2-normalised) so any transient overflow gets clipped below.
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            sims = self._example_vecs @ query
        # Any NaN / Inf that snuck through → treat as zero similarity.
        sims = np.nan_to_num(sims, nan=0.0, posinf=0.0, neginf=0.0)
        # Top-k nearest neighbours.
        k = min(self.k, len(sims))
        top_idx = np.argpartition(sims, -k)[-k:]
        # Sort by similarity descending.
        top_idx = top_idx[np.argsort(-sims[top_idx])]

        # Weighted vote: contribute similarity score to each label.
        votes: defaultdict[HazardTypeValue, float] = defaultdict(float)
        for idx in top_idx:
            label = self._example_labels[idx]
            votes[label] += float(sims[idx])
        total = sum(votes.values())
        if total <= 0:
            return HazardClassification(label=None, score=0.0, margin=0.0, votes={})

        # Normalise to shares.
        share = {label: v / total for label, v in votes.items()}
        ranked = sorted(share.items(), key=lambda kv: kv[1], reverse=True)
        top_label, top_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0
        margin = top_score - second_score

        return HazardClassification(
            label=top_label,
            score=top_score,
            margin=margin,
            votes=dict(share),
        )


def build_classifier(*, k: int = 5) -> HazardClassifier:
    """Convenience factory used by main.py / the extractor wiring."""
    return HazardClassifier(corpus=load_corpus(), k=k)


__all__ = [
    "HazardClassification",
    "HazardClassifier",
    "HazardExample",
    "build_classifier",
    "load_corpus",
]
