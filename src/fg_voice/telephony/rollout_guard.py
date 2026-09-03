"""Staged rollout guard (spec §P9 — "single district first").

Reads the enabled-district list from SSM at call-start time (cached 60 s)
and returns True if a call should be accepted. When False, the caller
hears the overflow greeting and the call hangs up gracefully without
consuming an agent slot.

District matching is based on `caller_district` set by the intake path
(derived from the Twilio caller's number circle or an explicit header in
staging). If the district can't be determined (unknown number series),
the call is allowed through — "unknown district" is always in the
rollout (fail open is safer than silently dropping disaster reports).

SSM parameter: `/fg-voice/{env}/rollout/enabled_districts`
Value: comma-separated district canonical names, e.g. "Krishna,Guntur"
An empty value or missing parameter = ALL districts allowed.

Degraded mode: if SSM is unavailable, all calls are allowed (fail open).
This means a misconfigured rollout parameter can't accidentally block
every inbound call on a cyclone day.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Final

from fg_voice.obs.logging import get_logger

log = get_logger(__name__)

_CACHE_TTL_SEC: Final[float] = 60.0
_PARAM_TEMPLATE = "/fg-voice/{env}/rollout/enabled_districts"

# Module-level cache: (timestamp, frozenset[str] | None)
# None means "allow all" (SSM unavailable or empty)
_cache: tuple[float, frozenset[str] | None] = (0.0, None)
_cache_lock = asyncio.Lock()


def _ssm_param_name() -> str:
    env = os.environ.get("FG_ENV", "dev")
    return _PARAM_TEMPLATE.format(env=env)


async def _fetch_enabled_districts() -> frozenset[str] | None:
    """Fetch the enabled-district list from SSM. Returns None on failure
    (which maps to "allow all" in the caller)."""
    try:
        import aioboto3

        region = os.environ.get("FG_REGION", "ap-south-1")
        session = aioboto3.Session()
        async with session.client("ssm", region_name=region) as ssm:
            resp = await ssm.get_parameter(Name=_ssm_param_name())
            value: str = resp["Parameter"]["Value"].strip()
            if not value:
                return None
            districts = frozenset(d.strip() for d in value.split(",") if d.strip())
            return districts if districts else None
    except Exception as exc:
        log.warning(
            "rollout_guard.ssm_fetch_failed",
            error=str(exc),
            note="failing open — all calls allowed",
        )
        return None


async def get_enabled_districts() -> frozenset[str] | None:
    """Return the cached district set, refreshing if stale.
    None = all districts allowed."""
    global _cache
    now = time.monotonic()
    ts, cached = _cache
    if now - ts < _CACHE_TTL_SEC:
        return cached

    async with _cache_lock:
        # Double-check after acquiring lock
        now = time.monotonic()
        ts, cached = _cache
        if now - ts < _CACHE_TTL_SEC:
            return cached

        districts = await _fetch_enabled_districts()
        _cache = (now, districts)
        log.info(
            "rollout_guard.cache_refreshed",
            enabled_districts=sorted(districts) if districts else "ALL",
        )
        return districts


async def is_call_allowed(caller_district: str | None) -> bool:
    """True if the call should be accepted.

    `caller_district` is the canonical district name inferred from the
    caller's number circle or an explicit parameter. If None or unknown,
    the call is allowed through (fail open).
    """
    districts = await get_enabled_districts()
    if districts is None:
        # No restriction → all calls allowed
        return True
    if caller_district is None:
        # Unknown district → allow (don't drop disaster reports)
        log.debug("rollout_guard.unknown_district_allowed")
        return True
    allowed = caller_district in districts
    if not allowed:
        log.info(
            "rollout_guard.call_blocked",
            district=caller_district,
            enabled_districts=sorted(districts),
        )
    return allowed


def reset_cache() -> None:
    """Reset the cache — used in tests only."""
    global _cache
    _cache = (0.0, None)


__all__ = ["get_enabled_districts", "is_call_allowed", "reset_cache"]
