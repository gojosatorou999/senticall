"""Admin API-key authentication for the reports read + stream + export
endpoints.

The Twilio webhooks (`/voice/*`) authenticate via X-Twilio-Signature
(see `telephony/twilio_signature.py`); the health endpoints are
public. This module is only for the ops-facing report endpoints
that expose caller-report data.

Design:
- Header: `X-Admin-Api-Key`
- Setting: `ADMIN_API_KEY` — SecretStr, default empty. Empty MEANS
  auth is disabled (dev bypass) — a fresh clone can hit `/reports`
  without any config. In production, `require_production_secrets()`
  logs a boot warning when the key is empty so operators notice
- Constant-time comparison via `hmac.compare_digest` so timing
  attacks can't leak the key one character at a time
- Rejection uses 401 with a `WWW-Authenticate` header so browsers
  behave predictably; error body is intentionally minimal
- Applied via `Depends(require_admin_api_key)` on the endpoint — no
  global middleware, so it's obvious per-route which endpoints are
  guarded and which aren't"""

from __future__ import annotations

import hmac
import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from fg_voice.config import get_settings

ADMIN_API_KEY_HEADER = "X-Admin-Api-Key"


def require_admin_api_key(
    x_admin_api_key: Annotated[str | None, Header(alias=ADMIN_API_KEY_HEADER)] = None,
) -> None:
    """FastAPI dependency. Raises 401 on missing/wrong key when the
    setting is populated; no-op when the setting is empty.

    Returning None on success is deliberate — endpoints don't need
    the key value, just the fact that it was validated."""
    expected = get_settings().admin_api_key.get_secret_value()
    if not expected:
        # Dev bypass. `require_production_secrets` in config.py refuses
        # to boot production without the key set, so this branch is
        # only reachable in dev/staging.
        return
    if x_admin_api_key is None or not hmac.compare_digest(x_admin_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing admin API key",
            headers={"WWW-Authenticate": ADMIN_API_KEY_HEADER},
        )


# Convenience `Depends(...)` alias so route decorators stay short:
#   @router.get("/reports", dependencies=[AdminApiKey])
AdminApiKey = Depends(require_admin_api_key)


_basic = HTTPBasic()

_BASIC_USER = "sentinel"
_BASIC_PASS = "sentinel123"


def require_basic_auth(
    credentials: Annotated[HTTPBasicCredentials, Depends(_basic)],
) -> str:
    """Browser-friendly Basic auth. Guards the dial page + console UI.
    Fixed credentials for dev — swap to a settings lookup for prod."""
    user_ok = secrets.compare_digest(credentials.username, _BASIC_USER)
    pass_ok = secrets.compare_digest(credentials.password, _BASIC_PASS)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


BasicAuth = Depends(require_basic_auth)


__all__ = [
    "ADMIN_API_KEY_HEADER",
    "AdminApiKey",
    "BasicAuth",
    "require_admin_api_key",
    "require_basic_auth",
]
