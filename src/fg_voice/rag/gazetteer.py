"""In-memory gazetteer — spec §10.1 store side.

Loads district / mandal / coastal-POI JSON files and materialises a
unified index the resolver walks per query. Kept as an immutable
container so the snapshot loader can atomically swap a new instance
in on hot-reload without any per-query locking.

Storage strategy:
- **Rows**: list of `GazetteerEntry` dataclasses, one per name+variant
  (variants are exploded to their own row pointing back to the
  canonical entry).
- **Indices**: three parallel dicts for O(1) exact-match paths —
  `name_index` (normalised name / variant → entry), `phonetic_index`
  (phonetic key → list of entries; collision-friendly), and
  `district_index` (district name → list of entries in that district,
  for the geographic-prior re-ranker).

The gazetteer file layout mirrors the existing `districts.json` +
`mandals.json` shape so `JsonGazetteerGeocoder` (P6 dedupe helper)
and this loader share the same on-disk contract."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from fg_voice.rag.phonetic import normalise_ascii, phonetic_keys

# Kinds classified across the three tiers. Kept as a Final tuple so
# a stray typo in a JSON row fails at boot rather than silently
# creating a new kind.
_ALLOWED_KINDS: Final[frozenset[str]] = frozenset(
    {"district", "mandal", "beach", "port", "jetty", "temple", "landmark"}
)


@dataclass(frozen=True, slots=True)
class GazetteerEntry:
    """One place in the gazetteer. `canonical_name` is the display
    form (`"RK Beach"`); `matched_name` is the specific alias/variant
    the row was indexed under (may equal canonical_name)."""

    canonical_name: str
    matched_name: str
    kind: str
    district: str | None  # None for district-tier rows themselves
    state: str
    lat: float | None
    lon: float | None
    variants: tuple[str, ...] = field(default_factory=tuple)

    @property
    def display(self) -> str:
        """Human-facing form used in prompts + reports. Mandals prepend
        their district; district-tier rows return "District, State"."""
        if self.kind == "district":
            return f"{self.canonical_name}, {self.state}"
        if self.district:
            return f"{self.canonical_name}, {self.district}, {self.state}"
        return f"{self.canonical_name}, {self.state}"


@dataclass(frozen=True, slots=True)
class Gazetteer:
    """Immutable indexed gazetteer. Instances are cheap to construct
    (~50k dict ops on the full 3-tier corpus) and safe to share across
    async tasks. `swap()` on the loader replaces the atomic reference,
    not the underlying dicts, so an in-flight resolve() sees a
    consistent snapshot."""

    entries: tuple[GazetteerEntry, ...]
    name_index: dict[str, GazetteerEntry]
    phonetic_index: dict[str, tuple[GazetteerEntry, ...]]
    district_index: dict[str, tuple[GazetteerEntry, ...]]
    state_index: dict[str, tuple[GazetteerEntry, ...]]

    def size(self) -> int:
        return len(self.entries)

    def by_district(self, district: str) -> tuple[GazetteerEntry, ...]:
        """Return all entries in `district`. Case-insensitive; empty
        tuple when the district isn't in the gazetteer."""
        key = normalise_ascii(district)
        return self.district_index.get(key, ())

    def by_state(self, state: str) -> tuple[GazetteerEntry, ...]:
        key = normalise_ascii(state)
        return self.state_index.get(key, ())


# ─── Loaders ─────────────────────────────────────────────────────────


def load_districts(path: Path) -> list[GazetteerEntry]:
    """Parse the districts.json file. Districts have no `district`
    field on their own rows (they ARE the district)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: list[GazetteerEntry] = []
    for row in raw.get("districts", []):
        name = row["name"]
        state = row["state"]
        variants = tuple(row.get("variants", []))
        out.append(
            GazetteerEntry(
                canonical_name=name,
                matched_name=name,
                kind="district",
                district=None,
                state=state,
                lat=None,
                lon=None,
                variants=variants,
            )
        )
    return out


def load_mandals(path: Path) -> list[GazetteerEntry]:
    """Parse the mandals.json file (produced by
    `scripts/build_mandal_gazetteer.py`). Mandal rows carry their
    parent district."""
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: list[GazetteerEntry] = []
    for row in raw.get("mandals", []):
        name = row["name"]
        district = row.get("district")
        state = row["state"]
        variants = tuple(row.get("variants", []))
        out.append(
            GazetteerEntry(
                canonical_name=name,
                matched_name=name,
                kind="mandal",
                district=district,
                state=state,
                lat=None,
                lon=None,
                variants=variants,
            )
        )
    return out


def load_pois(path: Path) -> list[GazetteerEntry]:
    """Parse the coastal_pois.json file. Rows carry `kind` (beach,
    port, temple, landmark). Invalid `kind` values raise loud at load
    so a typo in the JSON never silently creates an untyped POI."""
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: list[GazetteerEntry] = []
    for row in raw.get("pois", []):
        kind = row.get("kind", "landmark")
        if kind not in _ALLOWED_KINDS:
            raise ValueError(f"POI {row.get('name')!r} has unknown kind {kind!r}")
        out.append(
            GazetteerEntry(
                canonical_name=row["name"],
                matched_name=row["name"],
                kind=kind,
                district=row.get("district"),
                state=row["state"],
                lat=row.get("lat"),
                lon=row.get("lon"),
                variants=tuple(row.get("variants", [])),
            )
        )
    return out


# ─── Index build ─────────────────────────────────────────────────────


def build_gazetteer(entries: list[GazetteerEntry]) -> Gazetteer:
    """Index the entry list. Explodes variants into their own
    lookup keys (each pointing back to the canonical entry so
    prompts + reports always show the canonical form)."""
    exploded: list[GazetteerEntry] = []
    name_idx: dict[str, GazetteerEntry] = {}
    phon_idx: dict[str, list[GazetteerEntry]] = defaultdict(list)
    district_idx: dict[str, list[GazetteerEntry]] = defaultdict(list)
    state_idx: dict[str, list[GazetteerEntry]] = defaultdict(list)

    for entry in entries:
        exploded.append(entry)
        _register(entry, entry.canonical_name, name_idx, phon_idx)
        for variant in entry.variants:
            # Variant rows share the canonical fields; matched_name
            # is the alias that hit. Kept as a distinct entry in the
            # `entries` tuple so the resolver's `by_district` /
            # `by_state` still surfaces them when the caller uses an
            # alias-y phrasing.
            aliased = GazetteerEntry(
                canonical_name=entry.canonical_name,
                matched_name=variant,
                kind=entry.kind,
                district=entry.district,
                state=entry.state,
                lat=entry.lat,
                lon=entry.lon,
                variants=(),
            )
            _register(aliased, variant, name_idx, phon_idx)
            exploded.append(aliased)
        # District / state indices key on the entry's own district /
        # state (not on the aliased variant), so the geographic prior
        # counts each canonical row once per district.
        if entry.district:
            district_idx[normalise_ascii(entry.district)].append(entry)
        else:
            district_idx[normalise_ascii(entry.canonical_name)].append(entry)
        state_idx[normalise_ascii(entry.state)].append(entry)

    return Gazetteer(
        entries=tuple(exploded),
        name_index=name_idx,
        phonetic_index={k: tuple(v) for k, v in phon_idx.items()},
        district_index={k: tuple(v) for k, v in district_idx.items()},
        state_index={k: tuple(v) for k, v in state_idx.items()},
    )


def _register(
    entry: GazetteerEntry,
    surface_form: str,
    name_idx: dict[str, GazetteerEntry],
    phon_idx: dict[str, list[GazetteerEntry]],
) -> None:
    """Register `surface_form` → entry in both the exact and phonetic
    indices. Exact-match collisions are resolved FIRST-WIN — earlier
    entries win. Combined with the load order (districts → mandals
    → POIs in `load_full_gazetteer`), this means a caller saying
    "Kakinada" resolves to the DISTRICT (the containing area) rather
    than a mandal or POI that happens to share the name. Compound
    POI names like "Kakinada Beach" normalise to a distinct key
    (`kakinadabeach`) and don't collide."""
    normal = normalise_ascii(surface_form)
    if normal and normal not in name_idx:
        name_idx[normal] = entry
    # Phonetic index is multi-value; still append every hit so the
    # resolver considers all candidates.
    primary, secondary = phonetic_keys(surface_form)
    if primary:
        phon_idx[primary].append(entry)
    if secondary and secondary != primary:
        phon_idx[secondary].append(entry)


def load_full_gazetteer(
    *,
    districts_path: Path,
    mandals_path: Path | None,
    pois_path: Path | None,
) -> Gazetteer:
    """Convenience for the boot path: load all three JSON tiers into
    one indexed gazetteer. Any missing file (mandals, POIs) is
    silently skipped — a deploy without them is degraded, not broken.
    Districts are required (raises if the file's missing)."""
    if not districts_path.exists():
        raise FileNotFoundError(f"districts gazetteer missing: {districts_path}")
    entries: list[GazetteerEntry] = []
    entries.extend(load_districts(districts_path))
    if mandals_path is not None:
        entries.extend(load_mandals(mandals_path))
    if pois_path is not None:
        entries.extend(load_pois(pois_path))
    return build_gazetteer(entries)


__all__ = [
    "Gazetteer",
    "GazetteerEntry",
    "build_gazetteer",
    "load_districts",
    "load_full_gazetteer",
    "load_mandals",
    "load_pois",
]
