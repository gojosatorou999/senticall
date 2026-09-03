"""Tests for scripts/pilot_report.py exit gate logic."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))


def _import_pilot():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "pilot_report_mod", _SCRIPTS_DIR / "pilot_report.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def pilot():
    return _import_pilot()


def _make_items(n: int, completed: int, life_safety: int = 0) -> list[dict]:
    """Generate synthetic report items."""
    items = []
    for i in range(n):
        items.append(
            {
                "report_id": f"r{i}",
                "short_ref": f"FG-{i:04d}",
                "hazard_type": "storm" if i < completed else None,
                "severity": "moderate" if i < completed else None,
                "location_text": "Kakinada" if i < completed else None,
                "description_clean": "flooding" if i < completed else None,
                "life_safety_flag": i < life_safety,
                "enrichment_status": "enriched" if i < completed else None,
            }
        )
    return items


def test_exit_gate_passes_when_all_conditions_met(pilot) -> None:
    items = _make_items(n=50, completed=42)  # 84% completion

    def _fake_get(base_url, path, admin_key, params=None):
        if "/api/v1/reports" in path:
            return {"items": items}
        if "/api/v1/dlq" in path:
            return {"total": 0}
        return {}

    with patch.object(pilot, "_get", side_effect=_fake_get):
        r = pilot._compute_report("http://x", "key", None, min_calls=50)

    gate = r["exit_gate"]
    assert gate["min_calls_met"] is True
    assert gate["completion_rate_met"] is True
    assert gate["zero_data_loss_met"] is True
    assert gate["passed"] is True


def test_exit_gate_fails_on_low_completion(pilot) -> None:
    items = _make_items(n=50, completed=30)  # 60%

    def _fake_get(base_url, path, admin_key, params=None):
        if "/api/v1/reports" in path:
            return {"items": items}
        return {"total": 0}

    with patch.object(pilot, "_get", side_effect=_fake_get):
        r = pilot._compute_report("http://x", "key", None, min_calls=50)

    assert r["exit_gate"]["completion_rate_met"] is False
    assert r["exit_gate"]["passed"] is False


def test_exit_gate_fails_on_insufficient_call_count(pilot) -> None:
    items = _make_items(n=10, completed=10)  # 100% but only 10 calls

    def _fake_get(base_url, path, admin_key, params=None):
        if "/api/v1/reports" in path:
            return {"items": items}
        return {"total": 0}

    with patch.object(pilot, "_get", side_effect=_fake_get):
        r = pilot._compute_report("http://x", "key", None, min_calls=50)

    assert r["exit_gate"]["min_calls_met"] is False
    assert r["exit_gate"]["passed"] is False


def test_exit_gate_fails_on_dlq_depth(pilot) -> None:
    items = _make_items(n=50, completed=45)  # 90%

    def _fake_get(base_url, path, admin_key, params=None):
        if "/api/v1/reports" in path:
            return {"items": items}
        if "/api/v1/dlq" in path:
            return {"total": 3}  # DLQ non-empty = data loss
        return {}

    with patch.object(pilot, "_get", side_effect=_fake_get):
        r = pilot._compute_report("http://x", "key", None, min_calls=50)

    assert r["exit_gate"]["zero_data_loss_met"] is False
    assert r["exit_gate"]["passed"] is False


def test_life_safety_count_is_tracked(pilot) -> None:
    items = _make_items(n=50, completed=45, life_safety=3)

    def _fake_get(base_url, path, admin_key, params=None):
        if "/api/v1/reports" in path:
            return {"items": items}
        return {"total": 0}

    with patch.object(pilot, "_get", side_effect=_fake_get):
        r = pilot._compute_report("http://x", "key", None, min_calls=50)

    assert r["life_safety_reports"] == 3


def test_api_error_returns_error_dict(pilot) -> None:
    def _fake_get(base_url, path, admin_key, params=None):
        return None  # simulates request failure

    with patch.object(pilot, "_get", side_effect=_fake_get):
        r = pilot._compute_report("http://x", "key", None, min_calls=50)

    assert "error" in r


def test_completion_rate_computed_correctly(pilot) -> None:
    items = _make_items(n=20, completed=16)  # 80%

    def _fake_get(base_url, path, admin_key, params=None):
        if "/api/v1/reports" in path:
            return {"items": items}
        return {"total": 0}

    with patch.object(pilot, "_get", side_effect=_fake_get):
        r = pilot._compute_report("http://x", "key", None, min_calls=20)

    assert abs(r["completion_rate"] - 0.80) < 0.01
