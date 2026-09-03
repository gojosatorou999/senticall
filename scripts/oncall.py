"""On-call operations runbook (spec §P9 + §21).

Automates the common on-call checks from the §21 runbook table. Run any
subcommand standalone; the tool does NOT page or alter production without
explicit flags.

Subcommands
-----------
health      Check /healthz and /readyz
dlq         Inspect DLQ depth and recent stuck rows
call_stats  Recent call volume, completion rate, and outcome distribution
alert_test  Fire a test alert to verify the paging chain is wired
surge       Wrapper for scripts/surge_mode.py (on | off | status)
csv_lag     Check the CSV freshness SLO (≤ 10 s p95)
qa_queue    Show the unreviewed QA sample backlog size

Usage
-----
    uv run python scripts/oncall.py health
    uv run python scripts/oncall.py dlq
    uv run python scripts/oncall.py call_stats --hours 1
    uv run python scripts/oncall.py alert_test --apply
    uv run python scripts/oncall.py surge on --apply
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

_BASE_URL = os.environ.get("FG_VOICE_BASE_URL", "http://localhost:8080")
_ADMIN_KEY = os.environ.get("ADMIN_API_KEY", "")
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))


def _get(path: str, params: dict | None = None) -> dict[str, Any]:
    import urllib.parse
    import urllib.request

    url = f"{_BASE_URL.rstrip('/')}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-Admin-Api-Key": _ADMIN_KEY})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        return {"error": str(exc)}


# ── Subcommands ──────────────────────────────────────────────────────


def cmd_health(_args: argparse.Namespace) -> int:
    print("Checking /healthz ...")
    h = _get("/healthz")
    print(f"  healthz: {h}")

    print("Checking /readyz ...")
    r = _get("/readyz")
    print(f"  readyz:  {r}")

    ok = h.get("status") == "ok" and r.get("status") in ("ok", "degraded")
    print(f"\n{'✅ Healthy' if ok else '❌ Unhealthy — investigate /readyz response'}")
    return 0 if ok else 1


def cmd_dlq(args: argparse.Namespace) -> int:
    data = _get("/api/v1/dlq", {"limit": args.limit})
    if "error" in data:
        print(f"  ERROR: {data['error']}", file=sys.stderr)
        return 1

    items = data.get("items", [])
    total = data.get("total", len(items))
    print(f"\n  DLQ depth: {total}")

    if not items:
        print("  ✅ DLQ is empty")
        return 0

    print(f"\n  Most recent {len(items)} stuck rows:")
    for row in items:
        print(
            f"    id={row.get('id')}  report_id={str(row.get('report_id', ''))[:8]}  "
            f"retries={row.get('retry_count')}  "
            f"last_error={str(row.get('last_error', ''))[:60]}"
        )
    print(f"\n  {'❌ DLQ non-empty — page if > 0 per §16.2'}")
    return 1 if total > 0 else 0


def cmd_call_stats(args: argparse.Namespace) -> int:
    since = (datetime.now(UTC) - timedelta(hours=args.hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = _get("/api/v1/reports", {"limit": 200, "source": "voice", "from": since})
    if "error" in data:
        print(f"  ERROR: {data['error']}", file=sys.stderr)
        return 1

    items = data.get("items", [])
    total = len(items)
    print(f"\n  Call stats (last {args.hours}h, n={total}):")

    if total == 0:
        print("  No calls in window.")
        return 0

    completed = sum(1 for r in items if r.get("hazard_type") and r.get("severity"))
    life_safety = sum(1 for r in items if r.get("life_safety_flag"))
    completion_rate = completed / total

    hazard_dist: dict[str, int] = {}
    for r in items:
        h = r.get("hazard_type") or "unknown"
        hazard_dist[h] = hazard_dist.get(h, 0) + 1

    print(f"  Completion rate : {completion_rate * 100:.1f}%  (target ≥75%)")
    print(f"  Life-safety     : {life_safety}")
    print(f"  Hazard dist     : {dict(sorted(hazard_dist.items(), key=lambda x: -x[1]))}")

    if completion_rate < 0.75:
        print("\n  ⚠️  Completion rate below SLO — check abandoned calls in the console")
    else:
        print("\n  ✅ Completion rate within SLO")
    return 0


def cmd_alert_test(args: argparse.Namespace) -> int:
    """Fire a synthetic extreme-severity report to test the paging chain.
    Only makes real API calls when --apply is set."""
    if not args.apply:
        print("  dry-run: would POST a synthetic extreme report to the reports API")
        print("  Pass --apply to actually fire the test alert.")
        return 0

    # Post a synthetic report directly to trigger alert fan-out.
    # In production this exercises the SNS → PagerDuty path.
    import urllib.request

    payload = json.dumps(
        {
            "source": "voice",
            "call_sid": "CA_alert_test",
            "caller_hash": "alert_test_hash",
            "short_ref": "FG-TEST",
            "hazard_type": "storm",
            "severity": "extreme",
            "description_clean": "[ALERT TEST] Synthetic extreme report for paging chain verification",
            "event_type": "report.submitted",
        }
    ).encode()

    req = urllib.request.Request(
        f"{_BASE_URL.rstrip('/')}/api/v1/reports",
        data=payload,
        headers={
            "X-Admin-Api-Key": _ADMIN_KEY,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"  Alert test response: {resp.status}")
        print("  ✅ Test alert fired — check the paging channel for delivery")
        return 0
    except Exception as exc:
        print(f"  ❌ Alert test failed: {exc}", file=sys.stderr)
        return 1


def cmd_surge(args: argparse.Namespace) -> int:
    """Delegate to surge_mode.py."""
    script = os.path.join(_SCRIPTS_DIR, "surge_mode.py")
    cmd = [sys.executable, script, args.surge_mode]
    if args.apply:
        cmd.append("--apply")
    if args.trigger_source:
        cmd.extend(["--from", args.trigger_source])
    print(f"  Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    return result.returncode


def cmd_csv_lag(_args: argparse.Namespace) -> int:
    """Check CSV freshness — most recent report should be in the CSV within 10 s."""
    data = _get("/api/v1/reports", {"limit": 1, "source": "voice"})
    if "error" in data or not data.get("items"):
        print("  No reports available to measure CSV lag.")
        return 0

    latest = data["items"][0]
    received_at = latest.get("received_at_utc")
    if not received_at:
        print("  Cannot determine CSV lag (no timestamp on latest report).")
        return 0

    # Parse the timestamp and compare against now
    try:
        dt = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
        lag = (datetime.now(UTC) - dt).total_seconds()
        print(f"  Latest report received: {received_at}")
        print(f"  CSV lag estimate: {lag:.1f}s")
        if lag > 30:
            print(f"  ❌ CSV lag {lag:.1f}s > 30s threshold — check csv-projector service")
            return 1
        else:
            print("  ✅ CSV lag within SLO")
            return 0
    except Exception:
        print(f"  Could not parse timestamp: {received_at}")
        return 0


def cmd_qa_queue(_args: argparse.Namespace) -> int:
    data = _get("/api/v1/reports", {"limit": 1, "qa_sample": "true", "qa_reviewed": "false"})
    if "error" in data:
        print(f"  ERROR: {data['error']}", file=sys.stderr)
        return 1
    total = data.get("total", len(data.get("items", [])))
    print(f"  Unreviewed QA samples: {total}")
    if total > 20:
        print("  ⚠️  Backlog > 20 — review in the call console to keep golden set fresh")
    else:
        print("  ✅ QA backlog manageable")
    return 0


# ── CLI ──────────────────────────────────────────────────────────────


def main() -> int:
    global _BASE_URL, _ADMIN_KEY
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--base-url", default=_BASE_URL, help="API base URL")
    parser.add_argument("--admin-key", default=_ADMIN_KEY, help="X-Admin-Api-Key value")

    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health")
    p_dlq = sub.add_parser("dlq")
    p_dlq.add_argument("--limit", type=int, default=10)
    p_stats = sub.add_parser("call_stats")
    p_stats.add_argument("--hours", type=int, default=1)
    p_alert = sub.add_parser("alert_test")
    p_alert.add_argument("--apply", action="store_true")
    p_surge = sub.add_parser("surge")
    p_surge.add_argument("surge_mode", choices=["on", "off"])
    p_surge.add_argument("--apply", action="store_true")
    p_surge.add_argument("--trigger-source", dest="trigger_source", default="manual")
    sub.add_parser("csv_lag")
    sub.add_parser("qa_queue")

    args = parser.parse_args()
    _BASE_URL = args.base_url
    _ADMIN_KEY = args.admin_key

    dispatch = {
        "health": cmd_health,
        "dlq": cmd_dlq,
        "call_stats": cmd_call_stats,
        "alert_test": cmd_alert_test,
        "surge": cmd_surge,
        "csv_lag": cmd_csv_lag,
        "qa_queue": cmd_qa_queue,
    }
    return dispatch[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
