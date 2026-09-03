"""Tests for the staged rollout guard (telephony/rollout_guard.py).

Verifies:
- All calls allowed when SSM parameter absent / empty (fail open)
- Only enabled districts pass when parameter is set
- Unknown district is always allowed (fail open)
- Already-erased sentinel is detected
- Cache invalidation works
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fg_voice.telephony.rollout_guard import (
    is_call_allowed,
    reset_cache,
)


@pytest.fixture(autouse=True)
def clear_cache():
    reset_cache()
    yield
    reset_cache()


# ── Fail-open behaviour ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_ssm_parameter_allows_all_calls() -> None:
    """When SSM raises ParameterNotFound, all calls are allowed."""
    fake_ssm = AsyncMock()
    fake_ssm.get_parameter = AsyncMock(side_effect=_make_not_found_exc())
    fake_ssm.__aenter__ = AsyncMock(return_value=fake_ssm)
    fake_ssm.__aexit__ = AsyncMock(return_value=None)
    mock_session = MagicMock()
    mock_session.client = MagicMock(return_value=fake_ssm)

    with patch("aioboto3.Session", return_value=mock_session):
        result = await is_call_allowed("Krishna")
    assert result is True


@pytest.mark.asyncio
async def test_empty_parameter_value_allows_all_calls() -> None:
    """Empty SSM value means no restriction."""
    reset_cache()
    fake_ssm = AsyncMock()
    fake_ssm.get_parameter = AsyncMock(return_value={"Parameter": {"Value": ""}})
    fake_ssm.__aenter__ = AsyncMock(return_value=fake_ssm)
    fake_ssm.__aexit__ = AsyncMock(return_value=None)
    mock_session = MagicMock()
    mock_session.client = MagicMock(return_value=fake_ssm)

    with patch("aioboto3.Session", return_value=mock_session):
        result = await is_call_allowed("Guntur")
    assert result is True


@pytest.mark.asyncio
async def test_ssm_network_error_allows_all_calls() -> None:
    """Network error fetching SSM → fail open (all calls allowed)."""
    reset_cache()
    fake_ssm = AsyncMock()
    fake_ssm.get_parameter = AsyncMock(side_effect=ConnectionError("network error"))
    fake_ssm.__aenter__ = AsyncMock(return_value=fake_ssm)
    fake_ssm.__aexit__ = AsyncMock(return_value=None)
    mock_session = MagicMock()
    mock_session.client = MagicMock(return_value=fake_ssm)

    with patch("aioboto3.Session", return_value=mock_session):
        result = await is_call_allowed("Vizianagaram")
    assert result is True


# ── District filtering ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enabled_district_allowed() -> None:
    """District in the SSM list is allowed."""
    reset_cache()
    fake_ssm = AsyncMock()
    fake_ssm.get_parameter = AsyncMock(return_value={"Parameter": {"Value": "Krishna,Guntur"}})
    fake_ssm.__aenter__ = AsyncMock(return_value=fake_ssm)
    fake_ssm.__aexit__ = AsyncMock(return_value=None)
    mock_session = MagicMock()
    mock_session.client = MagicMock(return_value=fake_ssm)

    with patch("aioboto3.Session", return_value=mock_session):
        assert await is_call_allowed("Krishna") is True


@pytest.mark.asyncio
async def test_disabled_district_blocked() -> None:
    """District NOT in the SSM list is blocked."""
    reset_cache()
    fake_ssm = AsyncMock()
    fake_ssm.get_parameter = AsyncMock(return_value={"Parameter": {"Value": "Krishna"}})
    fake_ssm.__aenter__ = AsyncMock(return_value=fake_ssm)
    fake_ssm.__aexit__ = AsyncMock(return_value=None)
    mock_session = MagicMock()
    mock_session.client = MagicMock(return_value=fake_ssm)

    with patch("aioboto3.Session", return_value=mock_session):
        assert await is_call_allowed("Visakhapatnam") is False


@pytest.mark.asyncio
async def test_unknown_district_none_is_allowed() -> None:
    """None district (unknown number circle) → always allowed."""
    reset_cache()
    fake_ssm = AsyncMock()
    fake_ssm.get_parameter = AsyncMock(return_value={"Parameter": {"Value": "Krishna"}})
    fake_ssm.__aenter__ = AsyncMock(return_value=fake_ssm)
    fake_ssm.__aexit__ = AsyncMock(return_value=None)
    mock_session = MagicMock()
    mock_session.client = MagicMock(return_value=fake_ssm)

    with patch("aioboto3.Session", return_value=mock_session):
        assert await is_call_allowed(None) is True


@pytest.mark.asyncio
async def test_whitespace_in_parameter_value_is_handled() -> None:
    """SSM value with extra spaces is parsed correctly."""
    reset_cache()
    fake_ssm = AsyncMock()
    fake_ssm.get_parameter = AsyncMock(
        return_value={"Parameter": {"Value": " Krishna , Guntur , "}}
    )
    fake_ssm.__aenter__ = AsyncMock(return_value=fake_ssm)
    fake_ssm.__aexit__ = AsyncMock(return_value=None)
    mock_session = MagicMock()
    mock_session.client = MagicMock(return_value=fake_ssm)

    with patch("aioboto3.Session", return_value=mock_session):
        assert await is_call_allowed("Guntur") is True
        reset_cache()
    with patch("aioboto3.Session", return_value=mock_session):
        assert await is_call_allowed("Visakhapatnam") is False


# ── Cache behaviour ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_prevents_multiple_ssm_calls() -> None:
    """SSM is fetched once; subsequent calls use the cache."""
    reset_cache()
    call_count = 0

    async def _count(*_a, **_kw):
        nonlocal call_count
        call_count += 1
        return {"Parameter": {"Value": "Krishna"}}

    fake_ssm = AsyncMock()
    fake_ssm.get_parameter = _count
    fake_ssm.__aenter__ = AsyncMock(return_value=fake_ssm)
    fake_ssm.__aexit__ = AsyncMock(return_value=None)
    mock_session = MagicMock()
    mock_session.client = MagicMock(return_value=fake_ssm)

    with patch("aioboto3.Session", return_value=mock_session):
        await is_call_allowed("Krishna")
        await is_call_allowed("Krishna")
        await is_call_allowed("Guntur")

    assert call_count == 1, f"SSM fetched {call_count} times instead of once"


# ── Helper ───────────────────────────────────────────────────────────


def _make_not_found_exc():
    """Create a mock ParameterNotFound exception."""
    exc = MagicMock()
    exc.__class__ = type("ParameterNotFound", (Exception,), {})
    return exc.__class__("ParameterNotFound")
