"""DLQ depth monitor.

The relay logs `outbox.dead_lettered` when a row hits max_retries,
but there's no ongoing visibility — if 50 rows dead-letter over a
weekend, ops sees 50 line-items scattered across the log stream
rather than a single "DLQ depth is now 50" heartbeat.

This module runs a periodic background task that:

- Counts rows matching the DLQ condition (`dispatched_at IS NULL AND
  retry_count >= max_retries`).
- Logs the depth at INFO with a stable event key
  (`outbox.dlq.depth`) so ops can graph / alert on the metric.
- Logs at WARNING when the depth first crosses the alert threshold
  (default 1) so a single stuck row surfaces immediately, and again
  each time the depth grows past a prior peak.

Deliberately does NOT:

- Fire webhook / SNS alerts. Log-based alerting is the standard ops
  pattern for this repo; a webhook alert layer would duplicate what
  `AlertDispatcher` already provides and add a second failure mode
  (webhook down → alerts silently missing).
- Auto-retry / auto-purge. Ops decides. Retry/purge lives on the
  admin API (`api/routes_dlq.py`).
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession as SqlAsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from fg_voice.obs.logging import get_logger
from fg_voice.persistence.db import get_session_maker
from fg_voice.persistence.models import OutboxEntry
from fg_voice.persistence.relay import DEFAULT_MAX_RETRIES

log = get_logger(__name__)

DEFAULT_INTERVAL_SEC: Final[float] = 60.0
DEFAULT_ALERT_THRESHOLD: Final[int] = 1


@dataclass(slots=True)
class DlqMonitor:
    """Periodic scanner. Instantiate once in main.py's lifespan and
    hand off to `asyncio.create_task(monitor.run(shutdown_event))`."""

    interval_sec: float = DEFAULT_INTERVAL_SEC
    alert_threshold: int = DEFAULT_ALERT_THRESHOLD
    max_retries: int = DEFAULT_MAX_RETRIES
    session_maker: async_sessionmaker[SqlAsyncSession] | None = None
    # Mutable state — highest depth observed so far. Reset to 0 when
    # the DLQ drains (via ops retry/purge or all rows dispatching),
    # so a re-fill triggers a fresh WARNING.
    _peak_depth: int = field(default=0, init=False)

    async def run(self, shutdown_event: asyncio.Event) -> None:
        """Long-running loop. Sleeps `interval_sec` between polls;
        exits cleanly on shutdown_event."""
        log.info(
            "outbox.dlq_monitor.starting",
            interval_sec=self.interval_sec,
            alert_threshold=self.alert_threshold,
            max_retries=self.max_retries,
        )
        while not shutdown_event.is_set():
            try:
                await self._scan_once()
            except Exception as exc:
                log.exception("outbox.dlq_monitor.scan_failed", error=str(exc))
            # Race-safe sleep — wakes early on shutdown so tests don't
            # need to wait a full interval to see the stop.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(shutdown_event.wait(), timeout=self.interval_sec)
        log.info("outbox.dlq_monitor.stopped", peak_depth=self._peak_depth)

    async def _scan_once(self) -> int:
        """One poll cycle. Returns the observed depth for testability;
        the log side-effect is the real product."""
        sm = self.session_maker or get_session_maker()
        async with sm() as session:
            depth = await self._depth(session)

        # Always log at INFO so dashboards get a heartbeat even at
        # depth 0. Downstream can filter on `depth > 0` if verbosity
        # matters.
        log.info("outbox.dlq.depth", depth=depth, threshold=self.alert_threshold)

        # First-crossing WARNING: a new stuck row (or a re-fill after
        # a drain) triggers ops attention exactly once, not every
        # tick after that.
        if depth >= self.alert_threshold and depth > self._peak_depth:
            log.warning(
                "outbox.dlq.depth_exceeded",
                depth=depth,
                previous_peak=self._peak_depth,
                threshold=self.alert_threshold,
            )
            self._peak_depth = depth
        elif depth < self._peak_depth:
            # DLQ drained (or partially drained) — track downward so a
            # future refill triggers a fresh warning.
            self._peak_depth = depth

        return depth

    async def _depth(self, session: SqlAsyncSession) -> int:
        """One COUNT — light enough at the volumes this hits (rare
        events, one row per stuck message) that we don't need a
        pre-aggregated stats table."""
        stmt = (
            select(func.count())
            .select_from(OutboxEntry)
            .where(
                and_(
                    OutboxEntry.dispatched_at.is_(None),
                    OutboxEntry.retry_count >= self.max_retries,
                )
            )
        )
        result = await session.scalar(stmt)
        return int(result or 0)


__all__ = ["DEFAULT_ALERT_THRESHOLD", "DEFAULT_INTERVAL_SEC", "DlqMonitor"]
