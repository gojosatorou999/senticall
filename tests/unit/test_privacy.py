"""Tests for the DPDP privacy endpoints (spec §17.2).

Covers:
- GET  /api/v1/privacy/caller/{hash} — right of access
- DELETE /api/v1/privacy/caller/{hash} — right of erasure
- Erasure is idempotent
- Erasure zeroes PII fields, retains the anonymised hazard record
- Already-erased hash returns early without a DB hit
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fg_voice.api.routes_privacy import (
    _ERASED_HASH_PREFIX,
    _erased_hash,
    _is_erased,
)
from fg_voice.api.routes_privacy import (
    router as privacy_router,
)

# ── Pure helper tests ─────────────────────────────────────────────────


def test_erased_hash_has_prefix() -> None:
    h = _erased_hash("abc123def456" * 5)
    assert h.startswith(_ERASED_HASH_PREFIX)


def test_erased_hash_truncates_to_8_chars() -> None:
    original = "a" * 64
    sentinel = _erased_hash(original)
    assert sentinel == f"{_ERASED_HASH_PREFIX}{'a' * 8}"


def test_is_erased_true_for_sentinel() -> None:
    assert _is_erased(_erased_hash("any_hash")) is True


def test_is_erased_false_for_real_hash() -> None:
    assert _is_erased("a" * 64) is False


# ── FastAPI endpoint tests ────────────────────────────────────────────


def _make_fake_report(caller_hash: str, **overrides: Any) -> MagicMock:
    r = MagicMock()
    r.report_id = uuid.uuid4()
    r.short_ref = "FG-TEST"
    r.caller_hash = caller_hash
    r.hazard_type = overrides.get("hazard_type", "storm")
    r.severity = overrides.get("severity", "moderate")
    r.location_resolved = overrides.get("location_resolved", "Kakinada")
    r.location_raw = overrides.get("location_raw", "kakinada harbour")
    r.description = "oil spill"
    r.description_clean = "oil spill"
    r.created_at = datetime.now(UTC)
    r.pii_erased_at = overrides.get("pii_erased_at")
    r.source = "voice"
    return r


def _make_execute_result(rows: list) -> MagicMock:
    """Make a sync MagicMock matching session.execute() result shape."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    return result


@pytest.fixture
def privacy_client():
    """TestClient with admin auth bypassed (empty key = dev mode)."""
    with patch("fg_voice.api.auth.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(admin_api_key=MagicMock(get_secret_value=lambda: ""))
        app = FastAPI()
        app.include_router(privacy_router)
        with TestClient(app) as client:
            yield client


def _make_session_mock(*execute_results: list) -> tuple[MagicMock, MagicMock]:
    """Build a mock session + session_maker for privacy endpoint tests.

    `execute_results`: each element is the list of rows returned for
    successive `session.execute()` calls.
    """
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.commit = AsyncMock()

    # `await session.execute(stmt)` returns a plain MagicMock (sync result)
    # so that .scalars().all() work as synchronous chained calls.
    side_effects = [_make_execute_result(rows) for rows in execute_results]
    session.execute = AsyncMock(side_effect=side_effects)

    maker = MagicMock(return_value=session)
    return session, maker


# ── GET /api/v1/privacy/caller/{hash} ────────────────────────────────


def test_get_caller_no_db_returns_empty(privacy_client: TestClient) -> None:
    with patch("fg_voice.api.routes_privacy.get_session_maker", return_value=None):
        resp = privacy_client.get("/api/v1/privacy/caller/abc123")
    assert resp.status_code == 200
    assert resp.json()["reports"] == []


def test_get_caller_with_rows_returns_access_list(privacy_client: TestClient) -> None:
    caller_hash = "a" * 64
    _, maker = _make_session_mock([_make_fake_report(caller_hash)])

    with patch("fg_voice.api.routes_privacy.get_session_maker", return_value=maker):
        resp = privacy_client.get(f"/api/v1/privacy/caller/{caller_hash}")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "active"
    assert data["report_count"] == 1
    assert data["reports"][0]["short_ref"] == "FG-TEST"


def test_get_caller_not_found_returns_404(privacy_client: TestClient) -> None:
    # First execute: original hash → no rows
    # Second execute: erased sentinel → no rows
    _, maker = _make_session_mock([], [])

    with patch("fg_voice.api.routes_privacy.get_session_maker", return_value=maker):
        resp = privacy_client.get("/api/v1/privacy/caller/" + "b" * 64)
    assert resp.status_code == 404


def test_erased_caller_shows_as_erased_on_get(privacy_client: TestClient) -> None:
    original = "e" * 64
    sentinel = _erased_hash(original)
    fake_erased = _make_fake_report(sentinel)

    # First execute: original hash → no rows
    # Second execute: erased sentinel → 1 row
    _, maker = _make_session_mock([], [fake_erased])

    with patch("fg_voice.api.routes_privacy.get_session_maker", return_value=maker):
        resp = privacy_client.get(f"/api/v1/privacy/caller/{original}")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "erased"
    assert data["reports_erased"] == 1


# ── DELETE /api/v1/privacy/caller/{hash} ─────────────────────────────


def test_delete_caller_no_db_returns_503(privacy_client: TestClient) -> None:
    with patch("fg_voice.api.routes_privacy.get_session_maker", return_value=None):
        resp = privacy_client.delete("/api/v1/privacy/caller/abc123")
    assert resp.status_code == 503


def test_delete_already_erased_hash_returns_early(privacy_client: TestClient) -> None:
    """Already-erased sentinel returns 200 immediately (no DB hit)."""
    sentinel = _erased_hash("original" * 8)
    with patch("fg_voice.api.routes_privacy.get_session_maker", return_value=None):
        resp = privacy_client.delete(f"/api/v1/privacy/caller/{sentinel}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "already_erased"


def test_delete_caller_erases_pii(privacy_client: TestClient) -> None:
    """DELETE replaces PII with erased sentinels, retains hazard data."""
    caller_hash = "c" * 64
    fake_report = _make_fake_report(caller_hash)

    session, maker = _make_session_mock([fake_report])
    # Second execute is the bulk UPDATE (returns None-like result)
    session.execute = AsyncMock(
        side_effect=[
            _make_execute_result([fake_report]),
            MagicMock(),  # the UPDATE result (not iterated)
        ]
    )

    with patch("fg_voice.api.routes_privacy.get_session_maker", return_value=maker):
        resp = privacy_client.delete(f"/api/v1/privacy/caller/{caller_hash}")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "erased"
    assert data["reports_erased"] == 1
    assert data["caller_hash_sentinel"].startswith(_ERASED_HASH_PREFIX)
    assert "hazard_type" in data["retained"]
    assert "erased_at" in data


def test_delete_caller_not_found_returns_404(privacy_client: TestClient) -> None:
    _, maker = _make_session_mock([])  # no rows for this caller

    with patch("fg_voice.api.routes_privacy.get_session_maker", return_value=maker):
        resp = privacy_client.delete("/api/v1/privacy/caller/" + "d" * 64)
    assert resp.status_code == 404


def test_delete_is_idempotent_for_already_erased_sentinel(privacy_client: TestClient) -> None:
    """Calling DELETE twice on the same hash returns 200 both times."""
    sentinel = _erased_hash("f" * 40)
    with patch("fg_voice.api.routes_privacy.get_session_maker", return_value=None):
        r1 = privacy_client.delete(f"/api/v1/privacy/caller/{sentinel}")
        r2 = privacy_client.delete(f"/api/v1/privacy/caller/{sentinel}")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["status"] == "already_erased"
    assert r2.json()["status"] == "already_erased"
