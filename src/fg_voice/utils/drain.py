"""Graceful drain state (spec §14.3).

Single process-wide flag flipped by a SIGTERM handler. The `/voice/*`
routes consult it via `is_draining()`; when true, `/voice/inbound`
returns TwiML that plays the fallback greeting and hangs up — no new
call gets connected to a worker that's about to disappear. In-flight
calls (already on a WebSocket) are unaffected; ECS `stopTimeout: 300`
gives them up to 5 minutes to complete before SIGKILL lands.

Kept in `utils/` (not `main.py`) so the routes can import without
pulling FastAPI's lifespan machinery, and tests can flip the flag
directly.

Not thread-safe by design — the whole voice-agent process is a single
asyncio event loop. If a future refactor introduces threads, replace
the module-level variable with `contextvars` or `threading.Event`."""

from __future__ import annotations

_draining: bool = False


def is_draining() -> bool:
    """True after `mark_draining()` has been called at least once."""
    return _draining


def mark_draining() -> None:
    """Called from the SIGTERM handler in `main.py`. Idempotent —
    calling twice does nothing."""
    global _draining
    _draining = True


def reset_for_tests() -> None:
    """Test-only hook. Never call from application code."""
    global _draining
    _draining = False


__all__ = ["is_draining", "mark_draining", "reset_for_tests"]
