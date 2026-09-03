"""Mandal-level gazetteer — extends JsonGazetteerGeocoder with
sub-district entries.

Covers:
- Mandal exact match returns 3-part "Mandal, District, State"
- Mandal substring match inside a caller utterance
- Fuzzy typo on a mandal name
- **District beats mandal** on exact-name collision — a caller
  saying "Guntur" gets the district (safer coarser answer) even
  though "Guntur" is also a mandal name
- District's substring/fuzzy match still works when a mandal name
  appears elsewhere in the raw string
- Loader: happy path, missing top-level key, empty list, malformed
  row, missing `district` field
- build_gazetteer_geocoder_with_mandals with None mandals_path
  degrades to district-only (no error)
- Bundled districts + bundled mandals: smoke-test that a
  representative mandal resolves correctly end-to-end
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fg_voice.enrichment.geocoders.json_gazetteer import (
    DistrictEntry,
    JsonGazetteerGeocoder,
    build_gazetteer_geocoder_with_mandals,
    load_mandal_gazetteer,
)

REPO = Path(__file__).resolve().parents[2]
BUNDLED_DISTRICTS = REPO / "data" / "gazetteer" / "districts.json"
BUNDLED_MANDALS = REPO / "data" / "gazetteer" / "mandals.json"


def _sample_geocoder() -> JsonGazetteerGeocoder:
    """Small mixed geocoder — districts + mandals — for predictable
    unit tests. Includes deliberate name collisions:
    - "Guntur" is both a district AND a mandal (in Guntur district)
    - "Kakinada" is both a district AND a mandal (in Kakinada district)
    """
    entries = (
        # Districts
        DistrictEntry(name="Guntur", state="Andhra Pradesh", variants=()),
        DistrictEntry(name="Kakinada", state="Andhra Pradesh", variants=()),
        DistrictEntry(
            name="East Godavari",
            state="Andhra Pradesh",
            variants=("East Godavari District",),
        ),
        DistrictEntry(name="Visakhapatnam", state="Andhra Pradesh", variants=("Vizag",)),
        # Mandals
        DistrictEntry(
            name="Anaparthi",
            state="Andhra Pradesh",
            district="East Godavari",
            variants=(),
        ),
        DistrictEntry(
            name="Guntur",  # mandal collision with district above
            state="Andhra Pradesh",
            district="Guntur",
            variants=(),
        ),
        DistrictEntry(
            name="Kakinada",  # mandal collision with district above
            state="Andhra Pradesh",
            district="Kakinada",
            variants=(),
        ),
        DistrictEntry(
            name="Bapatla",
            state="Andhra Pradesh",
            district="Bapatla",
            variants=(),
        ),
    )
    return JsonGazetteerGeocoder(entries=entries)


# ─── DistrictEntry: 2-part vs 3-part resolved format ─────────────────


def test_district_resolved_is_two_part():
    e = DistrictEntry(name="Guntur", state="Andhra Pradesh", variants=())
    assert e.resolved() == "Guntur, Andhra Pradesh"
    assert e.is_mandal is False


def test_mandal_resolved_is_three_part():
    e = DistrictEntry(
        name="Anaparthi",
        state="Andhra Pradesh",
        district="East Godavari",
        variants=(),
    )
    assert e.resolved() == "Anaparthi, East Godavari, Andhra Pradesh"
    assert e.is_mandal is True


# ─── Mandal exact match ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mandal_exact_match_returns_three_part():
    geo = _sample_geocoder()
    result = await geo.resolve("Anaparthi")
    assert result == "Anaparthi, East Godavari, Andhra Pradesh"


@pytest.mark.asyncio
async def test_mandal_exact_match_case_insensitive():
    geo = _sample_geocoder()
    assert await geo.resolve("anaparthi") == "Anaparthi, East Godavari, Andhra Pradesh"
    assert await geo.resolve("  BAPATLA  ") == "Bapatla, Bapatla, Andhra Pradesh"


# ─── Collision: district wins ────────────────────────────────────────


@pytest.mark.asyncio
async def test_district_wins_on_exact_name_collision_guntur():
    """'Guntur' is a district AND a mandal — the district wins as
    the safer coarser answer."""
    geo = _sample_geocoder()
    result = await geo.resolve("Guntur")
    assert result == "Guntur, Andhra Pradesh"  # 2-part → district


@pytest.mark.asyncio
async def test_district_wins_on_exact_name_collision_kakinada():
    geo = _sample_geocoder()
    result = await geo.resolve("Kakinada")
    assert result == "Kakinada, Andhra Pradesh"  # 2-part → district


# ─── Mandal substring match ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_mandal_matches_inside_landmark_utterance():
    geo = _sample_geocoder()
    result = await geo.resolve("flooding near Anaparthi bridge")
    assert result == "Anaparthi, East Godavari, Andhra Pradesh"


@pytest.mark.asyncio
async def test_longer_district_beats_mandal_substring():
    """'East Godavari District' as a raw string should match the
    district (longer form wins), not the Anaparthi mandal that lives
    inside East Godavari."""
    geo = _sample_geocoder()
    result = await geo.resolve("East Godavari District")
    assert result == "East Godavari, Andhra Pradesh"


# ─── Mandal fuzzy ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mandal_fuzzy_typo():
    """'Anparthi' scores >80 vs 'Anaparthi' via WRatio."""
    geo = _sample_geocoder()
    result = await geo.resolve("Anparthi")
    assert result == "Anaparthi, East Godavari, Andhra Pradesh"


# ─── Loader ──────────────────────────────────────────────────────────


def test_load_mandal_gazetteer_happy_path(tmp_path):
    doc = {
        "mandals": [
            {"name": "Foo", "district": "Bar", "state": "Baz", "variants": ["Fooby"]},
            {"name": "Qux", "district": "Bar", "state": "Baz"},
        ]
    }
    p = tmp_path / "m.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    entries = load_mandal_gazetteer(p)
    assert len(entries) == 2
    assert entries[0].name == "Foo"
    assert entries[0].district == "Bar"
    assert entries[0].variants == ("Fooby",)
    assert entries[0].is_mandal is True


def test_load_mandal_missing_key_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text('{"not_mandals": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="mandals"):
        load_mandal_gazetteer(p)


def test_load_mandal_empty_list_raises(tmp_path):
    p = tmp_path / "empty.json"
    p.write_text('{"mandals": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        load_mandal_gazetteer(p)


def test_load_mandal_missing_district_raises(tmp_path):
    """A mandal row without `district` is a schema violation."""
    p = tmp_path / "malformed.json"
    p.write_text(
        '{"mandals": [{"name": "Foo", "state": "Bar"}]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="malformed"):
        load_mandal_gazetteer(p)


# ─── Optional mandal loading ─────────────────────────────────────────


def test_build_without_mandals_degrades_to_districts_only(tmp_path):
    """`mandals_path=None` → district-only, no error."""
    districts_doc = {"districts": [{"name": "Foo", "state": "Bar", "variants": []}]}
    dp = tmp_path / "d.json"
    dp.write_text(json.dumps(districts_doc), encoding="utf-8")
    geo = build_gazetteer_geocoder_with_mandals(dp, None)
    assert isinstance(geo, JsonGazetteerGeocoder)
    # No mandals loaded → all entries are districts
    assert all(not e.is_mandal for e in geo.entries)


def test_build_with_mandals_merges_corpus(tmp_path):
    districts_doc = {"districts": [{"name": "Foo", "state": "Bar", "variants": []}]}
    mandals_doc = {"mandals": [{"name": "Sub", "district": "Foo", "state": "Bar", "variants": []}]}
    dp = tmp_path / "d.json"
    mp = tmp_path / "m.json"
    dp.write_text(json.dumps(districts_doc), encoding="utf-8")
    mp.write_text(json.dumps(mandals_doc), encoding="utf-8")

    geo = build_gazetteer_geocoder_with_mandals(dp, mp)
    assert len(geo.entries) == 2
    assert sum(1 for e in geo.entries if e.is_mandal) == 1


# ─── Real bundled data smoke tests ───────────────────────────────────


@pytest.mark.asyncio
async def test_bundled_mandals_resolve_representative_cases():
    """Load both real files together and hit realistic caller
    utterances. Catches regressions in EITHER JSON (missing
    mandals, broken districts) as well as the geocoder logic."""
    if not BUNDLED_DISTRICTS.exists() or not BUNDLED_MANDALS.exists():
        pytest.skip("Bundled gazetteers not found")

    geo = build_gazetteer_geocoder_with_mandals(BUNDLED_DISTRICTS, BUNDLED_MANDALS)

    # Mandal happy path — Dharmavaram is a mandal in Ananthapuramu and
    # doesn't collide with any district name or variant.
    result = await geo.resolve("Dharmavaram")
    assert result == "Dharmavaram, Ananthapuramu, Andhra Pradesh"

    # District-name collision — "Kakinada" and "Guntur" are both
    # district AND mandal names in the bundled data. District wins.
    assert await geo.resolve("Kakinada") == "Kakinada, Andhra Pradesh"
    assert await geo.resolve("Guntur") == "Guntur, Andhra Pradesh"

    # District matching still works — sanity that we didn't regress.
    assert await geo.resolve("Vizag") == "Visakhapatnam, Andhra Pradesh"


@pytest.mark.asyncio
async def test_bundled_mandals_all_self_resolve():
    """Every mandal in the bundled file resolves when its canonical
    name is passed verbatim — except entries whose name collides with
    a district name (those resolve to the district instead, by design)."""
    if not BUNDLED_DISTRICTS.exists() or not BUNDLED_MANDALS.exists():
        pytest.skip("Bundled gazetteers not found")

    with BUNDLED_DISTRICTS.open(encoding="utf-8") as fh:
        districts_raw = json.load(fh)["districts"]
    # Exclude mandal names that collide with EITHER a district canonical
    # name OR any listed variant — those resolve to the district by
    # the "district wins on collision" rule, not to the mandal.
    district_forms: set[str] = set()
    for d in districts_raw:
        district_forms.add(d["name"].lower())
        for v in d.get("variants", []):
            district_forms.add(v.lower())

    with BUNDLED_MANDALS.open(encoding="utf-8") as fh:
        mandals = json.load(fh)["mandals"]

    geo = build_gazetteer_geocoder_with_mandals(BUNDLED_DISTRICTS, BUNDLED_MANDALS)

    non_colliding = [m for m in mandals if m["name"].lower() not in district_forms]
    assert non_colliding, "expected at least some non-colliding mandals"

    for m in non_colliding[:20]:  # spot-check first 20 to keep test fast
        expected = f"{m['name']}, {m['district']}, {m['state']}"
        got = await geo.resolve(m["name"])
        assert got == expected, f"{m['name']!r} → {got!r}, expected {expected!r}"
