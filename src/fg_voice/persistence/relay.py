"""Transactional outbox relay (§2.3 second half).

The `SqlReportSink` writes an `OutboxEntry` in the same transaction as
the `Report`. Something has to consume those rows and turn them into
side-effects — SSE fan-out, alerts, CSV projection. That "something"
is this module.

Design:

- Poll the `outbox` table for rows with `dispatched_at IS NULL` and
  `retry_count < max_retries`.
- Claim them atomically. On PostgreSQL that means `FOR UPDATE SKIP
  LOCKED` so multiple relay replicas never dispatch the same row twice.
  On SQLite (tests) locking is skipped — a single-process test never
  races, and the code path is exercised the same way.
- Call the injected `Dispatcher`. On success, mark `dispatched_at`.
  On failure, bump `retry_count`, store `last_error`, and leave
  `dispatched_at` NULL so the next poll re-attempts.
- Respect a `max_retries` cap so a poison message doesn't loop forever.
  Rows past the cap are logged and left in place — human intervention
  clears them.

Not part of this module: the actual event side-effects. Ship
`LogDispatcher` as the safe default (writes an OTel-friendly log
entry) so a fresh deploy has *something* draining the queue while the
real dispatchers (SSE, webhooks, alerts) get wired up."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Protocol

from sqlalchemy import select
from sqlalchemy.exc import DatabaseError
from sqlalchemy.ext.asyncio import AsyncSession as SqlAsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from fg_voice.obs.logging import get_logger
from fg_voice.persistence.db import get_session_maker
from fg_voice.persistence.models import OutboxEntry

log = get_logger(__name__)

DEFAULT_POLL_INTERVAL_SEC: Final[float] = 1.0
DEFAULT_BATCH_SIZE: Final[int] = 32
DEFAULT_MAX_RETRIES: Final[int] = 5


class DispatchError(Exception):
    """Raised by a Dispatcher when the side-effect failed. The relay
    catches this, bumps `retry_count`, and stores the message. Any
    other exception is treated the same way; DispatchError just makes
    the contract explicit."""


class Dispatcher(Protocol):
    """One side-effect handler. Must be idempotent — the relay may
    invoke the same event twice under crash recovery."""

    async def dispatch(self, entry: OutboxEntry) -> None: ...


@dataclass(slots=True)
class LogDispatcher:
    """Safe default. Emits a structured log entry so the row is
    audit-traceable, then returns. A real deploy replaces this with
    one that fans out to SSE / alerts / CSV projection."""

    async def dispatch(self, entry: OutboxEntry) -> None:
        log.info(
            "outbox.dispatched",
            event_type=entry.event_type,
            outbox_id=entry.id,
            report_id=str(entry.report_id) if entry.report_id else None,
            payload_keys=sorted(entry.payload.keys()) if entry.payload else [],
        )


@dataclass(slots=True)
class RecordingDispatcher:
    """Test double. Records every dispatched entry; can be flipped to
    raise on the next call to exercise the failure path."""

    dispatched: list[OutboxEntry] = field(default_factory=list)
    raise_next: bool = False
    next_error: str = "boom"

    async def dispatch(self, entry: OutboxEntry) -> None:
        if self.raise_next:
            self.raise_next = False
            raise DispatchError(self.next_error)
        self.dispatched.append(entry)


@dataclass(slots=True)
class RelayStats:
    """Counters that mount up over the lifetime of the relay."""

    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    dead_lettered: int = 0
    polls: int = 0


class OutboxRelay:
    """Long-running polling loop. Consumers instantiate once at app
    boot and call `.run(shutdown_event)` in a background task."""

    def __init__(
        self,
        dispatcher: Dispatcher,
        *,
        session_maker: async_sessionmaker[SqlAsyncSession] | None = None,
        poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._dispatcher = dispatcher
        self._sm = session_maker or get_session_maker()
        self._poll_interval_sec = poll_interval_sec
        self._batch_size = batch_size
        self._max_retries = max_retries
        self._stats = RelayStats()

    @property
    def stats(self) -> RelayStats:
        return self._stats

    async def run(self, shutdown_event: asyncio.Event) -> None:
        """Loop until `shutdown_event` fires. When there's no work,
        sleep up to `poll_interval_sec`; when there is work, drain as
        fast as batches allow."""
        log.info(
            "outbox.relay.starting",
            poll_interval_sec=self._poll_interval_sec,
            batch_size=self._batch_size,
            max_retries=self._max_retries,
        )
        while not shutdown_event.is_set():
            try:
                processed = await self.drain_once()
            except Exception as exc:
                log.exception("outbox.relay.drain_failed", error=str(exc))
                processed = 0
            if processed == 0:
                # No work — wait for either a shutdown or the poll tick.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(shutdown_event.wait(), timeout=self._poll_interval_sec)
        log.info(
            "outbox.relay.stopped",
            processed=self._stats.processed,
            succeeded=self._stats.succeeded,
            failed=self._stats.failed,
            dead_lettered=self._stats.dead_lettered,
            polls=self._stats.polls,
        )

    async def drain_once(self) -> int:
        """One poll → dispatch cycle. Returns the number of rows
        processed (success + failure both count)."""
        self._stats.polls += 1
        async with self._sm() as session, session.begin():
            claimed = await self._claim_batch(session)
            if not claimed:
                return 0
            for row in claimed:
                await self._handle_one(row)
        return len(claimed)

    async def _claim_batch(self, session: SqlAsyncSession) -> list[OutboxEntry]:
        """`FOR UPDATE SKIP LOCKED` on Postgres so a second replica
        doesn't grab the same row. SQLite silently ignores the hint,
        which is fine for tests (single process)."""
        stmt = (
            select(OutboxEntry)
            .where(
                OutboxEntry.dispatched_at.is_(None),
                OutboxEntry.retry_count < self._max_retries,
            )
            .order_by(OutboxEntry.created_at)
            .limit(self._batch_size)
        )
        with contextlib.suppress(NotImplementedError):  # pragma: no cover — old dialects
            stmt = stmt.with_for_update(skip_locked=True)
        try:
            result = await session.scalars(stmt)
        except DatabaseError:
            # Some dialects (SQLite) reject `FOR UPDATE SKIP LOCKED`
            # at execute-time rather than compile-time. Fall back to
            # the plain select — safe in tests, would need a real
            # leader lock in a multi-writer prod deploy.
            stmt = (
                select(OutboxEntry)
                .where(
                    OutboxEntry.dispatched_at.is_(None),
                    OutboxEntry.retry_count < self._max_retries,
                )
                .order_by(OutboxEntry.created_at)
                .limit(self._batch_size)
            )
            result = await session.scalars(stmt)
        return list(result.all())

    async def _handle_one(self, entry: OutboxEntry) -> None:
        self._stats.processed += 1
        try:
            await self._dispatcher.dispatch(entry)
        except Exception as exc:
            # Any exception counts as a dispatch failure. We store the
            # message on the row for post-hoc diagnosis.
            entry.retry_count += 1
            entry.last_error = str(exc)[:1000]
            self._stats.failed += 1
            if entry.retry_count >= self._max_retries:
                self._stats.dead_lettered += 1
                log.warning(
                    "outbox.dead_lettered",
                    outbox_id=entry.id,
                    event_type=entry.event_type,
                    retry_count=entry.retry_count,
                    last_error=entry.last_error,
                )
            return
        entry.dispatched_at = datetime.now(UTC)
        self._stats.succeeded += 1


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_POLL_INTERVAL_SEC",
    "DispatchError",
    "Dispatcher",
    "LogDispatcher",
    "OutboxRelay",
    "RecordingDispatcher",
    "RelayStats",
]
