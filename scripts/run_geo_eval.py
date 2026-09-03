"""Geo-resolution eval — spec §10.1 exit gate.

Runs the hybrid resolver against `data/eval/geo/fragments.json` and
reports:

- accuracy (top_entry.canonical_name == expected)
- accuracy_at_high_confidence (score >= 0.85) — the number the spec
  gates on (≥ 90%)
- confidently_wrong_rate (score >= 0.85 AND wrong) — the DANGEROUS
  failure mode. Spec gate: ≤ 5%.
- p95 resolve latency — spec gate ≤ 25 ms.

Usage:
    python -m scripts.run_geo_eval \\
        --fragments data/eval/geo/fragments.json \\
        --districts data/gazetteer/districts.json \\
        --pois data/gazetteer/coastal_pois.json \\
        --mandals data/gazetteer/mandals.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from fg_voice.rag.gazetteer import load_full_gazetteer
from fg_voice.rag.resolve_place import GazetteerResolver, GeographicPrior

# Confidence cutoff for the "high-confidence" accuracy bucket.
_HIGH_CONF_CUTOFF: float = 0.85


@dataclass(slots=True)
class TrialOutcome:
    id: str
    text: str
    expected: str | None
    predicted: str | None
    score: float
    high_confidence: bool
    correct: bool
    latency_ms: float


def _read_fragments(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["fragments"]


def _run_trial(resolver: GazetteerResolver, fragment: dict) -> TrialOutcome:
    prior = None
    if fragment.get("prior_district") or fragment.get("prior_state"):
        prior = GeographicPrior(
            district=fragment.get("prior_district"),
            state=fragment.get("prior_state"),
        )
    text = fragment.get("text", "")
    expected = fragment.get("expected")
    t0 = time.perf_counter()
    r = resolver.resolve(text, prior=prior)
    t1 = time.perf_counter()
    predicted = r.top_entry.canonical_name if r.top_entry else None
    high_conf = r.top_score >= _HIGH_CONF_CUTOFF
    correct = predicted == expected
    return TrialOutcome(
        id=fragment["id"],
        text=text,
        expected=expected,
        predicted=predicted,
        score=r.top_score,
        high_confidence=high_conf,
        correct=correct,
        latency_ms=(t1 - t0) * 1000,
    )


def run_eval(
    resolver: GazetteerResolver, fragments: list[dict]
) -> tuple[list[TrialOutcome], dict[str, float]]:
    outcomes = [_run_trial(resolver, f) for f in fragments]
    accuracy = sum(1 for o in outcomes if o.correct) / len(outcomes)
    high_conf = [o for o in outcomes if o.high_confidence]
    accuracy_hc = sum(1 for o in high_conf if o.correct) / len(high_conf) if high_conf else 0.0
    confidently_wrong = sum(1 for o in high_conf if not o.correct) / len(outcomes)
    latencies = sorted(o.latency_ms for o in outcomes)
    p95_idx = max(0, int(len(latencies) * 0.95) - 1)
    p95_ms = latencies[p95_idx] if latencies else 0.0

    stats = {
        "n": len(outcomes),
        "accuracy": accuracy,
        "high_confidence_count": len(high_conf),
        "accuracy_at_high_confidence": accuracy_hc,
        "confidently_wrong_rate": confidently_wrong,
        "resolve_p95_ms": p95_ms,
    }
    return outcomes, stats


def check_exit_gate(
    stats: dict[str, float],
    *,
    min_accuracy_at_high_conf: float = 0.90,
    max_confidently_wrong: float = 0.05,
    max_p95_ms: float = 25.0,
) -> tuple[bool, list[str]]:
    """Spec §10.1 exit gate. Returns (passed, failures)."""
    failures: list[str] = []
    if stats["accuracy_at_high_confidence"] < min_accuracy_at_high_conf:
        failures.append(
            f"accuracy_at_high_confidence={stats['accuracy_at_high_confidence']:.3f} "
            f"< target={min_accuracy_at_high_conf}"
        )
    if stats["confidently_wrong_rate"] > max_confidently_wrong:
        failures.append(
            f"confidently_wrong_rate={stats['confidently_wrong_rate']:.3f} "
            f"> max={max_confidently_wrong}"
        )
    if stats["resolve_p95_ms"] > max_p95_ms:
        failures.append(f"resolve_p95_ms={stats['resolve_p95_ms']:.2f} > max={max_p95_ms}")
    return (len(failures) == 0, failures)


def _print_report(outcomes: list[TrialOutcome], stats: dict[str, float]) -> None:
    print(f"n={stats['n']}")
    print(f"accuracy: {stats['accuracy']:.3f}")
    print(
        f"accuracy_at_high_confidence (>= {_HIGH_CONF_CUTOFF}): "
        f"{stats['accuracy_at_high_confidence']:.3f} "
        f"({stats['high_confidence_count']} of {stats['n']})"
    )
    print(f"confidently_wrong_rate: {stats['confidently_wrong_rate']:.3f}")
    print(f"resolve_p95_ms: {stats['resolve_p95_ms']:.2f}")
    print()
    print("Misses:")
    for o in outcomes:
        if not o.correct:
            print(
                f"  [{o.id}] text={o.text!r} expected={o.expected!r} "
                f"got={o.predicted!r} score={o.score:.2f} conf={o.high_confidence}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Geo-resolution eval")
    parser.add_argument("--fragments", type=Path, required=True)
    parser.add_argument("--districts", type=Path, required=True)
    parser.add_argument("--pois", type=Path)
    parser.add_argument("--mandals", type=Path)
    args = parser.parse_args(argv)

    if not args.fragments.exists():
        print(f"error: fragments file missing: {args.fragments}", file=sys.stderr)
        return 2

    gaz = load_full_gazetteer(
        districts_path=args.districts,
        mandals_path=args.mandals,
        pois_path=args.pois,
    )
    resolver = GazetteerResolver(gaz)
    fragments = _read_fragments(args.fragments)
    outcomes, stats = run_eval(resolver, fragments)
    _print_report(outcomes, stats)
    passed, failures = check_exit_gate(stats)
    if passed:
        print("\n✓ P4 geo exit-gate targets MET.")
        return 0
    print("\n✗ P4 geo exit-gate targets NOT met:")
    for f in failures:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())


# Small dep to keep ruff quiet on unused std imports for the CLI-only
# shape (statistics comes handy for future p50/p99 additions).
_ = statistics
