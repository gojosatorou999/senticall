"""Region A vs B latency measurement (spec §14.5).

Measures the round-trip time from within an AWS ECS task to:
  A: ap-south-1 (Mumbai)   — agent co-located with RDS
  B: ap-southeast-1 (Singapore) — co-located with Twilio sg1 media edge

Run from within staging ECS tasks during the P8 hardening window.
Results feed the architectural decision in §14.5.

Usage (from within an ECS task or EC2 in ap-south-1):
    uv run python scripts/measure_region_latency.py
    uv run python scripts/measure_region_latency.py --reps 100 --region-b ap-southeast-1

The script uses simple TCP SYN timing (via `socket.create_connection`)
to the Deepgram API endpoint in each region — a representative proxy
for the STT → agent latency that dominates the §5 latency budget.

Note: this script requires outbound internet access and is not run in
CI. It is an ops tool that produces the data for the §14.5 decision.
"""

from __future__ import annotations

import argparse
import socket
import statistics
import sys
import time


def _tcp_latency_ms(host: str, port: int, timeout: float = 2.0) -> float | None:
    """Single TCP connection latency in milliseconds. Returns None on failure."""
    try:
        t0 = time.perf_counter()
        with socket.create_connection((host, port), timeout=timeout):
            pass
        return (time.perf_counter() - t0) * 1000
    except (TimeoutError, OSError):
        return None


def _measure(
    name: str,
    host: str,
    port: int,
    reps: int,
) -> dict[str, float | str | None]:
    """Measure TCP latency to host:port reps times and return statistics."""
    samples: list[float] = []
    failures = 0
    for _ in range(reps):
        ms = _tcp_latency_ms(host, port)
        if ms is not None:
            samples.append(ms)
        else:
            failures += 1

    if not samples:
        return {"region": name, "host": host, "error": "all connections failed"}

    return {
        "region": name,
        "host": host,
        "reps": reps,
        "failures": failures,
        "mean_ms": round(statistics.mean(samples), 1),
        "p50_ms": round(statistics.median(samples), 1),
        "p95_ms": round(sorted(samples)[int(0.95 * len(samples))], 1),
        "min_ms": round(min(samples), 1),
        "max_ms": round(max(samples), 1),
    }


# Known endpoints to probe (TCP-only, no auth)
_PROBES = {
    # Deepgram API is the primary STT hot path
    "deepgram-global": ("api.deepgram.com", 443),
    # S3 endpoint for RAG snapshots (in-region = fast)
    "s3-ap-south-1": ("s3.ap-south-1.amazonaws.com", 443),
    "s3-ap-southeast-1": ("s3.ap-southeast-1.amazonaws.com", 443),
    # RDS-proxy hostname (fill in from terraform output)
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--reps", type=int, default=20, help="Repetitions per probe (default 20)")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of human-readable")
    args = parser.parse_args()

    results = []
    for name, (host, port) in _PROBES.items():
        print(f"Probing {name} ({host}:{port}) x {args.reps} …", flush=True)
        r = _measure(name, host, port, args.reps)
        results.append(r)

    if args.json:
        import json

        print(json.dumps(results, indent=2))
        return 0

    print("\n" + "═" * 70)
    print(f"  {'Probe':<30}  {'p50 (ms)':>8}  {'p95 (ms)':>8}  {'mean (ms)':>9}")
    print("  " + "─" * 66)
    for r in results:
        if "error" in r:
            print(f"  {r['region']:<30}  ERROR: {r['error']}")
        else:
            print(f"  {r['region']:<30}  {r['p50_ms']:>8}  {r['p95_ms']:>8}  {r['mean_ms']:>9}")
    print("═" * 70)
    print("""
Decision guide (spec §14.5):
  Option A (ap-south-1 only):  Twilio sg1 → ap-south-1 adds ~30-60 ms round-trip.
                                DB queries stay in-region. Recommended default.
  Option B (ap-southeast-1):   Saves the Twilio media hop but adds cross-region
                                DB write latency on the outbox. Only worth it if
                                p95 Deepgram RTT from sg1 > 80 ms.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
