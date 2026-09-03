"""Builds `data/gazetteer/mandals.json` from the backend's Survey-of-India
sub-district shapefile.

This is a one-off script — run it after the backend's boundary data
updates. The output JSON gets committed so the runtime path never
depends on the DBF or dbfread.

Input:  ../backend/boundaries/talukas/SUBDISTRICT_BOUNDARY.dbf
Output: data/gazetteer/mandals.json

Filters to Telangana + Andhra Pradesh mandals only. Handles the
canonical district name mapping the same way `districts.json` was
built — the backend's `DISTRICT_VARIANT_MAP` is the source of truth.

The DBF's `STATE_UT` / `DISTRICT` / `SUB_DIST` fields carry mixed
case + occasional trailing whitespace; normalisation matches the
backend loader's `normalize_key` + `canonical_district` behaviour.

Dedupes by (state, district, mandal) — the shapefile has one row per
polygon so a mandal spanning multiple polygons appears multiple times.

Requires the `scripts` dep group:
    uv sync --group=scripts
    uv run python scripts/build_mandal_gazetteer.py
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# ai-calling-agent/ sits next to backend/ in the sentinel/ monorepo.
DBF_PATH = REPO.parent / "backend" / "boundaries" / "talukas" / "SUBDISTRICT_BOUNDARY.dbf"
OUT_PATH = REPO / "data" / "gazetteer" / "mandals.json"

# Kept in sync with backend/apps/geo/management/commands/load_region_boundaries.py.
# Any renames the backend recognises we should recognise too.
TARGET_STATES = {
    "TELANGANA": "Telangana",
    "ANDHRA PRADESH": "Andhra Pradesh",
    "ANDHRAPRADESH": "Andhra Pradesh",
}

DISTRICT_VARIANT_MAP = {
    # Telangana
    "HANAMKONDA": "Hanumakonda",
    "HANUMAKONDA": "Hanumakonda",
    "WARANGAL URBAN": "Hanumakonda",
    "WARANGAL RURAL": "Warangal",
    "WARANGAL": "Warangal",
    "JAYASHANKAR": "Jayashankar Bhupalpally",
    "JAYASHANKAR BHUPALPALLY": "Jayashankar Bhupalpally",
    "BHUPALAPALLI": "Jayashankar Bhupalpally",
    "KOMARAM BHEEM": "Komaram Bheem Asifabad",
    "KOMARAM BHEEM ASIFABAD": "Komaram Bheem Asifabad",
    "ASIFABAD": "Komaram Bheem Asifabad",
    "JOGULAMBA GADWAL": "Jogulamba Gadwal",
    "GADWAL": "Jogulamba Gadwal",
    "BHADRADRI KOTHAGUDEM": "Bhadradri Kothagudem",
    "KOTHAGUDEM": "Bhadradri Kothagudem",
    "MEDCHAL": "Medchal-Malkajgiri",
    "MEDCHAL-MALKAJGIRI": "Medchal-Malkajgiri",
    "MEDCHAL MALKAJGIRI": "Medchal-Malkajgiri",
    "RAJANNA SIRCILLA": "Rajanna Sircilla",
    "SIRCILLA": "Rajanna Sircilla",
    "YADADRI BHUVANAGIRI": "Yadadri Bhuvanagiri",
    "YADADRI": "Yadadri Bhuvanagiri",
    "RANGAREDDY": "Ranga Reddy",
    "RANGA REDDY": "Ranga Reddy",
    "R.R.DIST": "Ranga Reddy",
    # Andhra Pradesh
    "YSR": "YSR Kadapa",
    "KADAPA": "YSR Kadapa",
    "YSR KADAPA": "YSR Kadapa",
    "CUDDAPAH": "YSR Kadapa",
    "SPSR NELLORE": "Sri Potti Sriramulu Nellore",
    "NELLORE": "Sri Potti Sriramulu Nellore",
    "SRI POTTI SRIRAMULU NELLORE": "Sri Potti Sriramulu Nellore",
    "SRI SATHYA SAI": "Sri Sathya Sai",
    "SATHYA SAI": "Sri Sathya Sai",
    "ALLURI SITHARAMA RAJU": "Alluri Sitharama Raju",
    "ALLURI SITARAMA RAJU": "Alluri Sitharama Raju",
    "ASR": "Alluri Sitharama Raju",
    "DR. B. R. AMBEDKAR KONASEEMA": "Konaseema",
    "KONASEEMA": "Konaseema",
    "PARVATHIPURAM MANYAM": "Parvathipuram Manyam",
    "NTR": "NTR",
    "N.T.R.": "NTR",
    "N T R": "NTR",
    "VIZIANAGARAM": "Vizianagaram",
    "VISAKHAPATNAM": "Visakhapatnam",
    "EAST GODAVARI": "East Godavari",
    "WEST GODAVARI": "West Godavari",
    "ANANTAPUR": "Ananthapuramu",
    "ANANTHAPUR": "Ananthapuramu",
    "ANANTAPURAM": "Ananthapuramu",
    "ANANTHAPURAMU": "Ananthapuramu",
    "CHITTOOR": "Chittoor",
}


def _norm(raw: str) -> str:
    return re.sub(r"\s+", " ", (raw or "").strip()).upper()


def _canonical_district(raw: str) -> str:
    key = _norm(raw)
    return DISTRICT_VARIANT_MAP.get(key, key.title())


def _clean_mandal(raw: str) -> str:
    """Trim + collapse whitespace; preserve source casing."""
    return re.sub(r"\s+", " ", (raw or "").strip())


# Junk / placeholder rows in the shapefile — the DBF has entries like
# `N.A. ( 1711)` where the survey couldn't attribute a polygon to a
# named mandal. Filter these out at build time.
_JUNK_RE = re.compile(r"^N\.?\s*A\.?", re.IGNORECASE)


def _is_junk_mandal(name: str) -> bool:
    if not name:
        return True
    if _JUNK_RE.match(name):
        return True
    # Contains parenthesised digits — the survey's polygon-id fallback.
    return bool(re.search(r"\(\s*\d+\s*\)", name))


def main() -> int:
    if not DBF_PATH.exists():
        print(f"DBF not found at {DBF_PATH}", file=sys.stderr)
        return 1
    try:
        from dbfread import DBF
    except ImportError:
        print(
            "dbfread not installed. Run: uv sync --group=scripts",
            file=sys.stderr,
        )
        return 1

    # (state, district, mandal_upper) → mandal_canonical_casing
    seen: dict[tuple[str, str, str], str] = {}
    total_rows = 0
    skipped_states = 0
    for row in DBF(DBF_PATH, encoding="latin-1"):
        total_rows += 1
        state_key = _norm(row.get("STATE_UT", ""))
        state = TARGET_STATES.get(state_key)
        if state is None:
            skipped_states += 1
            continue
        district = _canonical_district(row.get("DISTRICT", ""))
        mandal_raw = _clean_mandal(row.get("SUB_DIST", ""))
        if not district or not mandal_raw:
            continue
        if _is_junk_mandal(mandal_raw):
            continue
        key = (state, district, mandal_raw.upper())
        # First-wins on casing — the DBF has consistent casing per mandal.
        seen.setdefault(key, mandal_raw.title())

    if not seen:
        print("No rows matched TG or AP; DBF may be empty or filter is wrong", file=sys.stderr)
        return 2

    entries = sorted(
        (
            {
                "name": canonical_casing,
                "district": district,
                "state": state,
                "variants": [],
            }
            for (state, district, _upper), canonical_casing in seen.items()
        ),
        key=lambda e: (e["state"], e["district"], e["name"]),
    )
    doc = {
        "schema_version": 1,
        "region": "Telangana + Andhra Pradesh",
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d"),
        "source": (
            "backend/boundaries/talukas/SUBDISTRICT_BOUNDARY.dbf (Survey of India), "
            "district names canonicalised via backend's DISTRICT_VARIANT_MAP"
        ),
        "notes": [
            "First-cut mandal gazetteer. Coverage matches whatever the "
            "backend has loaded in `boundaries/talukas/` — coastal-focused "
            "subset, not every mandal in TG+AP.",
            "`variants` is intentionally empty in this build; hand-curate as "
            "call-review data shows variants worth adding.",
            "The runtime geocoder prefers a district match over a mandal "
            "match for exact-input collisions (e.g. 'Kakinada'), because "
            "the district is the safer coarser answer when the caller "
            "utterance is ambiguous.",
        ],
        "mandals": entries,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    print(
        f"Wrote {len(entries)} mandals to {OUT_PATH.relative_to(REPO)} "
        f"(scanned {total_rows} DBF rows, skipped {skipped_states} out-of-region)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
