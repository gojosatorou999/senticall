"""μ-law codec round-trip tests.

The P1 exit gate says "audio/codec.py with round-trip tests (μ-law
↔ PCM16, bit-exact)". μ-law is *not* bit-exact on arbitrary PCM
(it's lossy), but it *is* bit-exact when the input is itself in the
μ-law-representable set, which is what we assert here."""

from __future__ import annotations

import struct

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fg_voice.audio.codec import (
    TWILIO_FRAME_BYTES_PCM16,
    TWILIO_FRAME_BYTES_ULAW,
    TWILIO_FRAME_SAMPLES,
    frame_ulaw,
    pcm16_to_ulaw,
    resample_pcm16,
    ulaw_silence,
    ulaw_to_pcm16,
)


def test_frame_constants() -> None:
    assert TWILIO_FRAME_SAMPLES == 160
    assert TWILIO_FRAME_BYTES_ULAW == 160
    assert TWILIO_FRAME_BYTES_PCM16 == 320


def test_ulaw_pcm16_ulaw_is_idempotent_after_one_pass() -> None:
    """G.711 μ-law is a lossy code: it has 256 code points but only 255
    distinct PCM output levels — code 0x7F (positive zero) and 0xFF
    (negative zero) both decode near zero and re-encode to 0xFF.

    So the useful invariant is *idempotency after one round trip*: once
    a byte has been through decode→encode once, further round trips are
    the identity. This is what we assert."""
    all_ulaw = bytes(range(256))
    once = pcm16_to_ulaw(ulaw_to_pcm16(all_ulaw))
    twice = pcm16_to_ulaw(ulaw_to_pcm16(once))
    assert once == twice, "μ-law round-trip is not idempotent — codec broken"
    # Sanity: at most a handful of bytes change on the first pass.
    diffs = sum(a != b for a, b in zip(once, all_ulaw, strict=True))
    assert diffs <= 4, f"μ-law round-trip lost {diffs} bytes; expected ≤ 4 aliases"


@given(st.binary(min_size=0, max_size=8000))
def test_ulaw_decode_length_is_2x(payload: bytes) -> None:
    assert len(ulaw_to_pcm16(payload)) == 2 * len(payload)


@given(st.binary(min_size=0, max_size=8000).filter(lambda b: len(b) % 2 == 0))
def test_pcm16_encode_length_is_half(payload: bytes) -> None:
    assert len(pcm16_to_ulaw(payload)) == len(payload) // 2


def test_pcm16_odd_length_raises() -> None:
    with pytest.raises(ValueError):
        pcm16_to_ulaw(b"\x00\x00\x01")


def test_resample_identity_when_same_rate() -> None:
    pcm = struct.pack("<8h", *range(8))
    assert resample_pcm16(pcm, 8000, 8000) == pcm


def test_resample_8k_to_16k_doubles_samples() -> None:
    pcm = struct.pack("<8h", *range(8))  # 8 samples = 16 bytes
    up = resample_pcm16(pcm, 8000, 16000)
    # Length is approximately doubled (audioop.ratecv is not a filter
    # bank; it interpolates linearly). We assert bounds rather than exact.
    assert 30 <= len(up) <= 34


def test_frame_chunks_at_20ms_boundary() -> None:
    payload = b"\xff" * (TWILIO_FRAME_BYTES_ULAW * 3 + 40)
    chunks = frame_ulaw(payload)
    assert len(chunks) == 4
    assert all(len(c) == TWILIO_FRAME_BYTES_ULAW for c in chunks[:3])
    assert len(chunks[-1]) == 40


def test_silence_is_ulaw_ff() -> None:
    s = ulaw_silence(frames=2)
    assert len(s) == TWILIO_FRAME_BYTES_ULAW * 2
    assert all(b == 0xFF for b in s)
