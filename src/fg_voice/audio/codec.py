"""μ-law ↔ PCM16 codec and sample-rate helpers.

Twilio Media Streams deliver μ-law-encoded 8 kHz mono PCM in 20 ms
frames — 160 samples = 160 bytes of μ-law. Everything above 4 kHz is
already gone by the time our code sees it; no amount of processing
recovers it. Get the codec right, get the frame boundaries right, and
the rest of the pipeline just works.

Reference: ITU-T G.711 μ-law. Implemented via Python's `audioop` on 3.12
(and `audioop-lts` on 3.13+, pinned in pyproject.toml).

Everything here is stateless and bit-exact — the round-trip identity
`ulaw_to_pcm16(pcm16_to_ulaw(x)) == x` is a pinned test invariant, since
μ-law is a *lossless-through-round-trip* encoding for values that are
themselves in the μ-law-representable range."""

from __future__ import annotations

import audioop
from typing import Final

# Twilio Media Streams frame constants.
TWILIO_SAMPLE_RATE_HZ: Final[int] = 8000
TWILIO_FRAME_MS: Final[int] = 20
TWILIO_FRAME_SAMPLES: Final[int] = TWILIO_SAMPLE_RATE_HZ * TWILIO_FRAME_MS // 1000  # 160
TWILIO_FRAME_BYTES_ULAW: Final[int] = TWILIO_FRAME_SAMPLES  # 1 byte/sample for μ-law
TWILIO_FRAME_BYTES_PCM16: Final[int] = TWILIO_FRAME_SAMPLES * 2  # 2 bytes/sample


def ulaw_to_pcm16(ulaw: bytes) -> bytes:
    """Decode μ-law → signed little-endian 16-bit PCM."""
    return audioop.ulaw2lin(ulaw, 2)  # 2 = sample width in bytes


def pcm16_to_ulaw(pcm16: bytes) -> bytes:
    """Encode signed little-endian 16-bit PCM → μ-law."""
    if len(pcm16) % 2 != 0:
        raise ValueError(f"pcm16 length must be even; got {len(pcm16)}")
    return audioop.lin2ulaw(pcm16, 2)


def resample_pcm16(pcm16: bytes, src_hz: int, dst_hz: int) -> bytes:
    """Resample 16-bit PCM. Uses audioop.ratecv, which is 16-bit safe and
    ships with the interpreter — no scipy dep on the hot path."""
    if src_hz == dst_hz:
        return pcm16
    if len(pcm16) % 2 != 0:
        raise ValueError(f"pcm16 length must be even; got {len(pcm16)}")
    converted, _state = audioop.ratecv(pcm16, 2, 1, src_hz, dst_hz, None)
    return converted


def frame_ulaw(payload: bytes) -> list[bytes]:
    """Chunk an arbitrary-length μ-law payload into Twilio-sized 20 ms
    frames. A trailing partial frame is left as-is (the caller decides
    whether to pad with μ-law silence or defer)."""
    return [
        payload[i : i + TWILIO_FRAME_BYTES_ULAW]
        for i in range(0, len(payload), TWILIO_FRAME_BYTES_ULAW)
    ]


# μ-law "silence" is 0xFF (biased binary offset). Used when we need to
# pad an underflowed frame rather than let it stutter.
ULAW_SILENCE_BYTE: Final[int] = 0xFF


def ulaw_silence(frames: int = 1) -> bytes:
    """`frames` * 20 ms of μ-law silence."""
    return bytes([ULAW_SILENCE_BYTE]) * (TWILIO_FRAME_BYTES_ULAW * frames)
