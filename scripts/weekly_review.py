"""Weekly accuracy review loop (spec §P9).

Pulls the past 7 days of voice reports from the API and produces a
structured review report covering SLOs from §16.3. Used in the weekly
accuracy review meeting to identify regressions and prioritise golden
set additions.

Outputs
-------
- Submission rate vs 100% target
- Completion rate vs ≥75% target
- Geo resolution rate at ≥0.85 confidence vs ≥90% target
- Hazard type and severity distributions
- DTMF fallback ratio vs ≤25% threshold
- QA sample backlog size
- Recommendations: which prompts/thresholds to tune

Usage
-----
    uv run python scripts/weekly_review.py
    uv run python scripts/weekly_review.py --weeks 2 --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import Any


def _get(base_url: str, path: str, admin_key: str, params: dict | None = None) -> Any:
    import urllib.parse
    import urllib.request

    url = f"{base_url.rstrip('/')}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-Admin-Api-Key": admin_key})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        return {"error": str(exc)}


def _weekly_review(base_url: str, admin_key: str, weeks: int) -> dict[str, Any]:
    since = (datetime.now(UTC) - timedelta(weeks=weeks)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {"limit": 500, "source": "voice", "from": since}

    data = _get(base_url, "/api/v1/reports", admin_key, params)
    if "error" in data:
        return {"error": data["error"]}

    items: list[dict] = data.get("items", [])
    total = len(items)
    if total == 0:
        return {"error": "no reports in the review window", "since": since}

    # Completion: all mandatory slots filled
    completed = [
        r for r in items if r.get("hazard_type") and r.get("severity") and r.get("location_text")
    ]

    # Geo resolution rate (reports where geo_confidence >= 0.85)
    geo_resolved = [r for r in items if (r.get("geo_confidence") or 0) >= 0.85]

    # Life safety
    life_safety = [r for r in items if r.get("life_safety_flag")]

    # QA backlog
    qa_data = _get(
        base_url,
        "/api/v1/reports",
        admin_key,
        {**params, "qa_sample": "true", "qa_reviewed": "false", "limit": 1},
    )
    qa_backlog = qa_data.get("total", "unknown") if "error" not in qa_data else "unknown"

    # DLQ
    dlq = _get(base_url, "/api/v1/dlq", admin_key, {"limit": 1})
    dlq_depth = dlq.get("total", "unknown") if "error" not in dlq else "unknown"

    # Hazard type distribution
    hazard_dist: dict[str, int] = {}
    for r in items:
        h = r.get("hazard_type") or "unknown"
        hazard_dist[h] = hazard_dist.get(h, 0) + 1

    # Severity distribution
    sev_dist: dict[str, int] = {}
    for r in items:
        s = r.get("severity") or "unknown"
        sev_dist[s] = sev_dist.get(s, 0) + 1

    completion_rate = len(completed) / total
    geo_rate = len(geo_resolved) / total

    # SLO checks per §16.3
    slos = {
        "submission_success_100pct": dlq_depth == 0,
        "completion_rate_gte_75pct": completion_rate >= 0.75,
        "geo_resolution_gte_90pct": geo_rate >= 0.90,
    }
    all_slos_met = all(slos.values())

    # Recommendations
    recs = []
    if completion_rate < 0.75:
        recs.append(
            f"Completion rate {completion_rate * 100:.1f}% < 75%. Review abandoned calls in the console; add failing transcripts to data/eval/golden/."
        )
    if geo_rate < 0.90:
        recs.append(
            f"Geo resolution {geo_rate * 100:.1f}% < 90%. Add missing place aliases to data/gazetteer/; tune GEO_ACCEPT_THRESHOLD."
        )
    if isinstance(qa_backlog, int) and qa_backlog > 20:
        recs.append(
            f"QA backlog is {qa_backlog} reviews. Complete reviews to keep the golden set fresh."
        )
    if not recs:
        recs.append(
            "All SLOs met. Continue monitoring — add any new failure patterns to data/eval/golden/."
        )

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "review_period_weeks": weeks,
        "since": since,
        "total_reports": total,
        "completed_reports": len(completed),
        "completion_rate": round(completion_rate, 4),
        "geo_resolved_reports": len(geo_resolved),
        "geo_resolution_rate": round(geo_rate, 4),
        "life_safety_reports": len(life_safety),
        "qa_backlog": qa_backlog,
        "dlq_depth": dlq_depth,
        "hazard_distribution": hazard_dist,
        "severity_distribution": sev_dist,
        "slos": slos,
        "all_slos_met": all_slos_met,
        "recommendations": recs,
    }


def _print_review(r: dict[str, Any]) -> None:
    if "error" in r:
        print(f"  ERROR: {r['error']}")
        return

    ok = lambda b: "✅" if b else "❌"
    print(f"\n{'━' * 62}")
    print("  Sentinel Voice — Weekly Accuracy Review")
    print(f"  Period: last {r['review_period_weeks']} week(s) ({r['since'][:10]} →)")
    print(f"  Generated: {r['generated_at'][:19]}")
    print(f"{'━' * 62}")
    print(f"  Total reports   : {r['total_reports']}")
    print(f"  Completed       : {r['completed_reports']}  ({r['completion_rate'] * 100:.1f}%)")
    print(
        f"  Geo resolved    : {r['geo_resolved_reports']}  ({r['geo_resolution_rate'] * 100:.1f}% @ conf≥0.85)"
    )
    print(f"  Life-safety     : {r['life_safety_reports']}")
    print(f"  QA backlog      : {r['qa_backlog']}")
    print(f"  DLQ depth       : {r['dlq_depth']}")

    print("\n  Hazard distribution:")
    for h, n in sorted(r["hazard_distribution"].items(), key=lambda x: -x[1]):
        bar = "█" * max(1, n * 20 // max(r["total_reports"], 1))
        print(f"    {h:<20} {n:>4}  {bar}")

    print("\n  Severity distribution:")
    for s, n in sorted(r["severity_distribution"].items(), key=lambda x: -x[1]):
        print(f"    {s:<12} {n:>4}")

    print("\n  SLOs (§16.3):")
    for k, v in r["slos"].items():
        print(f"    {ok(v)}  {k}")

    print("\n  Recommendations:")
    for rec in r["recommendations"]:
        print(f"    • {rec}")

    print(f"\n{'━' * 62}")
    print(f"  {'✅ ALL SLOs MET' if r['all_slos_met'] else '❌ SLO REGRESSIONS — action required'}")
    print(f"{'━' * 62}\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--base-url", default=os.environ.get("FG_VOICE_BASE_URL", "http://localhost:8080")
    )
    parser.add_argument("--admin-key", default=os.environ.get("ADMIN_API_KEY", ""))
    parser.add_argument("--weeks", type=int, default=1, help="Review window in weeks (default 1)")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    review = _weekly_review(args.base_url, args.admin_key, args.weeks)
    if args.json:
        print(json.dumps(review, indent=2))
    else:
        _print_review(review)

    return 0 if review.get("all_slos_met", False) else 1


if __name__ == "__main__":
    sys.exit(main())
