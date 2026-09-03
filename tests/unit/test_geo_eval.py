"""Regression pinning of the geo-resolution eval so a stray edit to
the resolver, scorer, or seed data can't silently drop the score
below the P4 exit gate."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT_FOR_SCRIPTS = Path(__file__).parent.parent.parent
if str(_REPO_ROOT_FOR_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT_FOR_SCRIPTS))

from scripts.run_geo_eval import (
    _read_fragments,
    check_exit_gate,
    run_eval,
)

from fg_voice.rag.gazetteer import load_full_gazetteer
from fg_voice.rag.resolve_place import GazetteerResolver

_REPO_ROOT = Path(__file__).parent.parent.parent


def test_geo_eval_still_meets_exit_gate() -> None:
    """Full-corpus regression: any resolver change that regresses the
    P4 exit gate should fail here loudly."""
    fragments = _read_fragments(_REPO_ROOT / "data/eval/geo/fragments.json")
    gaz = load_full_gazetteer(
        districts_path=_REPO_ROOT / "data/gazetteer/districts.json",
        mandals_path=_REPO_ROOT / "data/gazetteer/mandals.json",
        pois_path=_REPO_ROOT / "data/gazetteer/coastal_pois.json",
    )
    resolver = GazetteerResolver(gaz)
    _, stats = run_eval(resolver, fragments)
    passed, failures = check_exit_gate(stats)
    assert passed, f"P4 geo exit gate regressed: {failures} stats={stats}"


def test_geo_eval_latency_under_budget() -> None:
    """p95 resolve latency must stay under the spec §10.1 budget of
    25 ms even on the full test corpus. A regression here (say, a
    O(N^2) rewrite) would fail this within one dev iteration."""
    fragments = _read_fragments(_REPO_ROOT / "data/eval/geo/fragments.json")
    gaz = load_full_gazetteer(
        districts_path=_REPO_ROOT / "data/gazetteer/districts.json",
        mandals_path=_REPO_ROOT / "data/gazetteer/mandals.json",
        pois_path=_REPO_ROOT / "data/gazetteer/coastal_pois.json",
    )
    resolver = GazetteerResolver(gaz)
    _, stats = run_eval(resolver, fragments)
    assert stats["resolve_p95_ms"] < 25.0, f"latency regressed: {stats['resolve_p95_ms']}ms"
