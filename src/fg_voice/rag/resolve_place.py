"""Hybrid place resolver — spec §10.1.

Input: a spoken fragment ("near RK beach", "Kakinada", "Bheemili")
plus an optional geographic prior (caller's district/state).
Output: `ResolvedPlace` with the top-1 candidate, top-1 score, and
top-1/top-2 margin so the graph guards in `RESOLVE_LOCATION` can
route confident → ASK_SEVERITY, ambiguous → DISAMBIGUATE_LOCATION,
uncertain → CONFIRM_LOCATION_LOW_CONF, or hopeless → retry.

Signals fused per query:
- **exact_name**: normalised-ASCII exact match on name or variant.
  Score 1.0.
- **substring**: whole-word substring inside the fragment (§10.1
  fallback). Longest match wins. Score 0.85.
- **phonetic**: Double-Metaphone key equality on the last-word token.
  Score 0.75 per hit; multiple hits keep the top scorer.
- **fuzzy**: rapidfuzz WRatio on name + variants. Score is
  `score/100` normalised. Kept as the safety net.

Fusion strategy: **Reciprocal Rank Fusion (RRF)** across the four
signals. Each signal produces its own top-N list; RRF blends the
ranks. Ties are broken by the sum of raw signal scores, so a place
that landed rank 1 in exact + rank 3 in fuzzy still beats one that
landed rank 2 in both.

Geographic prior: entries in the caller's district/state get a
constant score bump AFTER fusion — mimics the spec's "geographic
prior re-rank" without a full learned model. When no prior is
available, this is a no-op."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from fg_voice.rag.gazetteer import Gazetteer, GazetteerEntry
from fg_voice.rag.phonetic import normalise_ascii, phonetic_keys

try:
    from rapidfuzz import fuzz

    _HAVE_RAPIDFUZZ = True
except ImportError:  # pragma: no cover
    _HAVE_RAPIDFUZZ = False


# Signal weights when we compute the fused score. Kept as constants
# so a tuning change is a single-line edit.
_WEIGHT_EXACT: Final[float] = 1.00
_WEIGHT_SUBSTRING: Final[float] = 0.85
_WEIGHT_PHONETIC: Final[float] = 0.75
_WEIGHT_FUZZY: Final[float] = 0.60

# RRF constant. Standard value from Cormack et al.; 60 dampens the
# rank-1 dominance so signals actually mix.
_RRF_K: Final[int] = 60

# Fuzzy cutoff — below this the fuzzy signal contributes nothing.
# Anything lower and rapidfuzz starts matching "erosion" ↔ "arrive".
_FUZZY_MIN_SCORE: Final[int] = 70

# Geographic-prior bumps. Kept small so a strong-signal match from
# outside the caller's district still wins over a weak-signal match
# from inside it. The two are additive.
_PRIOR_DISTRICT_BUMP: Final[float] = 0.05
_PRIOR_STATE_BUMP: Final[float] = 0.02


@dataclass(frozen=True, slots=True)
class GeographicPrior:
    """Bundled hints from the caller side. Any field may be None
    when we don't have that information yet — the resolver treats
    absent fields as "no bump" rather than "penalty"."""

    district: str | None = None
    state: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedPlace:
    """Result of one resolve() call.

    - `top_entry`: the winning gazetteer entry (or None if nothing
      scored above zero).
    - `top_score`: fused + prior-adjusted score in [0, 1+]. Not
      strictly bounded above 1.0 because the prior bump adds to a
      max-1.0 fused score.
    - `top1_top2_margin`: gap between the winner and runner-up.
      Below `Settings.geo_margin_threshold` triggers the disambiguate
      node.
    - `runner_up`: the second-best candidate (for the disambiguate
      prompt's `{option_b}`). None when only one candidate scored.
    """

    top_entry: GazetteerEntry | None
    top_score: float
    top1_top2_margin: float
    runner_up: GazetteerEntry | None
    candidates: tuple[tuple[GazetteerEntry, float], ...] = field(default_factory=tuple)


class GazetteerResolver:
    """Stateful resolver holding a reference to the current gazetteer.
    Cheap to build — the gazetteer does the heavy lifting at load
    time. Hot-reload works by swapping `.gazetteer` on the instance."""

    def __init__(self, gazetteer: Gazetteer) -> None:
        self.gazetteer = gazetteer

    def resolve(
        self,
        fragment: str,
        prior: GeographicPrior | None = None,
        *,
        top_k: int = 5,
    ) -> ResolvedPlace:
        """Resolve `fragment` against the gazetteer. Returns a
        ResolvedPlace even when nothing matched (top_entry=None,
        top_score=0.0). `top_k` caps the returned candidate list —
        the resolver still internally considers everything."""
        cleaned = normalise_ascii(fragment)
        if not cleaned:
            return ResolvedPlace(None, 0.0, 0.0, None, ())

        # Gather ranked lists per signal.
        exact_ranked = self._exact_match(cleaned)
        substring_ranked = self._substring_match(cleaned)
        phonetic_ranked = self._phonetic_match(fragment)
        fuzzy_scored = self._fuzzy_match_scored(fragment) if _HAVE_RAPIDFUZZ else []
        fuzzy_ranked = [e for _, e in fuzzy_scored]

        # RRF fusion — used for RANKING when signals disagree.
        rrf = _reciprocal_rank_fusion(
            [
                (_WEIGHT_EXACT, exact_ranked),
                (_WEIGHT_SUBSTRING, substring_ranked),
                (_WEIGHT_PHONETIC, phonetic_ranked),
                (_WEIGHT_FUZZY, fuzzy_ranked),
            ]
        )
        if not rrf:
            return ResolvedPlace(None, 0.0, 0.0, None, ())

        # Compute the per-entry MAX signal score — this is the number
        # used as `top_score` (and therefore the confidence gate). An
        # exact-match hit produces 1.0; a fuzzy-only match maxes at
        # the normalised rapidfuzz WRatio. RRF stays for ranking so
        # multi-signal hits still order sensibly.
        signal_scores = _compute_signal_scores(
            cleaned=cleaned,
            exact=exact_ranked,
            substring=substring_ranked,
            phonetic=phonetic_ranked,
            fuzzy=fuzzy_scored,
        )

        # Ranking pass: prefer higher RRF (multi-signal agreement)
        # first, then higher signal-max as tiebreak.
        def _sort_key(item: tuple[GazetteerEntry, float]) -> tuple[float, float]:
            entry, rrf_score = item
            key = f"{entry.canonical_name}|{entry.district}|{entry.state}"
            return (rrf_score, signal_scores.get(key, 0.0))

        # Apply prior BEFORE the final sort.
        if prior is not None:
            rrf = _apply_prior(rrf, prior)

        rrf.sort(key=_sort_key, reverse=True)

        # Build candidates with the SIGNAL-max score (not the RRF sum).
        candidates_list: list[tuple[GazetteerEntry, float]] = []
        for entry, _rrf_score in rrf[:top_k]:
            key = f"{entry.canonical_name}|{entry.district}|{entry.state}"
            candidates_list.append((entry, signal_scores.get(key, 0.0)))
        candidates = tuple(candidates_list)

        top_entry, top_score = candidates[0]
        runner_up: GazetteerEntry | None = None
        margin = top_score
        if len(candidates) > 1:
            runner_up = candidates[1][0]
            margin = top_score - candidates[1][1]

        return ResolvedPlace(
            top_entry=top_entry,
            top_score=top_score,
            top1_top2_margin=margin,
            runner_up=runner_up,
            candidates=candidates,
        )

    # ─── Per-signal helpers ──────────────────────────────────────────

    def _exact_match(self, cleaned: str) -> list[GazetteerEntry]:
        """Whole-fragment exact match against name_index."""
        entry = self.gazetteer.name_index.get(cleaned)
        return [entry] if entry is not None else []

    def _substring_match(self, cleaned: str) -> list[GazetteerEntry]:
        """Any name / variant that appears as a whole-word substring
        inside the fragment. Longest-first, so 'kakinada beach' beats
        'kakinada'."""
        hits: list[tuple[int, GazetteerEntry]] = []
        for key, entry in self.gazetteer.name_index.items():
            if not key:
                continue
            # Whole-word by delimiting cleaned with spaces on both
            # sides — cleaned is already whitespace-free ASCII from
            # normalise_ascii, but that means substrings must simply
            # be a suffix / infix / prefix. For "kakinadabeach"
            # (concatenated), any name that appears is a real hit.
            if key in cleaned:
                hits.append((len(key), entry))
        hits.sort(key=lambda kv: kv[0], reverse=True)
        # Dedup — same entry might match via multiple aliases.
        seen: set[str] = set()
        out: list[GazetteerEntry] = []
        for _, e in hits:
            k = f"{e.canonical_name}|{e.district}|{e.state}"
            if k in seen:
                continue
            seen.add(k)
            out.append(e)
        return out

    def _phonetic_match(self, fragment: str) -> list[GazetteerEntry]:
        """Phonetic-key match on the LAST word of the fragment (the
        assumption: place names typically sit at the end of Indian-
        English location utterances — "in Kakinada", "at RK beach")."""
        tokens = [t for t in fragment.split() if t.strip()]
        if not tokens:
            return []
        primary, secondary = phonetic_keys(tokens[-1])
        candidates: list[GazetteerEntry] = []
        seen: set[str] = set()
        for key in (primary, secondary):
            if not key:
                continue
            for e in self.gazetteer.phonetic_index.get(key, ()):
                k = f"{e.canonical_name}|{e.district}|{e.state}"
                if k in seen:
                    continue
                seen.add(k)
                candidates.append(e)
        return candidates

    def _fuzzy_match_scored(self, fragment: str) -> list[tuple[float, GazetteerEntry]]:
        """rapidfuzz WRatio over the whole entry corpus. Slow-ish
        (O(N) per query); the gazetteer's small enough for now.
        Returns (score, entry) pairs sorted by score descending,
        filtered to those clearing `_FUZZY_MIN_SCORE`. Score is
        rapidfuzz's raw WRatio (0-100)."""
        cleaned_fragment = normalise_ascii(fragment)
        if not cleaned_fragment:
            return []
        scored: list[tuple[float, GazetteerEntry]] = []
        seen: set[str] = set()
        for entry in self.gazetteer.entries:
            k = f"{entry.canonical_name}|{entry.district}|{entry.state}"
            if k in seen:
                continue
            best_score = fuzz.WRatio(cleaned_fragment, normalise_ascii(entry.matched_name))
            if entry.matched_name != entry.canonical_name:
                cscore = fuzz.WRatio(cleaned_fragment, normalise_ascii(entry.canonical_name))
                best_score = max(best_score, cscore)
            if best_score < _FUZZY_MIN_SCORE:
                continue
            seen.add(k)
            scored.append((float(best_score), entry))
        scored.sort(key=lambda kv: kv[0], reverse=True)
        return scored


# ─── Per-entry signal-max scorer ─────────────────────────────────────


def _compute_signal_scores(
    *,
    cleaned: str,
    exact: list[GazetteerEntry],
    substring: list[GazetteerEntry],
    phonetic: list[GazetteerEntry],
    fuzzy: list[tuple[float, GazetteerEntry]],
) -> dict[str, float]:
    """For each entry that surfaced in any signal, compute its MAX
    calibrated score across signals. Exact hits get 1.0; substring
    gets the coverage ratio (matched length / fragment length,
    capped at 0.95); phonetic gets 0.75; fuzzy gets rapidfuzz/100.

    The returned dict keys are `canonical|district|state` — same key
    scheme as the RRF helper uses so downstream lookups match."""
    scores: dict[str, float] = {}

    def _add(entry: GazetteerEntry, score: float) -> None:
        key = f"{entry.canonical_name}|{entry.district}|{entry.state}"
        if score > scores.get(key, 0.0):
            scores[key] = score

    # Exact — 1.0 (perfect confidence).
    for e in exact:
        _add(e, 1.0)

    # Substring — coverage-weighted with a LENGTH bonus so a longer
    # matched name (more specific) beats a shorter one. Cap at 0.95
    # so exact stays strictly better than substring.
    #
    # "problem at Kakinada Beach" — both "kakinada" (8 chars) and
    # "kakinadabeach" (13 chars) hit. The 13-char match is more
    # informative — the caller named the specific POI. So we weight
    # coverage BY the matched-name length: length * coverage.
    if substring and cleaned:
        for e in substring:
            name = normalise_ascii(e.matched_name)
            if not name:
                continue
            coverage = min(1.0, len(name) / max(len(cleaned), 1))
            # Length-weighted score: a 4-char name maxes at 0.72; a
            # 13-char name maxes at 0.95. Prevents "at" being a
            # useful substring signal.
            length_factor = min(1.0, len(name) / 12.0)
            _add(e, min(0.95, 0.60 + 0.35 * coverage * length_factor))

    # Phonetic — 0.75. STT-mishear resilience.
    for e in phonetic:
        _add(e, 0.75)

    # Fuzzy — rapidfuzz WRatio / 100.
    for score, e in fuzzy:
        _add(e, min(0.99, score / 100.0))

    return scores


# ─── RRF fusion helper ───────────────────────────────────────────────


def _reciprocal_rank_fusion(
    signal_lists: list[tuple[float, list[GazetteerEntry]]],
) -> list[tuple[GazetteerEntry, float]]:
    """Fuse per-signal ranked lists via RRF.

    RRF score for entry `e` = sum over signals of
        weight_signal * (1 / (K + rank_signal(e)))
    where rank is 1-based. Missing from a signal → contributes 0.

    Returns unsorted list of (entry, fused_score). Caller sorts —
    keeping it unsorted here lets a prior-bump step happen before
    the final sort without a second traversal."""
    scores: dict[str, float] = {}
    entries: dict[str, GazetteerEntry] = {}
    for weight, ranked in signal_lists:
        for rank, entry in enumerate(ranked, start=1):
            key = f"{entry.canonical_name}|{entry.district}|{entry.state}"
            entries.setdefault(key, entry)
            scores[key] = scores.get(key, 0.0) + weight * (1.0 / (_RRF_K + rank))
    return [(entries[k], score) for k, score in scores.items()]


def _apply_prior(
    fused: list[tuple[GazetteerEntry, float]],
    prior: GeographicPrior,
) -> list[tuple[GazetteerEntry, float]]:
    """Add small bumps to entries whose district / state matches the
    prior. Bumps are additive so a prior can nudge an entry with two
    matches (right district AND right state) more than one with just
    the state."""
    if prior.district is None and prior.state is None:
        return fused
    prior_district_norm = normalise_ascii(prior.district) if prior.district else None
    prior_state_norm = normalise_ascii(prior.state) if prior.state else None
    out: list[tuple[GazetteerEntry, float]] = []
    for entry, score in fused:
        bump = 0.0
        if (
            prior_district_norm
            and entry.district
            and normalise_ascii(entry.district) == prior_district_norm
        ) or (
            prior_district_norm
            and entry.kind == "district"
            and normalise_ascii(entry.canonical_name) == prior_district_norm
        ):
            bump += _PRIOR_DISTRICT_BUMP
        if prior_state_norm and normalise_ascii(entry.state) == prior_state_norm:
            bump += _PRIOR_STATE_BUMP
        out.append((entry, score + bump))
    return out


__all__ = [
    "GazetteerResolver",
    "GeographicPrior",
    "ResolvedPlace",
]
