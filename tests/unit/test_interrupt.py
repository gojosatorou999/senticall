"""Barge-in coordinator behaviour + lockout guard."""

from __future__ import annotations

import asyncio

import pytest

from fg_voice.pipeline.interrupt import BARGE_IN_LOCKOUT_MS, InterruptController


async def _noop_send(_: str) -> None:
    return None


class _RecordingSender:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, msg: str) -> None:
        self.sent.append(msg)


async def _long_playback() -> None:
    await asyncio.sleep(10)


async def _quick_playback() -> None:
    await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_barge_in_cancels_task_and_sends_clear():
    sender = _RecordingSender()
    ctrl = InterruptController(stream_sid="MZstream", send_clear=sender)
    task = asyncio.create_task(_long_playback())
    ctrl.track(task)
    # Wait past the lockout so the barge-in is honoured.
    await asyncio.sleep(BARGE_IN_LOCKOUT_MS / 1000 + 0.05)

    result = await ctrl.on_start_of_turn()

    assert result.fired
    assert task.cancelled() or task.done()
    assert any('"event":"clear"' in msg for msg in sender.sent)
    assert ctrl.stats.fired == 1


@pytest.mark.asyncio
async def test_barge_in_no_op_without_playback():
    ctrl = InterruptController(stream_sid="MZstream", send_clear=_noop_send)
    result = await ctrl.on_start_of_turn()
    assert not result.fired
    assert result.reason == "no_playback_in_flight"


@pytest.mark.asyncio
async def test_barge_in_rejected_within_lockout():
    """StartOfTurn during the first 150 ms of our own audio is treated
    as echo, not barge-in (§7.4)."""
    sender = _RecordingSender()
    ctrl = InterruptController(stream_sid="MZstream", send_clear=sender)
    task = asyncio.create_task(_long_playback())
    ctrl.track(task)
    # Immediately (before the lockout expires).
    result = await ctrl.on_start_of_turn()
    assert not result.fired
    assert result.reason == "lockout"
    assert ctrl.stats.false_barge_ins == 1
    assert not sender.sent  # no clear was sent

    # Cleanup.
    task.cancel()


@pytest.mark.asyncio
async def test_release_clears_state_after_natural_end():
    ctrl = InterruptController(stream_sid="MZstream", send_clear=_noop_send)
    task = asyncio.create_task(_quick_playback())
    ctrl.track(task)
    await task
    ctrl.release()
    result = await ctrl.on_start_of_turn()
    assert not result.fired
    assert result.reason == "no_playback_in_flight"


@pytest.mark.asyncio
async def test_drain_queue_callback_invoked_on_barge_in():
    sender = _RecordingSender()
    ctrl = InterruptController(stream_sid="MZstream", send_clear=sender)
    drained = {"called": False}

    def _drain() -> None:
        drained["called"] = True

    task = asyncio.create_task(_long_playback())
    ctrl.track(task, drain_queue=_drain)
    await asyncio.sleep(BARGE_IN_LOCKOUT_MS / 1000 + 0.05)
    await ctrl.on_start_of_turn()
    assert drained["called"]
