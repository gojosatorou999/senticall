"""OutboxRelay unit tests.

In-memory SQLite so we can prove the poll/dispatch/mark-dispatched
lifecycle without Postgres. `FOR UPDATE SKIP LOCKED` is a no-op on
SQLite; the relay's fallback path swallows the DatabaseError."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from fg_voice.persistence.db import (
    Base,
    get_session_maker,
    override_engine,
    reset_engine,
)
from fg_voice.persistence.models import OutboxEntry, Report
from fg_voice.persistence.outbox import OutboxEventType
from fg_voice.persistence.relay import (
    DEFAULT_MAX_RETRIES,
    LogDispatcher,
    OutboxRelay,
    RecordingDispatcher,
)


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", pool_pre_ping=False)
    override_engine(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()
    reset_engine()


async def _seed_outbox_row(event_type: str = "report.submitted") -> int:
    """Add one pending outbox row and return its id."""
    async with get_session_maker()() as session, session.begin():
        row = OutboxEntry(event_type=event_type, payload={"x": 1})
        session.add(row)
        await session.flush()
        return row.id


# ─── drain_once ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drain_once_empty_returns_zero(engine):
    relay = OutboxRelay(dispatcher=RecordingDispatcher())
    assert await relay.drain_once() == 0
    assert relay.stats.polls == 1
    assert relay.stats.processed == 0


@pytest.mark.asyncio
async def test_drain_once_dispatches_and_marks_row(engine):
    dispatcher = RecordingDispatcher()
    relay = OutboxRelay(dispatcher=dispatcher)
    row_id = await _seed_outbox_row()

    processed = await relay.drain_once()
    assert processed == 1
    assert relay.stats.succeeded == 1
    assert relay.stats.failed == 0
    assert len(dispatcher.dispatched) == 1
    assert dispatcher.dispatched[0].id == row_id

    async with get_session_maker()() as session:
        row = await session.get(OutboxEntry, row_id)
        assert row is not None
        assert row.dispatched_at is not None
        assert row.retry_count == 0
        assert row.last_error is None


@pytest.mark.asyncio
async def test_drain_once_failure_bumps_retry_and_stores_error(engine):
    dispatcher = RecordingDispatcher()
    dispatcher.raise_next = True
    dispatcher.next_error = "downstream sad"
    relay = OutboxRelay(dispatcher=dispatcher)
    row_id = await _seed_outbox_row()

    await relay.drain_once()
    assert relay.stats.failed == 1
    assert relay.stats.succeeded == 0

    async with get_session_maker()() as session:
        row = await session.get(OutboxEntry, row_id)
        assert row is not None
        assert row.dispatched_at is None
        assert row.retry_count == 1
        assert row.last_error == "downstream sad"


@pytest.mark.asyncio
async def test_drain_once_retries_row_next_poll(engine):
    dispatcher = RecordingDispatcher()
    dispatcher.raise_next = True
    relay = OutboxRelay(dispatcher=dispatcher)
    row_id = await _seed_outbox_row()

    await relay.drain_once()  # first attempt fails
    await relay.drain_once()  # second attempt succeeds
    assert relay.stats.succeeded == 1
    assert relay.stats.failed == 1

    async with get_session_maker()() as session:
        row = await session.get(OutboxEntry, row_id)
        assert row is not None
        assert row.dispatched_at is not None
        # retry_count reflects the ONE prior failure
        assert row.retry_count == 1


@pytest.mark.asyncio
async def test_row_past_max_retries_is_not_reclaimed(engine):
    """A dead-lettered row (retry_count == max_retries) never comes
    back on subsequent polls — it needs manual intervention."""
    dispatcher = RecordingDispatcher()
    relay = OutboxRelay(dispatcher=dispatcher, max_retries=DEFAULT_MAX_RETRIES)
    # Seed a row that's already past the cap.
    async with get_session_maker()() as session, session.begin():
        row = OutboxEntry(
            event_type="stale.event",
            payload={},
            retry_count=DEFAULT_MAX_RETRIES,
            last_error="tried too many times",
        )
        session.add(row)

    processed = await relay.drain_once()
    assert processed == 0
    assert dispatcher.dispatched == []


@pytest.mark.asyncio
async def test_dead_letter_stat_bumps_when_last_retry_fails(engine):
    dispatcher = RecordingDispatcher()
    relay = OutboxRelay(dispatcher=dispatcher, max_retries=2)
    await _seed_outbox_row()

    dispatcher.raise_next = True
    await relay.drain_once()  # attempt 1 fails → retry_count=1
    dispatcher.raise_next = True
    await relay.drain_once()  # attempt 2 fails → retry_count=2 → dead-letter

    assert relay.stats.dead_lettered == 1

    # And a third poll finds nothing.
    processed = await relay.drain_once()
    assert processed == 0


@pytest.mark.asyncio
async def test_batch_size_caps_rows_per_poll(engine):
    dispatcher = RecordingDispatcher()
    relay = OutboxRelay(dispatcher=dispatcher, batch_size=2)
    for _ in range(5):
        await _seed_outbox_row()

    first = await relay.drain_once()
    second = await relay.drain_once()
    assert first == 2
    assert second == 2
    # Third poll finishes the remainder.
    third = await relay.drain_once()
    assert third == 1
    total = first + second + third
    assert total == 5
    assert relay.stats.succeeded == 5


@pytest.mark.asyncio
async def test_run_loop_stops_on_shutdown_event(engine):
    relay = OutboxRelay(dispatcher=RecordingDispatcher(), poll_interval_sec=0.05)
    shutdown = asyncio.Event()

    async def _stopper() -> None:
        await asyncio.sleep(0.15)
        shutdown.set()

    await asyncio.gather(relay.run(shutdown), _stopper())
    # At least one poll should have completed.
    assert relay.stats.polls >= 1


# ─── LogDispatcher smoke test ────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_dispatcher_never_raises(engine):
    """The default dispatcher shouldn't be able to break a poll cycle
    (it's the safe default before real dispatchers are wired)."""
    relay = OutboxRelay(dispatcher=LogDispatcher())
    await _seed_outbox_row(OutboxEventType.REPORT_SUBMITTED)
    await relay.drain_once()
    assert relay.stats.succeeded == 1


# ─── End-to-end with the sink to prove they compose ───────────────────


@pytest.mark.asyncio
async def test_sink_write_then_relay_dispatch(engine):
    """`SqlReportSink` writes → `OutboxRelay` drains → dispatcher sees
    the entry. Proves the two halves of §2.3 work together."""
    from fg_voice.conversation.sql_report_sink import SqlReportSink
    from fg_voice.conversation.state import CallState, Slot, SlotValue

    sink = SqlReportSink()
    state = CallState(call_sid="CA_e2e", caller_hash="h")
    state.set_slot(Slot.HAZARD_TYPE, SlotValue(value="storm", confidence=0.9, source="asr"))
    await sink.write(state)

    dispatcher = RecordingDispatcher()
    relay = OutboxRelay(dispatcher=dispatcher)
    processed = await relay.drain_once()

    assert processed == 1
    assert len(dispatcher.dispatched) == 1
    entry = dispatcher.dispatched[0]
    assert entry.event_type == OutboxEventType.REPORT_SUBMITTED
    assert entry.payload["hazard_type"] == "storm"

    # And the report row is present alongside the drained outbox row.
    async with get_session_maker()() as session:
        row = await session.get(Report, state.report_id)
        assert row is not None
