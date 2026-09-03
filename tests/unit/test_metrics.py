"""Unit tests for obs/metrics.py (EMF emitter).

Tests verify that each emitter method produces valid JSON on stdout
in the EMF format CloudWatch expects. We capture stdout and parse the
JSON rather than testing the exact bytes so minor formatting changes
don't break the suite.
"""

from __future__ import annotations

import json
from io import StringIO

from fg_voice.obs.metrics import _NAMESPACE, _emf, metrics


def _capture_emf(func, *args, **kwargs) -> dict:
    """Call func(*args, **kwargs) and return the first parsed EMF record."""
    buf = StringIO()
    import sys

    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        func(*args, **kwargs)
    finally:
        sys.stdout = old_stdout
    return json.loads(buf.getvalue().strip())


# ── _emf low-level ──────────────────────────────────────────────────


def test_emf_basic_structure() -> None:
    rec = _capture_emf(_emf, "test_metric", 42.0, "Count")

    assert rec["_aws"]["CloudWatchMetrics"][0]["Namespace"] == _NAMESPACE
    assert rec["test_metric"] == 42.0
    dims = rec["_aws"]["CloudWatchMetrics"][0]["Dimensions"]
    assert ["Environment"] in dims


def test_emf_with_extra_dimension() -> None:
    rec = _capture_emf(_emf, "test_metric_dim", 1.0, "Count", dimensions={"outcome": "submitted"})

    assert rec["outcome"] == "submitted"
    all_dim_keys = [k for lst in rec["_aws"]["CloudWatchMetrics"][0]["Dimensions"] for k in lst]
    assert "outcome" in all_dim_keys


def test_emf_timestamp_present() -> None:
    rec = _capture_emf(_emf, "ts_test", 0.5, "None")
    ts = rec["_aws"]["Timestamp"]
    assert isinstance(ts, int)
    assert ts > 0


# ── Facade methods ───────────────────────────────────────────────────


def test_set_concurrent_calls() -> None:
    rec = _capture_emf(metrics.set_concurrent_calls, 7)
    assert rec["fg_voice_concurrent_calls"] == 7.0


def test_set_tts_cache_hit_ratio() -> None:
    rec = _capture_emf(metrics.set_tts_cache_hit_ratio, 0.87)
    assert abs(rec["fg_voice_tts_cache_hit_ratio"] - 0.87) < 1e-9


def test_set_dtmf_fallback_ratio() -> None:
    rec = _capture_emf(metrics.set_dtmf_fallback_ratio, 0.12)
    assert abs(rec["fg_voice_dtmf_fallback_ratio"] - 0.12) < 1e-9


def test_set_csv_lag_seconds() -> None:
    rec = _capture_emf(metrics.set_csv_lag_seconds, 5.3)
    assert abs(rec["fg_voice_csv_lag_seconds"] - 5.3) < 1e-9


def test_set_enrichment_dlq_depth() -> None:
    rec = _capture_emf(metrics.set_enrichment_dlq_depth, 3)
    assert rec["fg_voice_enrichment_dlq_depth"] == 3.0


def test_record_turn_latency() -> None:
    rec = _capture_emf(metrics.record_turn_latency, 342)
    assert rec["fg_voice_turn_latency_ms"] == 342.0


def test_record_asr_confidence() -> None:
    rec = _capture_emf(metrics.record_asr_confidence, 0.91)
    assert abs(rec["fg_voice_asr_confidence"] - 0.91) < 1e-9


def test_inc_barge_in() -> None:
    rec = _capture_emf(metrics.inc_barge_in)
    assert rec["fg_voice_barge_in_total"] == 1.0


def test_inc_premature_cutoff() -> None:
    rec = _capture_emf(metrics.inc_premature_cutoff)
    assert rec["fg_voice_premature_cutoff_total"] == 1.0


def test_inc_submission_failure() -> None:
    rec = _capture_emf(metrics.inc_submission_failure)
    assert rec["fg_voice_submission_failures_total"] == 1.0


def test_inc_call_outcome_submitted() -> None:
    rec = _capture_emf(metrics.inc_call_outcome, "submitted")
    assert rec["fg_voice_call_outcome_total"] == 1.0
    assert rec["outcome"] == "submitted"


def test_inc_call_outcome_abandoned() -> None:
    rec = _capture_emf(metrics.inc_call_outcome, "abandoned")
    assert rec["outcome"] == "abandoned"


def test_set_calls_per_task_with_cluster() -> None:
    rec = _capture_emf(metrics.set_calls_per_task, 7.5, cluster_name="fg-voice-staging")
    assert abs(rec["fg_voice_concurrent_calls_per_task"] - 7.5) < 1e-9
    assert rec["ClusterName"] == "fg-voice-staging"


def test_set_calls_per_task_without_cluster() -> None:
    rec = _capture_emf(metrics.set_calls_per_task, 3.0)
    assert abs(rec["fg_voice_concurrent_calls_per_task"] - 3.0) < 1e-9
