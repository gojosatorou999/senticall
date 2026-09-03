"""Caller-hash helper. Raw phone numbers are never persisted anywhere in
the system — see CLAUDE.md invariant #6 and spec §17.1. Everything
downstream keys on `HMAC-SHA256(msisdn, pepper)`."""

from __future__ import annotations

import hashlib
import hmac


def hash_msisdn(msisdn: str, pepper: str) -> str:
    """HMAC-SHA256(msisdn, pepper) as hex. Empty msisdn maps to a
    dedicated `<none>` sentinel so we never confuse "call with no
    caller ID" with a hash collision."""
    if not msisdn:
        return "<none>"
    return hmac.new(
        key=pepper.encode("utf-8"),
        msg=msisdn.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()
