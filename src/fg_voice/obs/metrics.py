"""CloudWatch EMF metrics emitter (§16.2).

Embedded Metrics Format (EMF) writes a JSON object to stdout. The
CloudWatch OTel collector sidecar (or the embedded CloudWatch agent)
picks it up and publishes it as a custom metric — no SDK, no network
call on the hot path, no per-metric thread.

All 11 metrics from §16.2 are modelled here:

    fg_voice_concurrent_calls          gauge
    fg_voice_turn_latency_ms           histogram (emit per-turn sample)
    fg_voice_tts_cache_hit_ratio       gauge
    fg_voice_asr_confidence            histogram
    fg_voice_barge_in_total            counter
    fg_voice_premature_cutoff_total    counter
    fg_voice_dtmf_fallback_ratio       gauge
    fg_voice_call_outcome_total        counter (dimension: outcome)
    fg_voice_submission_failures_total counter
    fg_voice_csv_lag_seconds           gauge
    fg_voice_enrichment_dlq_depth      gauge

Usage
-----
    from fg_voice.obs.metrics import metrics

    # Record a single turn latency sample
    metrics.record_turn_latency(342)

    # Update the concurrent-calls gauge
    metrics.set_concurrent_calls(7)
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

_NAMESPACE = "SentinelVoice"
_ENVIRONMENT = os.environ.get("FG_ENV", "dev")


def _emf(
    metric_name: str, value: float, unit: str, dimensions: dict[str, str] | None = None
) -> None:
    """Write one EMF log line to stdout. Synchronous + allocation-minimal."""
    dim_keys = list((dimensions or {}).keys())
    record: dict[str, Any] = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": _NAMESPACE,
                    "Dimensions": [["Environment", *dim_keys]],
                    "Metrics": [{"Name": metric_name, "Unit": unit}],
                }
            ],
        },
        "Environment": _ENVIRONMENT,
        metric_name: value,
    }
    if dimensions:
        record.update(dimensions)
    print(json.dumps(record, separators=(",", ":")), file=sys.stdout, flush=True)


class _Metrics:
    """Thin façade over the EMF emitter. Import the singleton `metrics`."""

    # ── Gauges ──────────────────────────────────────────────────────

    def set_concurrent_calls(self, count: int) -> None:
        """Current active call count across this task. Emit every ~10 s."""
        _emf("fg_voice_concurrent_calls", float(count), "Count")

    def set_tts_cache_hit_ratio(self, ratio: float) -> None:
        """Rolling hit rate (bank + Redis / total TTS requests this window)."""
        _emf("fg_voice_tts_cache_hit_ratio", ratio, "None")

    def set_dtmf_fallback_ratio(self, ratio: float) -> None:
        """DTMF fallback turns / total turns this window."""
        _emf("fg_voice_dtmf_fallback_ratio", ratio, "None")

    def set_csv_lag_seconds(self, lag: float) -> None:
        """Age of the oldest unwritten report row (seconds)."""
        _emf("fg_voice_csv_lag_seconds", lag, "Seconds")

    def set_enrichment_dlq_depth(self, depth: int) -> None:
        """Number of stuck outbox rows in the DLQ."""
        _emf("fg_voice_enrichment_dlq_depth", float(depth), "Count")

    # ── Histograms (emit one sample per observation) ─────────────────

    def record_turn_latency(self, latency_ms: int) -> None:
        """End-of-caller-speech → first audio byte at caller's ear (ms)."""
        _emf("fg_voice_turn_latency_ms", float(latency_ms), "Milliseconds")

    def record_asr_confidence(self, confidence: float) -> None:
        """STT transcript confidence for this turn."""
        _emf("fg_voice_asr_confidence", confidence, "None")

    # ── Counters (emit 1 per event) ──────────────────────────────────

    def inc_barge_in(self) -> None:
        _emf("fg_voice_barge_in_total", 1.0, "Count")

    def inc_premature_cutoff(self) -> None:
        _emf("fg_voice_premature_cutoff_total", 1.0, "Count")

    def inc_submission_failure(self) -> None:
        """Must stay at zero in production. Triggers a page alarm."""
        _emf("fg_voice_submission_failures_total", 1.0, "Count")

    def inc_call_outcome(self, outcome: str) -> None:
        """outcome: submitted | abandoned | not_reporting | timeout | error"""
        _emf(
            "fg_voice_call_outcome_total",
            1.0,
            "Count",
            dimensions={"outcome": outcome},
        )

    # ── Custom metric (autoscaling signal) ───────────────────────────

    def set_calls_per_task(self, value: float, cluster_name: str = "") -> None:
        """The primary autoscaling metric (§14.3). Emitted every 10 s."""
        _emf(
            "fg_voice_concurrent_calls_per_task",
            value,
            "Count",
            dimensions=({"ClusterName": cluster_name} if cluster_name else None),
        )


metrics = _Metrics()

__all__ = ["metrics"]
