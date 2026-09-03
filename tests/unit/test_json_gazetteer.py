"""JsonGazetteerGeocoder — district-level fuzzy resolver.

Covers:
- Exact canonical name (case-insensitive)
- Exact variant name (Vizag → Visakhapatnam)
- Whole-word substring match ("RK Beach, Vizag" → Visakhapatnam)
- Longest-form-wins tiebreak ("West Godavari district" → West Godavari,
  not Godavari)
- Fuzzy typo (Vishakapatnam → Visakhapatnam)
- Threshold gate — below cutoff returns None, doesn't guess
- Empty / whitespace returns None
- Loader validates the JSON shape
- Loader loads the real bundled gazetteer end-to-end
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fg_voice.enrichment.geocoders.json_gazetteer import (
    DEFAULT_MIN_SCORE,
    DistrictEntry,
    JsonGazetteerGeocoder,
    build_gazetteer_geocoder,
    load_gazetteer,
)


def _sample_geocoder(min_score: int = DEFAULT_MIN_SCORE) -> JsonGazetteerGeocoder:
    """Small in-memory geocoder — quicker + more predictable than the
    bundled 59-district file for unit tests."""
    entries = (
        DistrictEntry(
            name="Visakhapatnam",
            state="Andhra Pradesh",
            variants=("Vizag", "Visakhapatnam District"),
        ),
        DistrictEntry(name="Vizianagaram", state="Andhra Pradesh", variants=()),
        DistrictEntry(
            name="West Godavari",
            state="Andhra Pradesh",
            variants=("West Godavari District", "Bhimavaram"),
        ),
        DistrictEntry(name="East Godavari", state="Andhra Pradesh", variants=()),
        DistrictEntry(
            name="Hanumakonda",
            state="Telangana",
            variants=("Hanamkonda", "Warangal Urban"),
        ),
        DistrictEntry(name="Warangal", state="Telangana", variants=("Warangal Rural",)),
    )
    return JsonGazetteerGeocoder(entries=entries, min_score=min_score)


# ─── Exact match ─────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Visakhapatnam", "Visakhapatnam, Andhra Pradesh"),
        ("visakhapatnam", "Visakhapatnam, Andhra Pradesh"),
        ("  Warangal  ", "Warangal, Telangana"),
        ("HANUMAKONDA", "Hanumakonda, Telangana"),
    ],
)
async def test_exact_name_case_insensitive(raw, expected):
    geo = _sample_geocoder()
    assert await geo.resolve(raw) == expected


@pytest.mark.asyncio
async def test_exact_variant_match():
    geo = _sample_geocoder()
    assert await geo.resolve("Vizag") == "Visakhapatnam, Andhra Pradesh"
    assert await geo.resolve("Hanamkonda") == "Hanumakonda, Telangana"


# ─── Whole-word substring ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_substring_with_landmark():
    """A caller-typical utterance with a landmark + city."""
    geo = _sample_geocoder()
    assert await geo.resolve("RK Beach, Vizag") == "Visakhapatnam, Andhra Pradesh"


@pytest.mark.asyncio
async def test_longest_form_wins_on_substring():
    """'West Godavari' must beat 'Godavari' or 'East Godavari' when
    both appear as candidates in the raw string."""
    geo = _sample_geocoder()
    result = await geo.resolve("West Godavari district office collapsed")
    assert result == "West Godavari, Andhra Pradesh"


@pytest.mark.asyncio
async def test_word_boundary_prevents_partial_match():
    """'vizagerpalli' must NOT match 'vizag' via substring — the
    word-boundary check prevents mid-word matches."""
    geo = _sample_geocoder()
    # 'vizagerpalli' isn't in the gazetteer at all, and the fuzzy
    # WRatio between 'vizagerpalli' and 'Vizag' is 55 (below 80).
    # So this resolves to None.
    result = await geo.resolve("vizagerpalli hazard")
    assert result is None


# ─── Fuzzy typo ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_typo_resolves_to_nearest():
    """A common misspelling should still resolve — 'Vishakapatnam'
    scores ~92 against 'Visakhapatnam'."""
    geo = _sample_geocoder()
    assert await geo.resolve("Vishakapatnam") == "Visakhapatnam, Andhra Pradesh"


@pytest.mark.asyncio
async def test_transposition_resolves():
    """WRatio handles minor transpositions well — 'Wargangal' scores
    ~94 against 'Warangal'. (An extra suffix like ' district' would
    drag WRatio below the 80 cutoff — that's a known gazetteer
    limitation, not a bug.)"""
    geo = _sample_geocoder()
    result = await geo.resolve("Wargangal")
    assert result == "Warangal, Telangana"


# ─── Threshold gate ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_below_threshold_returns_none():
    """Random unrelated string shouldn't fabricate a match — leaving
    location_resolved NULL is safer than routing an alert to the
    wrong district."""
    geo = _sample_geocoder()
    assert await geo.resolve("qwertyuiop random noise") is None


@pytest.mark.asyncio
async def test_custom_threshold_respected():
    """A caller who set min_score=95 gets stricter matching. 'Vishakapatnam'
    at ~92 should still fail the 95 gate."""
    strict = _sample_geocoder(min_score=95)
    assert await strict.resolve("Vishakapatnam") is None
    # But an exact match still lands (score 100).
    assert await strict.resolve("Vizag") == "Visakhapatnam, Andhra Pradesh"


# ─── Empty / whitespace ──────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["", "   ", "\n\t"])
async def test_empty_returns_none(raw):
    geo = _sample_geocoder()
    assert await geo.resolve(raw) is None


# ─── Loader ──────────────────────────────────────────────────────────


def test_load_gazetteer_happy_path(tmp_path):
    doc = {
        "schema_version": 1,
        "districts": [
            {"name": "Foo", "state": "Bar", "variants": ["Foobar"]},
            {"name": "Baz", "state": "Qux", "variants": []},
        ],
    }
    p = tmp_path / "g.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    entries = load_gazetteer(p)
    assert len(entries) == 2
    assert entries[0].name == "Foo"
    assert entries[0].variants == ("Foobar",)


def test_load_gazetteer_missing_key_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text('{"not_districts": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="districts"):
        load_gazetteer(p)


def test_load_gazetteer_empty_districts_raises(tmp_path):
    p = tmp_path / "empty.json"
    p.write_text('{"districts": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        load_gazetteer(p)


def test_load_gazetteer_malformed_row_raises(tmp_path):
    p = tmp_path / "malformed.json"
    p.write_text(
        '{"districts": [{"name": "Foo"}]}',  # missing state
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="malformed"):
        load_gazetteer(p)


def test_build_gazetteer_geocoder_wires_end_to_end(tmp_path):
    doc = {"districts": [{"name": "Foo", "state": "Bar", "variants": []}]}
    p = tmp_path / "g.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    geo = build_gazetteer_geocoder(p, min_score=90)
    assert isinstance(geo, JsonGazetteerGeocoder)
    assert geo.min_score == 90


# ─── Real bundled gazetteer smoke test ───────────────────────────────


BUNDLED_PATH = Path(__file__).resolve().parents[2] / "data" / "gazetteer" / "districts.json"


@pytest.mark.asyncio
async def test_bundled_gazetteer_resolves_representative_cases():
    """Load the real districts.json shipped in the repo and hit a
    handful of realistic caller utterances. Catches regressions in
    the JSON file (missing districts, broken variants) as well as
    the geocoder."""
    if not BUNDLED_PATH.exists():
        pytest.skip(f"Bundled gazetteer not found at {BUNDLED_PATH}")
    geo = build_gazetteer_geocoder(BUNDLED_PATH)

    # Sanity: canonical names on both sides of the region.
    assert await geo.resolve("Hyderabad") == "Hyderabad, Telangana"
    assert await geo.resolve("Guntur") == "Guntur, Andhra Pradesh"

    # Variant → canonical.
    assert await geo.resolve("Vizag") == "Visakhapatnam, Andhra Pradesh"
    assert await geo.resolve("Cuddapah") == "YSR Kadapa, Andhra Pradesh"
    assert await geo.resolve("Rangareddy") == "Ranga Reddy, Telangana"

    # Landmark + city typical caller utterance.
    assert await geo.resolve("beach near vizag flooding") == "Visakhapatnam, Andhra Pradesh"


@pytest.mark.asyncio
async def test_bundled_gazetteer_returns_none_for_unrelated_locations():
    if not BUNDLED_PATH.exists():
        pytest.skip("Bundled gazetteer not found")
    geo = build_gazetteer_geocoder(BUNDLED_PATH)
    # A location well outside the region shouldn't fabricate a match.
    assert await geo.resolve("Mumbai") is None
    assert await geo.resolve("Chennai") is None


@pytest.mark.asyncio
async def test_bundled_gazetteer_covers_all_59_districts():
    """Every district in the bundled file must resolve to itself when
    its canonical name is passed verbatim."""
    if not BUNDLED_PATH.exists():
        pytest.skip("Bundled gazetteer not found")
    with BUNDLED_PATH.open(encoding="utf-8") as fh:
        data = json.load(fh)
    entries = data["districts"]
    assert len(entries) == 59
    geo = build_gazetteer_geocoder(BUNDLED_PATH)
    for row in entries:
        expected = f"{row['name']}, {row['state']}"
        got = await geo.resolve(row["name"])
        assert got == expected, f"canonical name {row['name']!r} failed to self-resolve"
