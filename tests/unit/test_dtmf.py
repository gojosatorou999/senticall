"""DTMF buffer + digit-to-slot mapping."""

from __future__ import annotations

import pytest

from fg_voice.telephony.dtmf import DtmfBuffer, DtmfError, map_digit


def test_push_and_take_returns_digits_in_order():
    buf = DtmfBuffer()
    for d in "123":
        buf.push(d)
    assert buf.take() == "123"
    assert buf.peek() == ""


def test_take_clears_buffer():
    buf = DtmfBuffer()
    buf.push("1")
    _ = buf.take()
    assert len(buf) == 0


def test_invalid_digit_raises():
    buf = DtmfBuffer()
    with pytest.raises(DtmfError):
        buf.push("A")


def test_overflow_drops_oldest():
    buf = DtmfBuffer(max_length=3)
    for d in "1234":
        buf.push(d)
    assert buf.take() == "234"


def test_clear_wipes_the_buffer():
    buf = DtmfBuffer()
    buf.push("9")
    buf.clear()
    assert len(buf) == 0


def test_map_digit_returns_canonical_slot_value():
    m = {"1": "storm", "2": "sludge_oil"}
    assert map_digit("1", m) == "storm"
    assert map_digit("2", m) == "sludge_oil"


def test_map_digit_unknown_returns_none():
    m = {"1": "storm"}
    assert map_digit("9", m) is None


def test_map_digit_disarmed_returns_none():
    assert map_digit("1", None) is None
