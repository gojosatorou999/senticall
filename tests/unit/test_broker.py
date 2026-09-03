"""InProcessBroker + PubSubDispatcher unit tests."""

from __future__ import annotations

import asyncio

import pytest

from fg_voice.persistence.broker import LAGGED, InProcessBroker, ReportEvent
from fg_voice.persistence.dispatchers import PubSubDispatcher
from fg_voice.persistence.models import OutboxEntry


@pytest.mark.asyncio
async def test_subscriber_gets_published_event():
    broker = InProcessBroker()
    async with broker.subscribe() as q:
        await broker.publish(ReportEvent(event_type="report.submitted", payload={"a": 1}))
        item = await asyncio.wait_for(q.get(), timeout=1.0)
        assert isinstance(item, ReportEvent)
        assert item.event_type == "report.submitted"
        assert item.payload == {"a": 1}


@pytest.mark.asyncio
async def test_publish_fans_out_to_multiple_subscribers():
    broker = InProcessBroker()
    async with broker.subscribe() as q1, broker.subscribe() as q2:
        await broker.publish(ReportEvent("x", {"n": 1}))
        got1 = await asyncio.wait_for(q1.get(), timeout=1.0)
        got2 = await asyncio.wait_for(q2.get(), timeout=1.0)
        assert isinstance(got1, ReportEvent) and got1.payload == {"n": 1}
        assert isinstance(got2, ReportEvent) and got2.payload == {"n": 1}


@pytest.mark.asyncio
async def test_unsubscribe_on_scope_exit():
    broker = InProcessBroker()
    async with broker.subscribe():
        assert broker.subscriber_count == 1
    assert broker.subscriber_count == 0


@pytest.mark.asyncio
async def test_slow_subscriber_gets_lagged_marker_not_deadlock():
    """A subscriber that never drains its queue must not block the
    publisher. Once the queue is full we replace the oldest with a
    LAGGED sentinel so the subscriber knows to reconnect."""
    broker = InProcessBroker(queue_max=2)
    async with broker.subscribe() as q:
        # Fill + overflow.
        await broker.publish(ReportEvent("a", {"n": 1}))
        await broker.publish(ReportEvent("a", {"n": 2}))
        await broker.publish(ReportEvent("a", {"n": 3}))  # overflows

        items = []
        for _ in range(2):
            items.append(await asyncio.wait_for(q.get(), timeout=1.0))
        # LAGGED must be somewhere in what we got — exact position
        # depends on when queue.get_nowait fired.
        assert LAGGED in items or any(isinstance(x, type(LAGGED)) for x in items)


@pytest.mark.asyncio
async def test_publish_with_no_subscribers_is_noop():
    broker = InProcessBroker()
    # Should not raise.
    await broker.publish(ReportEvent("orphan", {}))
    assert broker.subscriber_count == 0


# ─── PubSubDispatcher ────────────────────────────────────────────────


def _outbox_entry(**overrides) -> OutboxEntry:
    """Build an OutboxEntry without going through the DB — the
    dispatcher shouldn't care about SA sessions."""
    e = OutboxEntry(event_type="report.submitted", payload={"hazard_type": "storm"})
    e.id = overrides.get("id", 42)
    return e


@pytest.mark.asyncio
async def test_pubsub_dispatcher_publishes_normalised_payload():
    broker = InProcessBroker()
    dispatcher = PubSubDispatcher(broker=broker)
    entry = _outbox_entry()

    async with broker.subscribe() as q:
        await dispatcher.dispatch(entry)
        event = await asyncio.wait_for(q.get(), timeout=1.0)
        assert isinstance(event, ReportEvent)
        assert event.event_type == "report.submitted"
        assert event.payload["outbox_id"] == 42
        assert event.payload["hazard_type"] == "storm"


@pytest.mark.asyncio
async def test_pubsub_dispatcher_never_blocks_on_no_subscribers():
    broker = InProcessBroker()
    dispatcher = PubSubDispatcher(broker=broker)
    # Should return promptly even though nothing is subscribed.
    await asyncio.wait_for(dispatcher.dispatch(_outbox_entry()), timeout=1.0)
