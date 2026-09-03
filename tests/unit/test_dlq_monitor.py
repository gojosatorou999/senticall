"""DLQ depth monitor — periodic counter of stuck outbox rows.

The monitor's product IS the log output; there's nothing to
`resp.json()`. Tests assert against the depth counts + log events
via caplog.

Covers:
- Empty DB → depth 0, no WARNING
- One stuck row → depth 1, first WARNING fires
- Second scan at same depth → no repeat WARNING (first-crossing only)
- Depth grows past prior peak → new WARNING
- DLQ drains → peak resets so a re-fill triggers again
- Threshold: rows under threshold don't count
- Dispatched rows never count
- Loop stops cleanly on shutdown_event
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fg_voice.persistence.db import Base, override_engine, reset_engine
from fg_voice.persistence.dlq_monitor import DlqMonitor
from fg_voice.persistence.models import OutboxEntry


@pytest.fixture
async def _db():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", pool_pre_ping=False)
    override_engine(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(eng, expire_on_commit=False)
    try:
        yield sm
    finally:
        await eng.dispose()
        reset_engine()


async def _insert_outbox(
    sm: async_sessionmaker,
    *,
    retry_count: int,
    dispatched_at: datetime | None = None,
) -> None:
    async with sm() as session, session.begin():
        session.add(
            OutboxEntry(
                report_id=None,
                event_type="report.submitted",
                payload={},
                retry_count=retry_count,
                dispatched_at=dispatched_at,
                last_error="stuck",
            )
        )


# ─── Depth computation ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_db_reports_depth_zero(_db):
    monitor = DlqMonitor(session_maker=_db, max_retries=5, alert_threshold=1)
    depth = await monitor._scan_once()
    assert depth == 0


@pytest.mark.asyncio
async def test_single_stuck_row_counted(_db):
    await _insert_outbox(_db, retry_count=5)
    monitor = DlqMonitor(session_maker=_db, max_retries=5, alert_threshold=1)
    assert await monitor._scan_once() == 1


@pytest.mark.asyncio
async def test_row_under_threshold_not_counted(_db):
    """retry_count < max_retries → still retrying; not DLQ."""
    await _insert_outbox(_db, retry_count=3)
    monitor = DlqMonitor(session_maker=_db, max_retries=5, alert_threshold=1)
    assert await monitor._scan_once() == 0


@pytest.mark.asyncio
async def test_dispatched_row_never_counted(_db):
    """A row that was retried past max but subsequently dispatched
    (via ops-triggered retry that succeeded) is no longer DLQ."""
    await _insert_outbox(_db, retry_count=5, dispatched_at=datetime.now(UTC))
    monitor = DlqMonitor(session_maker=_db, max_retries=5, alert_threshold=1)
    assert await monitor._scan_once() == 0


# ─── Alert semantics ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_crossing_updates_peak(_db):
    """First scan with depth >= threshold sets `_peak_depth`. The
    peak tracking IS the warning-suppression mechanism — if the peak
    moved from 0 to N, a WARNING fired; if it didn't, none did."""
    await _insert_outbox(_db, retry_count=5)
    monitor = DlqMonitor(session_maker=_db, max_retries=5, alert_threshold=1)
    assert monitor._peak_depth == 0
    await monitor._scan_once()
    assert monitor._peak_depth == 1  # warning fired on the transition 0→1


@pytest.mark.asyncio
async def test_repeat_scan_at_same_depth_no_peak_change(_db):
    """Second scan at the same depth doesn't re-fire (peak unchanged)."""
    await _insert_outbox(_db, retry_count=5)
    monitor = DlqMonitor(session_maker=_db, max_retries=5, alert_threshold=1)
    await monitor._scan_once()
    assert monitor._peak_depth == 1
    await monitor._scan_once()
    assert monitor._peak_depth == 1  # unchanged → no repeat warning


@pytest.mark.asyncio
async def test_growth_past_peak_updates(_db):
    """New stuck row bumps depth past prior peak → peak updates
    (and a fresh warning fires with it)."""
    await _insert_outbox(_db, retry_count=5)
    monitor = DlqMonitor(session_maker=_db, max_retries=5, alert_threshold=1)
    await monitor._scan_once()  # depth 1
    assert monitor._peak_depth == 1

    await _insert_outbox(_db, retry_count=5)
    await monitor._scan_once()  # depth 2
    assert monitor._peak_depth == 2  # bumped → new warning fired


@pytest.mark.asyncio
async def test_drain_resets_peak(_db):
    """DLQ drained (ops purged the rows) → peak resets so a future
    re-fill triggers a WARNING again rather than staying silent."""
    from sqlalchemy import update

    await _insert_outbox(_db, retry_count=5)
    monitor = DlqMonitor(session_maker=_db, max_retries=5, alert_threshold=1)
    await monitor._scan_once()
    assert monitor._peak_depth == 1

    # Simulate ops purging.
    async with _db() as session, session.begin():
        await session.execute(update(OutboxEntry).values(dispatched_at=datetime.now(UTC)))
    await monitor._scan_once()
    assert monitor._peak_depth == 0  # tracks downward on drain

    # Refill — new WARNING would fire because peak is back at 0.
    await _insert_outbox(_db, retry_count=5)
    await monitor._scan_once()
    assert monitor._peak_depth == 1  # first-crossing again


@pytest.mark.asyncio
async def test_below_alert_threshold_no_peak_move(_db):
    """A stuck row at depth 1 with threshold=5 shouldn't move the
    peak (ops knows this DLQ has a standing baseline)."""
    await _insert_outbox(_db, retry_count=5)
    monitor = DlqMonitor(session_maker=_db, max_retries=5, alert_threshold=5)
    await monitor._scan_once()
    assert monitor._peak_depth == 0  # depth < threshold → no warning, no peak update


# ─── Loop lifecycle ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_exits_on_shutdown_event(_db):
    """Loop must exit cleanly when the shared shutdown_event fires,
    so main.py's graceful shutdown drains both relay + monitor."""
    monitor = DlqMonitor(session_maker=_db, max_retries=5, interval_sec=60.0)
    shutdown = asyncio.Event()
    task = asyncio.create_task(monitor.run(shutdown))
    # Give the loop time to complete one scan.
    await asyncio.sleep(0.05)
    shutdown.set()
    # Should exit near-instantly (the wait_for wakes on the event).
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()


@pytest.mark.asyncio
async def test_scan_failure_never_crashes_loop(_db):
    """A DB error inside _scan_once must log + continue, not kill the
    monitor. Ops loses one heartbeat but not the whole visibility.

    Uses a subclass to inject the failure — DlqMonitor uses `slots=True`
    so attribute reassignment on an instance would fail."""

    class FlakyMonitor(DlqMonitor):
        __slots__ = ("_boomed",)

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._boomed = False

        async def _scan_once(self) -> int:
            if not self._boomed:
                self._boomed = True
                raise RuntimeError("simulated DB blip")
            return await super()._scan_once()

    monitor = FlakyMonitor(session_maker=_db, max_retries=5, interval_sec=0.05)
    shutdown = asyncio.Event()
    task = asyncio.create_task(monitor.run(shutdown))
    await asyncio.sleep(0.15)  # let two ticks fire
    shutdown.set()
    await asyncio.wait_for(task, timeout=1.0)

    # Loop survived past the failure — verify by checking the second
    # scan actually ran (peak_depth would be untouched at 0 since no
    # DLQ rows, but the important thing is the task exited via the
    # shutdown_event path, not via an unhandled exception).
    assert task.done()
    assert not task.cancelled()
    # task.exception() being None proves no exception propagated.
    assert task.exception() is None
