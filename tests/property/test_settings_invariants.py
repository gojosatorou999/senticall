"""Property-based invariants on Settings — every value inside its declared
range must load, every value outside must raise."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from fg_voice.config import Settings


@given(threshold=st.floats(min_value=0.0, max_value=1.0, allow_nan=False))
def test_stt_thresholds_accept_valid(threshold: float) -> None:
    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        stt_eot_threshold=threshold,
        stt_eager_eot_threshold=threshold,
        geo_accept_threshold=threshold,
    )
    assert 0.0 <= s.stt_eot_threshold <= 1.0


@given(threshold=st.floats(min_value=1.01, max_value=10.0, allow_nan=False))
def test_stt_thresholds_reject_above_one(threshold: float) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, stt_eot_threshold=threshold)  # type: ignore[call-arg]


@given(duration=st.integers(min_value=30, max_value=1800))
def test_max_call_duration_accepts_valid(duration: int) -> None:
    s = Settings(_env_file=None, max_call_duration_sec=duration)  # type: ignore[call-arg]
    assert s.max_call_duration_sec == duration


@given(attempts=st.integers(min_value=1, max_value=5))
def test_max_attempts_bounded(attempts: int) -> None:
    s = Settings(_env_file=None, max_attempts_per_slot=attempts)  # type: ignore[call-arg]
    assert 1 <= s.max_attempts_per_slot <= 5
