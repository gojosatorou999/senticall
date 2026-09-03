"""Dispatchers for the outbox relay — concrete side-effect handlers
that plug into `OutboxRelay(dispatcher=...)`.

`LogDispatcher` (in relay.py) is the safe default. Everything here is
opt-in: `PubSubDispatcher` fans events into the in-process broker so
SSE subscribers see live report updates.

Add a new dispatcher by implementing `Dispatcher` and either using
one of these in isolation or composing several via `ChainDispatcher`."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any

from fg_voice.obs.logging import get_logger
from fg_voice.persistence.broker import InProcessBroker, ReportEvent
from fg_voice.persistence.models import OutboxEntry
from fg_voice.persistence.relay import Dispatcher

log = get_logger(__name__)


@dataclass(slots=True)
class PubSubDispatcher:
    """Publishes each outbox row into the in-process broker. Fast: no
    I/O beyond an async queue put per subscriber. Never blocks the
    relay; the broker itself handles slow-consumer backpressure."""

    broker: InProcessBroker

    async def dispatch(self, entry: OutboxEntry) -> None:
        event = ReportEvent(
            event_type=entry.event_type,
            payload=_normalise_payload(entry),
        )
        await self.broker.publish(event)


@dataclass(slots=True)
class ChainDispatcher:
    """Runs several dispatchers in sequence. If one raises, the rest
    still run — but the exception is re-raised at the end so the
    relay records the failure (retry_count bumps). Best-effort
    semantics; a partial-success is treated as a failure so the row
    stays available for retry."""

    dispatchers: list[Dispatcher] = field(default_factory=list)

    async def dispatch(self, entry: OutboxEntry) -> None:
        first_error: BaseException | None = None
        for d in self.dispatchers:
            try:
                await d.dispatch(entry)
            except BaseException as exc:
                log.warning(
                    "outbox.chain.dispatcher_failed",
                    dispatcher=type(d).__name__,
                    error=str(exc),
                )
                first_error = first_error or exc
        if first_error is not None:
            raise first_error


def _normalise_payload(entry: OutboxEntry) -> dict[str, Any]:
    """Add a few relay-level fields on top of the row's payload so
    subscribers can distinguish new events from replays."""
    # `entry.payload` is JSON-typed but SQLAlchemy hands it back as
    # dict[str, Any]; copy so mutations here don't leak.
    payload = dict(entry.payload or {})
    payload.setdefault("outbox_id", entry.id)
    payload.setdefault("event_type", entry.event_type)
    if entry.report_id is not None:
        payload.setdefault("report_id", str(entry.report_id))
    with contextlib.suppress(Exception):
        if entry.created_at is not None:
            payload.setdefault("created_at", entry.created_at.isoformat())
    return payload


__all__ = ["ChainDispatcher", "PubSubDispatcher"]
