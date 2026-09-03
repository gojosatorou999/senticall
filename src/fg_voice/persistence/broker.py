"""In-process async pub/sub for report events.

Not a general-purpose message bus — just enough to fan events from
the outbox relay out to any in-process consumers (SSE subscribers,
CSV projector, alert handlers) without going back through the DB.

Design:
- One `InProcessBroker` per process, held in `main.py`'s state
- Subscribers get a per-subscription `asyncio.Queue`; slow consumers
  are bounded (max 100 buffered) — a subscriber that can't keep up
  loses events rather than blocking the publisher, and gets a
  `SubscriberLagged` sentinel so it can decide to reconnect
- Cleanup on `async with broker.subscribe()` exit (or on explicit
  `unsubscribe`)

For a multi-process deploy this needs a real broker (Redis pub/sub,
NATS, etc). That's a P5-plus concern; a single-process voice worker
happily runs on this today."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final

_DEFAULT_QUEUE_MAX: Final[int] = 100


@dataclass(frozen=True, slots=True)
class ReportEvent:
    """One published event. Structure is what the SSE endpoint
    serialises verbatim into `data:` lines."""

    event_type: str
    payload: dict[str, Any]


class SubscriberLagged:
    """Sentinel put into a subscriber's queue when the broker had to
    drop an event because the queue was full. Subscribers should
    treat this as "you missed something; consider reconnecting"."""

    __slots__ = ()


LAGGED: Final[SubscriberLagged] = SubscriberLagged()


class InProcessBroker:
    """Single-process pub/sub. Publish is O(subscribers)."""

    def __init__(self, *, queue_max: int = _DEFAULT_QUEUE_MAX) -> None:
        self._queues: set[asyncio.Queue[ReportEvent | SubscriberLagged]] = set()
        self._queue_max = queue_max
        self._lock = asyncio.Lock()

    async def publish(self, event: ReportEvent) -> None:
        """Fan out to every subscriber. Never blocks: if a subscriber's
        queue is full we drop the event for them and enqueue a
        `LAGGED` sentinel."""
        async with self._lock:
            queues = list(self._queues)
        for q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Drain one slot so we can enqueue the lag marker. If
                # `LAGGED` is already in the queue, that's fine — one is
                # enough to signal.
                with contextlib.suppress(asyncio.QueueEmpty):  # pragma: no cover
                    q.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):  # pragma: no cover
                    q.put_nowait(LAGGED)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[ReportEvent | SubscriberLagged]]:
        """`async with broker.subscribe() as q: while True: item = await q.get()`
        cleans itself up on scope exit."""
        q: asyncio.Queue[ReportEvent | SubscriberLagged] = asyncio.Queue(maxsize=self._queue_max)
        async with self._lock:
            self._queues.add(q)
        try:
            yield q
        finally:
            async with self._lock:
                self._queues.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._queues)


__all__ = ["LAGGED", "InProcessBroker", "ReportEvent", "SubscriberLagged"]
