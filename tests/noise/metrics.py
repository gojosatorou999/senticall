"""Noise-sweep evaluation metrics — spec §9.5.

Five metrics per (noise_type, snr) cell:

- **WER** (Word Error Rate): reference vs hypothesis at the token
  level. Standard edit-distance / #ref_words formula. Lower is
  better. Range [0, +inf) — >1.0 is possible when the hypothesis is
  longer than the reference and full of insertions.

- **slot_accuracy**: fraction of utterances whose extracted slot
  value matched the ground-truth slot value exactly. The single
  number that most closely tracks "did the call actually work".
  Range [0, 1].

- **turns_to_completion**: mean number of turns the runner took to
  fill each slot. In real calls this maps to "how much reprompt
  ladder did we burn". Lower is better.

- **false_barge_in_rate**: fraction of turns where a `StartOfTurn`
  fired while the agent was still playing pre-lockout audio. The
  interrupt controller's own stats populate this — the harness just
  aggregates. Lower is better.

- **premature_cutoff_rate**: fraction of turns where the pipeline
  fired `EndOfTurn` before the caller finished the utterance (i.e.
  the transcript is shorter than the reference by more than a
  threshold). Detected by (hyp_word_count / ref_word_count) below
  the cutoff. Lower is better.

Ship-target reference (from spec §9.5 exit gate):
- slot_accuracy >= 0.92 at 10 dB SNR across all noise types
- slot_accuracy >= 0.85 at 5 dB SNR
- false_barge_in_rate < 0.02
- premature_cutoff_rate < 0.03
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# Lowercase, strip punctuation, collapse whitespace. Standard ASR
# scoring normalisation — matching what Deepgram's own WER tool uses.
_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)


def _tokenise(text: str) -> list[str]:
    lowered = text.lower()
    stripped = _PUNCT_RE.sub(" ", lowered)
    return stripped.split()


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Standard Levenshtein-distance-over-reference-length WER.
    Empty reference → 0.0 if hypothesis also empty, else 1.0."""
    ref = _tokenise(reference)
    hyp = _tokenise(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0

    # Classic 2-row edit-distance table. n * m memory + time.
    prev = list(range(len(hyp) + 1))
    curr = [0] * (len(hyp) + 1)
    for i, r in enumerate(ref, start=1):
        curr[0] = i
        for j, h in enumerate(hyp, start=1):
            cost = 0 if r == h else 1
            curr[j] = min(
                curr[j - 1] + 1,  # insertion
                prev[j] + 1,  # deletion
                prev[j - 1] + cost,  # substitution
            )
        prev, curr = curr, prev
    return prev[-1] / len(ref)


@dataclass(frozen=True, slots=True)
class TrialResult:
    """One utterance run through the pipeline at one (noise, snr)
    setting. `hyp_transcript` is what the STT produced; `expected_slot`
    / `extracted_slot` are the ground truth vs the extractor's output
    (may be None if the extractor gave up)."""

    utterance_id: str
    ref_transcript: str
    hyp_transcript: str
    expected_slot: str | None
    extracted_slot: str | None
    turns_used: int
    barge_in_count: int
    barge_in_false_count: int
    achieved_snr_db: float

    @property
    def slot_correct(self) -> bool:
        return self.expected_slot == self.extracted_slot

    @property
    def wer(self) -> float:
        return word_error_rate(self.ref_transcript, self.hyp_transcript)

    @property
    def premature_cutoff(self) -> bool:
        """A hypothesis with <70% of the reference token count is
        treated as premature cutoff. Threshold from empirical tuning
        on the P2 test bank — anything higher over-flags natural
        contractions ("gonna" vs "going to")."""
        ref_words = len(_tokenise(self.ref_transcript))
        if ref_words == 0:
            return False
        hyp_words = len(_tokenise(self.hyp_transcript))
        return (hyp_words / ref_words) < 0.7


@dataclass(slots=True)
class CellSummary:
    """Aggregated metrics across every utterance in one
    (noise_type, snr_db) cell of the sweep."""

    noise_type: str
    snr_db: float
    trials: list[TrialResult] = field(default_factory=list)

    def add(self, trial: TrialResult) -> None:
        self.trials.append(trial)

    @property
    def n(self) -> int:
        return len(self.trials)

    @property
    def wer_mean(self) -> float:
        if not self.trials:
            return 0.0
        return sum(t.wer for t in self.trials) / self.n

    @property
    def slot_accuracy(self) -> float:
        if not self.trials:
            return 0.0
        return sum(1 for t in self.trials if t.slot_correct) / self.n

    @property
    def turns_to_completion_mean(self) -> float:
        if not self.trials:
            return 0.0
        return sum(t.turns_used for t in self.trials) / self.n

    @property
    def false_barge_in_rate(self) -> float:
        """Total false barge-ins across all trials divided by total
        barge-in events. Zero-denominator → 0.0."""
        total_barge_ins = sum(t.barge_in_count for t in self.trials)
        if total_barge_ins == 0:
            return 0.0
        total_false = sum(t.barge_in_false_count for t in self.trials)
        return total_false / total_barge_ins

    @property
    def premature_cutoff_rate(self) -> float:
        if not self.trials:
            return 0.0
        return sum(1 for t in self.trials if t.premature_cutoff) / self.n

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe summary. Consumed by the harness report emitter."""
        return {
            "noise_type": self.noise_type,
            "snr_db": self.snr_db,
            "n": self.n,
            "wer_mean": round(self.wer_mean, 4),
            "slot_accuracy": round(self.slot_accuracy, 4),
            "turns_to_completion_mean": round(self.turns_to_completion_mean, 3),
            "false_barge_in_rate": round(self.false_barge_in_rate, 4),
            "premature_cutoff_rate": round(self.premature_cutoff_rate, 4),
        }


def summarise(trials: Iterable[TrialResult]) -> list[CellSummary]:
    """Group trials by (noise_type, snr_db) into CellSummary buckets.
    Ordering: noise_type ascending, snr_db descending (highest SNR
    first — matches the natural read-down of a sweep report)."""
    bucket: dict[tuple[str, float], CellSummary] = {}
    for t in trials:
        # The trial's noise/snr info is passed alongside via the
        # harness; a bare TrialResult doesn't carry it. Wrap in a
        # (noise, snr) key at the harness level and pass grouped lists
        # to this function — but to keep the API narrow, treat each
        # trial as its own noise/snr pair keyed on the achieved_snr_db.
        # In practice the harness invokes this function on grouped
        # sub-lists; the simple case here handles single-cell inputs.
        key = ("unspecified", round(t.achieved_snr_db, 1))
        cell = bucket.setdefault(key, CellSummary(noise_type=key[0], snr_db=key[1]))
        cell.add(t)
    return sorted(bucket.values(), key=lambda c: (c.noise_type, -c.snr_db))


__all__ = [
    "CellSummary",
    "TrialResult",
    "summarise",
    "word_error_rate",
]
