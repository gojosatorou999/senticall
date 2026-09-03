"""Pilot completion report (spec §18.7 + §P9 exit gate).

Queries the reports API and computes the P9 exit gate:
    ≥ 80% completion rate
    0 data-loss incidents (submission failures in the outbox DLQ)

Usage
-----
    uv run python scripts/pilot_report.py --base-url https://voice-staging.sentinel.in
    uv run python scripts/pilot_report.py --base-url http://localhost:8080 --min-calls 10
    uv run python scripts/pilot_report.py --from 2026-08-24 --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from typing import Any


def _get(base_url: str, path: str, admin_key: str, params: dict | None = None) -> Any:
    import urllib.parse
    import urllib.request

    url = f"{base_url.rstrip('/')}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-Admin-Api-Key": admin_key})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        print(f"  ERROR: {path} → {exc}", file=sys.stderr)
        return None


def _compute_report(
    base_url: str,
    admin_key: str,
    since: str | None,
    min_calls: int,
) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": 200, "source": "voice"}
    if since:
        params["from"] = since

    data = _get(base_url, "/api/v1/reports", admin_key, params)
    if not data:
        return {"error": "failed to fetch reports"}

    items: list[dict] = data.get("items", [])
    total = len(items)

    submitted = sum(1 for r in items if r.get("enrichment_status") not in (None, "failed"))
    completed = sum(
        1 for r in items if r.get("hazard_type") and r.get("severity") and r.get("location_text")
    )
    life_safety = sum(1 for r in items if r.get("life_safety_flag"))

    # DLQ depth (data-loss proxy)
    dlq = _get(base_url, "/api/v1/dlq", admin_key, {"limit": 1})
    dlq_depth = dlq.get("total", 0) if dlq else "unknown"

    completion_rate = completed / max(total, 1)
    data_loss_incidents = 0 if dlq_depth == 0 else (1 if isinstance(dlq_depth, int) else "unknown")

    # Slot fill rates
    slot_fill: dict[str, int] = {}
    for slot in ["hazard_type", "severity", "location_text", "description_clean"]:
        slot_fill[slot] = sum(1 for r in items if r.get(slot))

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "base_url": base_url,
        "since": since,
        "total_reports": total,
        "completed_reports": completed,
        "submitted_reports": submitted,
        "life_safety_reports": life_safety,
        "completion_rate": round(completion_rate, 4),
        "dlq_depth": dlq_depth,
        "data_loss_incidents": data_loss_incidents,
        "slot_fill_rates": {k: round(v / max(total, 1), 4) for k, v in slot_fill.items()},
        "exit_gate": {
            "min_calls_required": min_calls,
            "min_calls_met": total >= min_calls,
            "completion_rate_target": 0.80,
            "completion_rate_met": completion_rate >= 0.80,
            "zero_data_loss_target": True,
            "zero_data_loss_met": data_loss_incidents == 0,
            "passed": (total >= min_calls and completion_rate >= 0.80 and data_loss_incidents == 0),
        },
    }


def _print_report(r: dict[str, Any]) -> None:
    if "error" in r:
        print(f"  ERROR: {r['error']}", file=sys.stderr)
        return

    g = r["exit_gate"]
    passed = g.get("passed", False)

    print(f"\n{'━' * 60}")
    print("  Sentinel Voice — Pilot Completion Report")
    print(f"  Generated: {r['generated_at']}")
    if r.get("since"):
        print(f"  Since:     {r['since']}")
    print(f"{'━' * 60}")
    print(f"  Total reports :  {r['total_reports']}")
    print(f"  Completed     :  {r['completed_reports']}  ({r['completion_rate'] * 100:.1f}%)")
    print(f"  Life-safety   :  {r['life_safety_reports']}")
    print(f"  DLQ depth     :  {r['dlq_depth']}")
    print()
    print("  Slot fill rates:")
    for slot, rate in r["slot_fill_rates"].items():
        bar = "█" * int(rate * 20)
        print(f"    {slot:<25} {rate * 100:>5.1f}%  {bar}")
    print()
    print(f"{'━' * 60}")
    print("  EXIT GATE")
    mk = lambda ok: "✅" if ok else "❌"
    print(
        f"  {mk(g['min_calls_met'])}  ≥ {g['min_calls_required']} calls ({r['total_reports']} actual)"
    )
    print(f"  {mk(g['completion_rate_met'])}  ≥ 80% completion ({r['completion_rate'] * 100:.1f}%)")
    print(f"  {mk(g['zero_data_loss_met'])}  0 data-loss incidents (DLQ depth: {r['dlq_depth']})")
    print(f"{'━' * 60}")
    print(
        f"  {'✅ PILOT EXIT GATE PASSED' if passed else '❌ EXIT GATE FAILED — not ready for public launch'}"
    )
    print(f"{'━' * 60}\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("FG_VOICE_BASE_URL", "http://localhost:8080"),
    )
    parser.add_argument("--admin-key", default=os.environ.get("ADMIN_API_KEY", ""))
    parser.add_argument("--from", dest="since", default=None, help="ISO date lower bound")
    parser.add_argument("--min-calls", type=int, default=50, help="Exit gate: minimum call count")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of human-readable")
    args = parser.parse_args()

    report = _compute_report(args.base_url, args.admin_key, args.since, args.min_calls)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_report(report)

    exit_gate = report.get("exit_gate", {})
    return 0 if exit_gate.get("passed", False) else 1


if __name__ == "__main__":
    sys.exit(main())
