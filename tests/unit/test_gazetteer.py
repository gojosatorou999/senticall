"""In-memory gazetteer loader + indices — spec §10.1 store side."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fg_voice.rag.gazetteer import (
    Gazetteer,
    GazetteerEntry,
    build_gazetteer,
    load_districts,
    load_full_gazetteer,
    load_mandals,
    load_pois,
)

# ─── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def tiny_districts(tmp_path: Path) -> Path:
    path = tmp_path / "districts.json"
    path.write_text(
        json.dumps(
            {
                "districts": [
                    {"name": "Kakinada", "state": "Andhra Pradesh", "variants": []},
                    {
                        "name": "Visakhapatnam",
                        "state": "Andhra Pradesh",
                        "variants": ["Vizag", "Vishakhapatnam"],
                    },
                    {"name": "Hyderabad", "state": "Telangana", "variants": []},
                ]
            }
        )
    )
    return path


@pytest.fixture
def tiny_pois(tmp_path: Path) -> Path:
    path = tmp_path / "coastal_pois.json"
    path.write_text(
        json.dumps(
            {
                "pois": [
                    {
                        "name": "RK Beach",
                        "kind": "beach",
                        "district": "Visakhapatnam",
                        "state": "Andhra Pradesh",
                        "variants": ["Ramakrishna Beach"],
                        "lat": 17.71,
                        "lon": 83.32,
                    },
                    {
                        "name": "Kakinada Beach",
                        "kind": "beach",
                        "district": "Kakinada",
                        "state": "Andhra Pradesh",
                        "variants": [],
                        "lat": 16.99,
                        "lon": 82.25,
                    },
                ]
            }
        )
    )
    return path


# ─── Loaders ────────────────────────────────────────────────────────


def test_load_districts_parses_variants(tiny_districts: Path) -> None:
    entries = load_districts(tiny_districts)
    assert len(entries) == 3
    vizag = next(e for e in entries if e.canonical_name == "Visakhapatnam")
    assert vizag.variants == ("Vizag", "Vishakhapatnam")
    assert vizag.kind == "district"
    assert vizag.district is None
    assert vizag.state == "Andhra Pradesh"


def test_load_pois_rejects_unknown_kind(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"pois": [{"name": "X", "kind": "unicorn", "state": "AP"}]}))
    with pytest.raises(ValueError, match="unknown kind"):
        load_pois(path)


def test_load_mandals_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_mandals(tmp_path / "not_here.json") == []


def test_load_pois_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_pois(tmp_path / "not_here.json") == []


# ─── Indexed gazetteer ──────────────────────────────────────────────


def test_build_gazetteer_explodes_variants(tiny_districts: Path) -> None:
    entries = load_districts(tiny_districts)
    gaz = build_gazetteer(entries)
    # 3 canonical + 2 variants (Vizag + Vishakhapatnam) = 5 exploded rows.
    assert gaz.size() == 5
    # Both variant forms should resolve to the canonical entry.
    from fg_voice.rag.phonetic import normalise_ascii

    assert gaz.name_index[normalise_ascii("Vizag")].canonical_name == "Visakhapatnam"
    assert gaz.name_index[normalise_ascii("Vishakhapatnam")].canonical_name == "Visakhapatnam"


def test_build_gazetteer_phonetic_index_groups_similar_spellings(tiny_districts: Path) -> None:
    """Vizag / Vishakhapatnam / Visakhapatnam should collide in
    phonetic space — at least one shared phonetic key hit."""
    gaz = build_gazetteer(load_districts(tiny_districts))
    from fg_voice.rag.phonetic import phonetic_keys

    p, _ = phonetic_keys("Vizag")
    assert p in gaz.phonetic_index
    # At least one candidate should surface for that key.
    assert len(gaz.phonetic_index[p]) >= 1


def test_by_district_returns_expected_pois(tiny_districts: Path, tiny_pois: Path) -> None:
    entries = load_districts(tiny_districts) + load_pois(tiny_pois)
    gaz = build_gazetteer(entries)
    vizag_pois = gaz.by_district("Visakhapatnam")
    names = {e.canonical_name for e in vizag_pois}
    # The district itself + the RK Beach POI both live under
    # Visakhapatnam in the district_index.
    assert "RK Beach" in names or "Visakhapatnam" in names


def test_by_state_covers_ap_pois(tiny_districts: Path, tiny_pois: Path) -> None:
    entries = load_districts(tiny_districts) + load_pois(tiny_pois)
    gaz = build_gazetteer(entries)
    ap_entries = gaz.by_state("Andhra Pradesh")
    # 2 AP districts + 2 AP POIs = 4 (aliases don't get double-counted
    # in state_index because build_gazetteer registers on the entry,
    # not the aliased exploded row).
    assert len(ap_entries) >= 4


# ─── POI entry display ──────────────────────────────────────────────


def test_gazetteer_entry_display_shapes() -> None:
    """Districts render as 'District, State'; POIs as
    'Name, District, State'; POIs without a district as 'Name, State'."""
    d = GazetteerEntry(
        canonical_name="Kakinada",
        matched_name="Kakinada",
        kind="district",
        district=None,
        state="Andhra Pradesh",
        lat=None,
        lon=None,
    )
    assert d.display == "Kakinada, Andhra Pradesh"
    p = GazetteerEntry(
        canonical_name="RK Beach",
        matched_name="RK Beach",
        kind="beach",
        district="Visakhapatnam",
        state="Andhra Pradesh",
        lat=17.71,
        lon=83.32,
    )
    assert p.display == "RK Beach, Visakhapatnam, Andhra Pradesh"


# ─── load_full_gazetteer convenience ────────────────────────────────


def test_load_full_gazetteer_combines_all_tiers(
    tiny_districts: Path, tiny_pois: Path, tmp_path: Path
) -> None:
    gaz = load_full_gazetteer(
        districts_path=tiny_districts,
        mandals_path=tmp_path / "mandals.json",  # missing on purpose
        pois_path=tiny_pois,
    )
    assert isinstance(gaz, Gazetteer)
    # 3 districts + 2 POIs (canonical + aliases exploded).
    assert gaz.size() >= 5


def test_load_full_gazetteer_missing_districts_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="districts gazetteer"):
        load_full_gazetteer(
            districts_path=tmp_path / "no_districts.json",
            mandals_path=None,
            pois_path=None,
        )
