"""JSON-file gazetteer geocoder for the P6 enrichment DAG.

First real implementation of the `Geocoder` protocol. Loads a
hand-curated list of Telangana + Andhra Pradesh districts + variants
at construction time, then resolves caller `location_raw` strings via
fuzzy match. Returns a canonical `"District, State"` string, or None
when no match clears the score threshold.

Deliberate scope cuts:

- **District granularity only.** A caller who says "RK Beach" gets no
  match; one who says "RK Beach, Vizag" resolves to
  "Visakhapatnam, Andhra Pradesh". Landmark/village-level matching
  needs the P4 RAG gazetteer with proper name resolution + gazetteer
  vector embeddings. The JSON gazetteer is a first cut that resolves
  the district-level cases (which is the majority of ops-actionable
  reports anyway — dispatch happens per district).
- **No geometry / no coordinates.** `Geocoder.resolve()` returns a
  string, not a lat/lng. The full geocoder (P4) will return an
  enriched object; today we only fill `reports.location_resolved`,
  which is a string column.
- **No external service.** No Nominatim, no Google Maps, no ratelimit
  handling. Fully offline. When P4 lands, the external geocoder is a
  second-tier fallback for cases the gazetteer can't resolve.

Match algorithm (in order of precedence):

1. **Exact case-insensitive match** on canonical district name or any
   listed variant. Score 100.
2. **Substring match** — if the raw location contains a district name
   or variant as a whole word, that's a strong match. Score 95.
3. **Fuzzy match via rapidfuzz WRatio** over the full name+variant
   corpus. Score = the raw rapidfuzz score in [0, 100].

Results below `min_score` (default 80) are dropped — better to leave
`location_resolved` NULL than write a wrong district onto the row.
The threshold is tuned conservatively; a low-confidence match with
"Kothagudem" and "Kondapur" both scoring in the 70s isn't worth the
risk of routing an alert to the wrong district's response team.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from rapidfuzz import fuzz, process

from fg_voice.obs.logging import get_logger

log = get_logger(__name__)

# Default fuzzy-match cutoff. rapidfuzz WRatio returns [0, 100].
# 80 keeps out obvious mismatches while still catching phonetic
# variants ("Anantapuram" vs "Anantapur", 91) and typos ("Vishakapatnam"
# vs "Visakhapatnam", 92). Below 80, false positives dominate.
DEFAULT_MIN_SCORE: Final[int] = 80

# Word-boundary substring match — a raw location containing "vizag"
# as a whole word matches "Vizag" (a variant of Visakhapatnam), but
# "vizager" doesn't spuriously match. Case-insensitive.
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z\.\- ]*[A-Za-z]|[A-Za-z]")


@dataclass(frozen=True, slots=True)
class DistrictEntry:
    """One row from the loaded gazetteer JSON. Represents either a
    district (no `district` field) or a mandal / sub-district
    (`district` set to the parent). Kept as one class rather than
    a district/mandal split so the match corpus stays a flat list
    and rapidfuzz can score across both tiers in one pass.

    The `resolved()` format differs by tier:
    - District: `"Warangal, Telangana"`
    - Mandal:   `"Bapatla, Bapatla, Andhra Pradesh"`
      (mandal → district → state; deliberate three-part shape so
      downstream consumers can split on `, ` and know exactly which
      tier they got.)
    """

    name: str
    state: str
    variants: tuple[str, ...]
    # None → this is a district entry. Set → this is a mandal, and
    # the value is the canonical district name it belongs to.
    district: str | None = None

    def all_forms(self) -> tuple[str, ...]:
        return (self.name, *self.variants)

    def resolved(self) -> str:
        if self.district is not None:
            return f"{self.name}, {self.district}, {self.state}"
        return f"{self.name}, {self.state}"

    @property
    def is_mandal(self) -> bool:
        return self.district is not None


@dataclass(slots=True)
class JsonGazetteerGeocoder:
    """Resolves caller locations to canonical `"District, State"` via
    a JSON district list + fuzzy matcher. Not thread-safe on the mutable
    match caches (rapidfuzz's internals are), but instances are
    constructed once at boot and shared across the async event loop —
    single-writer, many-reader within one process.

    Instantiate with `from_path(path)` or hand-inject `entries` for
    tests.
    """

    entries: tuple[DistrictEntry, ...]
    min_score: int = DEFAULT_MIN_SCORE
    # Denormalised lookup structures — built once at construction.
    _exact_by_form: dict[str, DistrictEntry] = field(default_factory=dict, init=False)
    _all_forms: tuple[str, ...] = field(default_factory=tuple, init=False)
    _form_to_entry: dict[str, DistrictEntry] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        exact: dict[str, DistrictEntry] = {}
        forms: list[str] = []
        form_to_entry: dict[str, DistrictEntry] = {}
        # Register mandals FIRST — districts will overwrite mandal
        # exact matches on collision, because a district is the
        # safer coarser answer when the caller says just "Kakinada"
        # (both a district AND a mandal). Substring / fuzzy match
        # still compete across both tiers.
        mandals = tuple(e for e in self.entries if e.is_mandal)
        districts = tuple(e for e in self.entries if not e.is_mandal)
        for entry in (*mandals, *districts):
            for form in entry.all_forms():
                key = form.lower().strip()
                if not key:
                    continue
                # Unconditional overwrite so districts win on collision
                # over the mandals registered first. In the current
                # data this affects "Kakinada" (district beats mandal),
                # "Anantapur" (district beats mandal), etc.
                exact[key] = entry
                forms.append(form)
                form_to_entry[form] = entry
        # `dataclass(slots=True, frozen=False)` blocks attribute
        # assignment in `__post_init__` unless the field was declared;
        # our fields are declared with `init=False`, so this is fine.
        self._exact_by_form = exact
        self._all_forms = tuple(forms)
        self._form_to_entry = form_to_entry

    # ─── Public API ──────────────────────────────────────────────

    async def resolve(self, raw: str) -> str | None:
        """Fuzzy-resolve `raw` to `"District, State"`. Returns None if
        no candidate clears `min_score` (safe default — the row keeps
        NULL rather than getting a wrong district)."""
        if not raw or not raw.strip():
            return None

        cleaned = raw.strip()

        # (1) Exact match — the full raw string is a known form.
        exact = self._exact_by_form.get(cleaned.lower())
        if exact is not None:
            return exact.resolved()

        # (2) Whole-word substring — "RK Beach, Vizag" contains "Vizag".
        substring_hit = self._substring_match(cleaned)
        if substring_hit is not None:
            return substring_hit.resolved()

        # (3) Fuzzy match across the full form corpus. WRatio handles
        # typos, transpositions, and word-order shifts better than a
        # plain ratio.
        result = process.extractOne(
            cleaned,
            self._all_forms,
            scorer=fuzz.WRatio,
            score_cutoff=self.min_score,
        )
        if result is None:
            log.info(
                "enrichment.geocoder.no_match",
                raw=cleaned,
                cutoff=self.min_score,
            )
            return None
        matched_form, score, _index = result
        entry = self._form_to_entry[matched_form]
        log.info(
            "enrichment.geocoder.fuzzy_match",
            raw=cleaned,
            matched=matched_form,
            resolved=entry.resolved(),
            score=score,
        )
        return entry.resolved()

    # ─── Helpers ─────────────────────────────────────────────────

    def _substring_match(self, cleaned: str) -> DistrictEntry | None:
        """Whole-word case-insensitive substring — the raw string
        contains a known form as a distinct token."""
        raw_lower = cleaned.lower()
        # Iterate forms longest-first so "West Godavari" beats "Godavari"
        # on a raw like "West Godavari district office".
        forms_by_length = sorted(self._exact_by_form.keys(), key=len, reverse=True)
        for form in forms_by_length:
            # \b boundaries let "vizag" match in "RK Beach, Vizag" but
            # not in "vizagerpalli".
            if re.search(rf"\b{re.escape(form)}\b", raw_lower):
                return self._exact_by_form[form]
        return None


# ─── Loaders ─────────────────────────────────────────────────────────


def load_gazetteer(path: Path) -> tuple[DistrictEntry, ...]:
    """Read the JSON file and return the district tuple. Fail loud on
    a malformed file — boot-time errors are what we want; running with
    a bad gazetteer would silently mis-resolve for the whole shift."""
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or "districts" not in data:
        raise ValueError(f"Gazetteer at {path} missing top-level 'districts' key")
    raw_entries = data["districts"]
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ValueError(f"Gazetteer at {path} has empty or non-list 'districts'")
    entries: list[DistrictEntry] = []
    for i, row in enumerate(raw_entries):
        try:
            entries.append(
                DistrictEntry(
                    name=row["name"],
                    state=row["state"],
                    variants=tuple(row.get("variants", []) or ()),
                )
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(f"Gazetteer row {i} malformed: {exc}") from exc
    return tuple(entries)


def load_mandal_gazetteer(path: Path) -> tuple[DistrictEntry, ...]:
    """Read a mandal-level JSON (top-level `mandals` key, each row
    carries `name`/`district`/`state`). Returns `DistrictEntry` rows
    with `district` populated. Same fail-loud discipline as
    `load_gazetteer` — a bad file at boot beats mis-resolving for
    the whole shift."""
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or "mandals" not in data:
        raise ValueError(f"Mandal gazetteer at {path} missing top-level 'mandals' key")
    raw_entries = data["mandals"]
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ValueError(f"Mandal gazetteer at {path} has empty or non-list 'mandals'")
    entries: list[DistrictEntry] = []
    for i, row in enumerate(raw_entries):
        try:
            entries.append(
                DistrictEntry(
                    name=row["name"],
                    state=row["state"],
                    district=row["district"],
                    variants=tuple(row.get("variants", []) or ()),
                )
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(f"Mandal gazetteer row {i} malformed: {exc}") from exc
    return tuple(entries)


def build_gazetteer_geocoder(
    path: Path, *, min_score: int = DEFAULT_MIN_SCORE
) -> JsonGazetteerGeocoder:
    """Convenience constructor — reads the file + wires the geocoder in one call."""
    return JsonGazetteerGeocoder(entries=load_gazetteer(path), min_score=min_score)


def build_gazetteer_geocoder_with_mandals(
    districts_path: Path,
    mandals_path: Path | None,
    *,
    min_score: int = DEFAULT_MIN_SCORE,
) -> JsonGazetteerGeocoder:
    """Convenience constructor that loads BOTH gazetteers (districts +
    mandals). Passing None for `mandals_path` degrades gracefully to
    district-only matching — useful in tests + dev deploys that don't
    ship the mandal file."""
    entries: tuple[DistrictEntry, ...] = load_gazetteer(districts_path)
    if mandals_path is not None:
        entries = entries + load_mandal_gazetteer(mandals_path)
    return JsonGazetteerGeocoder(entries=entries, min_score=min_score)


# Keep _WORD_RE from being tree-shaken as dead code — reserved for a
# future landmark-token extractor that would run BEFORE district match.
_ = _WORD_RE

__all__ = [
    "DEFAULT_MIN_SCORE",
    "DistrictEntry",
    "JsonGazetteerGeocoder",
    "build_gazetteer_geocoder",
    "build_gazetteer_geocoder_with_mandals",
    "load_gazetteer",
    "load_mandal_gazetteer",
]
