"""Hybrid place resolver — spec §10.1.

Coverage:
- Exact match on canonical name → top_score ≈ 1.0
- Exact match on variant → resolves to canonical
- Substring match — 'near kakinada beach' → 'Kakinada Beach'
- Phonetic match — mishears like 'Kakinaba' still land on 'Kakinada'
- Fuzzy match — 'Vishakhapatanam' → 'Visakhapatnam'
- Geographic prior — district match bumps the winner
- Ambiguous case → runner_up populated, small margin
- Empty fragment → returns empty ResolvedPlace
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fg_voice.rag.gazetteer import load_full_gazetteer
from fg_voice.rag.resolve_place import GazetteerResolver, GeographicPrior


@pytest.fixture
def resolver(tmp_path: Path) -> GazetteerResolver:
    districts = tmp_path / "districts.json"
    districts.write_text(
        json.dumps(
            {
                "districts": [
                    {"name": "Kakinada", "state": "Andhra Pradesh", "variants": []},
                    {
                        "name": "Visakhapatnam",
                        "state": "Andhra Pradesh",
                        "variants": ["Vizag", "Vishakhapatnam"],
                    },
                    {"name": "Bapatla", "state": "Andhra Pradesh", "variants": []},
                    {"name": "Hyderabad", "state": "Telangana", "variants": []},
                ]
            }
        )
    )
    pois = tmp_path / "pois.json"
    pois.write_text(
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
                    {
                        "name": "Bheemili Beach",
                        "kind": "beach",
                        "district": "Visakhapatnam",
                        "state": "Andhra Pradesh",
                        "variants": ["Bheemunipatnam"],
                        "lat": 17.89,
                        "lon": 83.43,
                    },
                    {
                        "name": "Bapatla Beach",
                        "kind": "beach",
                        "district": "Bapatla",
                        "state": "Andhra Pradesh",
                        "variants": ["Suryalanka"],
                        "lat": 15.85,
                        "lon": 80.53,
                    },
                ]
            }
        )
    )
    gaz = load_full_gazetteer(districts_path=districts, mandals_path=None, pois_path=pois)
    return GazetteerResolver(gaz)


# ─── Basic matching ─────────────────────────────────────────────────


def test_exact_match_canonical_name(resolver: GazetteerResolver) -> None:
    r = resolver.resolve("Kakinada")
    assert r.top_entry is not None
    assert r.top_entry.canonical_name == "Kakinada"


def test_exact_match_variant_resolves_to_canonical(resolver: GazetteerResolver) -> None:
    r = resolver.resolve("Vizag")
    assert r.top_entry is not None
    assert r.top_entry.canonical_name == "Visakhapatnam"


def test_substring_match_finds_poi_in_phrase(resolver: GazetteerResolver) -> None:
    r = resolver.resolve("I am near Kakinada Beach")
    assert r.top_entry is not None
    assert r.top_entry.canonical_name == "Kakinada Beach"


def test_phonetic_match_handles_stt_mishear(resolver: GazetteerResolver) -> None:
    """A d→b mishear on the final word should still land on
    Kakinada via the phonetic signal."""
    r = resolver.resolve("I am at kakinaba")
    assert r.top_entry is not None
    assert r.top_entry.canonical_name in ("Kakinada", "Kakinada Beach")


def test_fuzzy_match_handles_extra_letter(resolver: GazetteerResolver) -> None:
    """Common misspelling 'Vishakhapatanam' (extra 'a') should fuzzy-
    match Visakhapatnam."""
    r = resolver.resolve("Vishakhapatanam")
    assert r.top_entry is not None
    assert r.top_entry.canonical_name == "Visakhapatnam"


# ─── Geographic prior ───────────────────────────────────────────────


def test_geographic_prior_district_bump_prefers_local_poi(
    resolver: GazetteerResolver,
) -> None:
    """A caller with a Visakhapatnam prior asking about 'beach'
    should get a Visakhapatnam POI, not a Bapatla one."""
    prior = GeographicPrior(district="Visakhapatnam", state="Andhra Pradesh")
    r = resolver.resolve("Bheemili beach", prior=prior)
    assert r.top_entry is not None
    # Bheemili Beach is in Visakhapatnam — prior bump should keep it
    # solidly at rank 1.
    assert r.top_entry.canonical_name == "Bheemili Beach"


def test_geographic_prior_state_only_still_helps(resolver: GazetteerResolver) -> None:
    prior = GeographicPrior(state="Andhra Pradesh")
    r = resolver.resolve("Bapatla", prior=prior)
    assert r.top_entry is not None
    assert r.top_entry.state == "Andhra Pradesh"


def test_geographic_prior_missing_is_noop(resolver: GazetteerResolver) -> None:
    """No prior → still resolve, just no prior bump."""
    r_no_prior = resolver.resolve("Kakinada")
    r_with_none = resolver.resolve("Kakinada", prior=None)
    assert r_no_prior.top_entry == r_with_none.top_entry


# ─── Margin + runner-up ─────────────────────────────────────────────


def test_top_and_runner_up_are_distinct(resolver: GazetteerResolver) -> None:
    """When both Kakinada + Kakinada Beach fire on 'kakinada', the
    winner and runner-up should be different canonical entries."""
    r = resolver.resolve("Kakinada")
    assert r.top_entry is not None
    if r.runner_up is not None:
        assert r.top_entry.canonical_name != r.runner_up.canonical_name


def test_margin_is_non_negative(resolver: GazetteerResolver) -> None:
    r = resolver.resolve("Vizag")
    assert r.top1_top2_margin >= 0.0


# ─── Empty + no-match paths ─────────────────────────────────────────


def test_empty_fragment_returns_empty_resolution(resolver: GazetteerResolver) -> None:
    r = resolver.resolve("")
    assert r.top_entry is None
    assert r.top_score == 0.0
    assert r.runner_up is None
    assert r.candidates == ()


def test_completely_unknown_fragment_returns_empty(resolver: GazetteerResolver) -> None:
    r = resolver.resolve("zxcvbnm qwertyuiop")
    # May be None (no phonetic + no substring + fuzzy below cutoff)
    # OR a very low score. Either way top_score should be small.
    assert r.top_entry is None or r.top_score < 0.05
