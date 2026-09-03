"""Unit tests for CircuitBreaker."""

from __future__ import annotations

import time

import pytest

from fg_voice.pipeline.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
)


def _cb(*, threshold: int = 3, reset_sec: float = 60.0) -> CircuitBreaker:
    return CircuitBreaker(failure_threshold=threshold, reset_sec=reset_sec, name="test")


# ── Initial state ──────────────────────────────────────────────────────


def test_initial_state_closed() -> None:
    cb = _cb()
    assert cb.state is CircuitState.CLOSED
    assert cb.is_usable is True


def test_before_call_does_not_raise_when_closed() -> None:
    cb = _cb()
    cb.before_call()  # must not raise


# ── Failure counting ───────────────────────────────────────────────────


def test_does_not_open_before_threshold() -> None:
    cb = _cb(threshold=3)
    for _ in range(2):
        cb.on_failure(RuntimeError("boom"))
    assert cb.state is CircuitState.CLOSED


def test_opens_at_threshold() -> None:
    cb = _cb(threshold=3)
    for _ in range(3):
        cb.on_failure(RuntimeError("boom"))
    assert cb.state is CircuitState.OPEN
    assert cb.is_usable is False


def test_open_raises_on_before_call() -> None:
    cb = _cb(threshold=1, reset_sec=999.0)
    cb.on_failure(RuntimeError("boom"))
    with pytest.raises(CircuitOpenError) as exc_info:
        cb.before_call()
    assert exc_info.value.name == "test"
    assert exc_info.value.reset_in_sec > 0


# ── Recovery ──────────────────────────────────────────────────────────


def test_success_resets_to_closed() -> None:
    cb = _cb(threshold=2)
    cb.on_failure(RuntimeError("x"))
    cb.on_failure(RuntimeError("y"))
    assert cb.state is CircuitState.OPEN
    # Simulate reset window passing
    cb._opened_at = time.monotonic() - 61.0  # type: ignore[attr-defined]
    # Now it should transition to HALF_OPEN
    assert cb.state is CircuitState.HALF_OPEN
    assert cb.is_usable is True
    cb.on_success()
    assert cb.state is CircuitState.CLOSED


def test_half_open_failure_reopens() -> None:
    cb = _cb(threshold=1, reset_sec=0.0)
    cb.on_failure(RuntimeError("first"))
    # Force half-open by expiring the reset window
    cb._opened_at = time.monotonic() - 1.0  # type: ignore[attr-defined]
    assert cb.state is CircuitState.HALF_OPEN
    cb.on_failure(RuntimeError("probe failed"))
    assert cb.state is CircuitState.OPEN


def test_success_resets_failure_counter() -> None:
    cb = _cb(threshold=3)
    cb.on_failure(RuntimeError("x"))
    cb.on_failure(RuntimeError("y"))
    cb.on_success()
    # Counter reset — should need 3 more failures to open.
    cb.on_failure(RuntimeError("a"))
    cb.on_failure(RuntimeError("b"))
    assert cb.state is CircuitState.CLOSED
    cb.on_failure(RuntimeError("c"))
    assert cb.state is CircuitState.OPEN
