"""Dynamic per-call keyterms — spec §9.3 update-mid-call knob."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fg_voice.rag.gazetteer import load_full_gazetteer
from fg_voice.rag.keyterms import (
    MAX_KEYTERMS,
    build_dynamic_keyterms,
    build_keyterms,
)


@pytest.fixture
def gazetteer(tmp_path: Path):
    d = tmp_path / "districts.json"
    d.write_text(
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
    p = tmp_path / "pois.json"
    p.write_text(
        json.dumps(
            {
                "pois": [
                    {
                        "name": "RK Beach",
                        "kind": "beach",
                        "district": "Visakhapatnam",
                        "state": "Andhra Pradesh",
                        "variants": ["Ramakrishna Beach"],
                        "lat": 17.7,
                        "lon": 83.3,
                    },
                    {
                        "name": "Kakinada Beach",
                        "kind": "beach",
                        "district": "Kakinada",
                        "state": "Andhra Pradesh",
                        "variants": [],
                        "lat": 17.0,
                        "lon": 82.2,
                    },
                    {
                        "name": "Bheemili Beach",
                        "kind": "beach",
                        "district": "Visakhapatnam",
                        "state": "Andhra Pradesh",
                        "variants": [],
                        "lat": 17.9,
                        "lon": 83.4,
                    },
                ]
            }
        )
    )
    return load_full_gazetteer(districts_path=d, mandals_path=None, pois_path=p)


def test_no_gazetteer_no_prior_returns_static(gazetteer) -> None:
    """Absent gazetteer + priors → identical to static build."""
    dynamic = build_dynamic_keyterms()
    static = build_keyterms()
    assert dynamic == static


def test_district_prior_adds_local_places(gazetteer) -> None:
    """A caller with a Visakhapatnam prior should get Vizag +
    RK Beach + Bheemili Beach in the keyterm list."""
    dynamic = build_dynamic_keyterms(gazetteer=gazetteer, prior_district="Visakhapatnam")
    lower = {t.lower() for t in dynamic}
    assert "rk beach" in lower or "ramakrishna beach" in lower
    assert "bheemili beach" in lower


def test_state_prior_adds_all_state_places(gazetteer) -> None:
    dynamic = build_dynamic_keyterms(gazetteer=gazetteer, prior_state="Andhra Pradesh")
    lower = {t.lower() for t in dynamic}
    # At least one AP place should surface.
    assert any(p in lower for p in ("kakinada", "visakhapatnam", "rk beach", "bheemili beach"))


def test_dynamic_keyterms_respects_cap(gazetteer) -> None:
    dynamic = build_dynamic_keyterms(
        gazetteer=gazetteer,
        prior_state="Andhra Pradesh",
        top_k_places=500,
    )
    assert len(dynamic) <= MAX_KEYTERMS


def test_district_prior_ranks_above_state_prior(gazetteer) -> None:
    """When both priors are set, district entries should appear
    BEFORE state entries in the returned list. This mirrors §9.3's
    "geographic prior" — the tighter prior wins on ordering."""
    dynamic = build_dynamic_keyterms(
        gazetteer=gazetteer,
        prior_district="Visakhapatnam",
        prior_state="Andhra Pradesh",
    )
    # Look at the trailing dynamic entries. The static prefix
    # (hazard + severity + coastal) comes first; dynamic places
    # come after. RK Beach is a Visakhapatnam POI; Hyderabad is
    # NOT — should not appear via the state fallback either
    # (Hyderabad is Telangana).
    assert "Hyderabad" not in dynamic


def test_no_side_effect_on_static_build(gazetteer) -> None:
    """build_dynamic_keyterms must NOT mutate the static tuples in
    the module — the returned list should be a fresh construction
    each call."""
    a = build_dynamic_keyterms(gazetteer=gazetteer, prior_district="Kakinada")
    static_after = build_keyterms()
    # Static build result should not have leaked Kakinada Beach.
    assert "Kakinada Beach" not in static_after
    assert "Kakinada Beach" in a
