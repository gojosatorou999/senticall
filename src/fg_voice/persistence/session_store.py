"""Minimal call-session store used by P1.

Backed by Redis in a `call:session:{call_sid}` hash. The full
`call_sessions` table + SQLAlchemy model + Alembic migration land in
P5; until then, using Redis keeps P1 self-contained and lets us prove
the /voice/status → duration write end-to-end.

The interface here is deliberately narrow — everything P5 needs to
replace lives behind `SessionStore`, so the swap is a one-file change."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol

import redis.asyncio as redis

from fg_voice.config import get_settings

_SESSION_TTL_SEC = 7200  # 2 h — covers a call plus a comfortable margin
_KEY_PREFIX = "fg:call:session:"


@dataclass(frozen=True, slots=True)
class CallSessionRow:
    call_sid: str
    report_id: str
    caller_hash: str
    direction: str
    started_at: datetime
    ended_at: datetime | None = None
    duration_sec: int | None = None
    outcome: str | None = None


class SessionStore(Protocol):
    async def create(
        self,
        *,
        call_sid: str,
        report_id: str,
        caller_hash: str,
        direction: str,
    ) -> None: ...

    async def finalize(
        self,
        *,
        call_sid: str,
        duration_sec: int | None,
        outcome: str,
    ) -> None: ...

    async def get(self, call_sid: str) -> CallSessionRow | None: ...


class RedisSessionStore:
    def __init__(self, client: redis.Redis[str]) -> None:
        self._r = client

    def _key(self, call_sid: str) -> str:
        return _KEY_PREFIX + call_sid

    async def create(
        self,
        *,
        call_sid: str,
        report_id: str,
        caller_hash: str,
        direction: str,
    ) -> None:
        payload = {
            "call_sid": call_sid,
            "report_id": report_id,
            "caller_hash": caller_hash,
            "direction": direction,
            "started_at": datetime.now(UTC).isoformat(),
        }
        await self._r.set(self._key(call_sid), json.dumps(payload), ex=_SESSION_TTL_SEC)

    async def finalize(
        self,
        *,
        call_sid: str,
        duration_sec: int | None,
        outcome: str,
    ) -> None:
        raw = await self._r.get(self._key(call_sid))
        # /voice/status can arrive before /voice/inbound in rare
        # reorderings; write a bare finalisation row so we still have
        # the outcome for post-call reconciliation.
        payload = {"call_sid": call_sid} if raw is None else json.loads(raw)
        payload["ended_at"] = datetime.now(UTC).isoformat()
        payload["duration_sec"] = duration_sec
        payload["outcome"] = outcome
        await self._r.set(self._key(call_sid), json.dumps(payload), ex=_SESSION_TTL_SEC)

    async def get(self, call_sid: str) -> CallSessionRow | None:
        raw = await self._r.get(self._key(call_sid))
        if raw is None:
            return None
        p = json.loads(raw)
        return CallSessionRow(
            call_sid=p["call_sid"],
            report_id=p.get("report_id", ""),
            caller_hash=p.get("caller_hash", ""),
            direction=p.get("direction", ""),
            started_at=datetime.fromisoformat(p["started_at"])
            if "started_at" in p
            else datetime.now(UTC),
            ended_at=datetime.fromisoformat(p["ended_at"]) if p.get("ended_at") else None,
            duration_sec=p.get("duration_sec"),
            outcome=p.get("outcome"),
        )


class InMemorySessionStore:
    """Test double. Not used in the request path."""

    def __init__(self) -> None:
        self._rows: dict[str, CallSessionRow] = {}

    async def create(
        self,
        *,
        call_sid: str,
        report_id: str,
        caller_hash: str,
        direction: str,
    ) -> None:
        self._rows[call_sid] = CallSessionRow(
            call_sid=call_sid,
            report_id=report_id,
            caller_hash=caller_hash,
            direction=direction,
            started_at=datetime.now(UTC),
        )

    async def finalize(
        self,
        *,
        call_sid: str,
        duration_sec: int | None,
        outcome: str,
    ) -> None:
        existing = self._rows.get(call_sid)
        if existing is None:
            self._rows[call_sid] = CallSessionRow(
                call_sid=call_sid,
                report_id="",
                caller_hash="",
                direction="",
                started_at=datetime.now(UTC),
                ended_at=datetime.now(UTC),
                duration_sec=duration_sec,
                outcome=outcome,
            )
            return
        self._rows[call_sid] = replace(
            existing,
            ended_at=datetime.now(UTC),
            duration_sec=duration_sec,
            outcome=outcome,
        )

    async def get(self, call_sid: str) -> CallSessionRow | None:
        return self._rows.get(call_sid)


_singleton: RedisSessionStore | InMemorySessionStore | None = None


async def get_session_store() -> SessionStore:
    """Process-wide session store. Uses Redis when available; falls back to
    in-memory when Redis is unreachable (dev/Windows without Docker).

    Mirrors `conversation/state_store.get_call_state_store` — the two
    stores are written on the same code path, so a fallback in one and a
    hard failure in the other just turns a dead Redis into a 500 on
    /voice/gather/start. The fallback is logged at WARNING so ops never
    silently gets a non-durable store in production without noticing."""
    global _singleton
    if _singleton is not None:
        return _singleton

    import logging

    settings = get_settings()
    try:
        client: redis.Redis[str] = redis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=1.0,
        )
        # Probe the connection — ping fails fast if Redis is down.
        await client.ping()
        _singleton = RedisSessionStore(client)
        logging.getLogger(__name__).info("session_store: using Redis at %s", settings.redis_url)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "session_store: Redis unavailable (%s) — falling back to "
            "in-memory store. Session data will be lost on process restart. "
            "Do NOT use in production.",
            exc,
        )
        _singleton = InMemorySessionStore()
    return _singleton
