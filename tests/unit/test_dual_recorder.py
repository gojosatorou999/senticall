"""Dual pre/post-denoise recorder — spec §9.1 tee line.

Coverage:
- DualRecorder Protocol is runtime-checkable
- NoOpDualRecorder swallows all calls
- S3DualRecorder buffers frames; finalise emits one PUT per stream
- Empty buffers → no PUT at all (avoid empty-file pollution)
- Buffer cap enforced with a single WARNING (no log spam)
- Finalise is idempotent
- S3 key layout follows the date-shard convention
- Upload failure never raises (never lets recording surface to caller)
- WAV wrapper produces something the stdlib wave module can read back
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass, field

import pytest

from fg_voice.audio.dual_recorder import (
    DualRecorder,
    InMemoryS3Uploader,
    NoOpDualRecorder,
    S3DualRecorder,
    build_dual_recorder,
)

# ─── Protocol shape ──────────────────────────────────────────────────


def test_dual_recorder_protocol_runtime_checkable():
    assert isinstance(NoOpDualRecorder(), DualRecorder)


def test_s3_recorder_conforms_to_protocol():
    r = S3DualRecorder(bucket="b", uploader=InMemoryS3Uploader())
    assert isinstance(r, DualRecorder)


# ─── NoOp behaviour ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_noop_is_silent():
    r = NoOpDualRecorder()
    r.push_raw_ulaw(b"\xff" * 100)
    r.push_clean_pcm16(b"\x00" * 200)
    await r.finalise("CA1")  # should not raise


# ─── S3 recorder happy path ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_s3_recorder_uploads_both_streams_on_finalise():
    uploader = InMemoryS3Uploader()
    r = S3DualRecorder(bucket="fg-rec", uploader=uploader)
    r.push_raw_ulaw(b"\xff" * 320)
    r.push_clean_pcm16(b"\x00\x01" * 160)
    await r.finalise("CA_test")

    # Two PUTs: raw + clean.
    assert len(uploader.puts) == 2
    keys = [put[1] for put in uploader.puts]
    assert any(k.endswith("/raw.wav") for k in keys)
    assert any(k.endswith("/clean.wav") for k in keys)
    # Bucket matches.
    assert all(put[0] == "fg-rec" for put in uploader.puts)
    # Content-type set correctly.
    assert all(put[3] == "audio/wav" for put in uploader.puts)


@pytest.mark.asyncio
async def test_s3_recorder_skips_empty_buffers():
    """Empty raw/clean buffers → zero PUTs. Prevents empty-file
    pollution of the recordings bucket for calls that hung up before
    any audio flowed."""
    uploader = InMemoryS3Uploader()
    r = S3DualRecorder(bucket="fg-rec", uploader=uploader)
    await r.finalise("CA_hangup")
    assert uploader.puts == []


@pytest.mark.asyncio
async def test_s3_recorder_uploads_only_populated_streams():
    """Raw pushed but no clean → one PUT (raw.wav), not two. Prevents
    zero-byte clean.wav from being uploaded when the denoise path was
    off but raw is still on."""
    uploader = InMemoryS3Uploader()
    r = S3DualRecorder(bucket="fg-rec", uploader=uploader)
    r.push_raw_ulaw(b"\xff" * 320)
    await r.finalise("CA_raw_only")
    assert len(uploader.puts) == 1
    assert uploader.puts[0][1].endswith("/raw.wav")


@pytest.mark.asyncio
async def test_s3_recorder_key_layout_is_date_sharded():
    """Key: `<yyyy>/<mm>/<dd>/<call_sid>/{raw,clean}.wav`. Matches the
    CSV projector's convention."""
    uploader = InMemoryS3Uploader()
    r = S3DualRecorder(bucket="fg-rec", uploader=uploader)
    r.push_raw_ulaw(b"\xff" * 160)
    await r.finalise("CA_shard_test")

    key = uploader.puts[0][1]
    parts = key.split("/")
    # yyyy / mm / dd / call_sid / raw.wav
    assert len(parts) == 5
    yyyy, mm, dd, sid, name = parts
    assert len(yyyy) == 4 and yyyy.isdigit()
    assert len(mm) == 2 and mm.isdigit() and 1 <= int(mm) <= 12
    assert len(dd) == 2 and dd.isdigit() and 1 <= int(dd) <= 31
    assert sid == "CA_shard_test"
    assert name == "raw.wav"


# ─── Idempotency ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_finalise_is_idempotent():
    """Twilio's stop event can fire twice on some edge cases (WS
    disconnect + explicit stop). A double-finalise must not double-
    upload the same call."""
    uploader = InMemoryS3Uploader()
    r = S3DualRecorder(bucket="fg-rec", uploader=uploader)
    r.push_raw_ulaw(b"\xff" * 160)
    await r.finalise("CA_idem")
    await r.finalise("CA_idem")
    assert len(uploader.puts) == 1


@pytest.mark.asyncio
async def test_push_after_finalise_is_noop():
    uploader = InMemoryS3Uploader()
    r = S3DualRecorder(bucket="fg-rec", uploader=uploader)
    r.push_raw_ulaw(b"\xff" * 160)
    await r.finalise("CA_late")
    # These pushes should not affect anything — the recorder is done.
    r.push_raw_ulaw(b"\xff" * 160)
    r.push_clean_pcm16(b"\x00\x01" * 160)
    await r.finalise("CA_late")  # second finalise still noop
    assert len(uploader.puts) == 1


# ─── Buffer cap ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_buffer_cap_enforced():
    """Pushing more than the max buffer size must not grow the buffer
    unbounded. The recorder drops overflow silently after one warning
    per stream. We assert on the recorded byte count (pre-widening)
    so the 2x μ-law → PCM16 widening isn't double-counted."""
    uploader = InMemoryS3Uploader()
    r = S3DualRecorder(bucket="fg-rec", uploader=uploader)
    # 8 MB cap + 1 MB overflow.
    chunk = b"\xff" * 1024 * 1024
    for _ in range(9):
        r.push_raw_ulaw(chunk)

    # Direct assertion on the internal buffer: cap enforced.
    assert len(r._raw_buf) <= 8 * 1024 * 1024
    assert r._raw_truncated is True

    await r.finalise("CA_cap")
    assert len(uploader.puts) == 1


# ─── Upload failure containment ──────────────────────────────────────


@dataclass
class _FailingUploader:
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def put_object(self, *, bucket: str, key: str, body: bytes, content_type: str) -> None:
        self.calls.append((bucket, key))
        raise RuntimeError("s3 gone")


@pytest.mark.asyncio
async def test_upload_failure_never_raises():
    """A recording-side S3 failure MUST NOT surface — the audio path
    already ran, the caller heard success. Ops sees the log; nothing
    else changes."""
    r = S3DualRecorder(bucket="fg-rec", uploader=_FailingUploader())
    r.push_raw_ulaw(b"\xff" * 160)
    r.push_clean_pcm16(b"\x00\x01" * 160)
    await r.finalise("CA_fail")  # no raise — the point


# ─── WAV wrapper interop ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wav_wrapper_produces_readable_pcm16_wav():
    """The clean.wav upload must be a WAV the stdlib wave module can
    parse without complaint — that's the sanity contract for Audacity
    / any downstream tool."""
    uploader = InMemoryS3Uploader()
    r = S3DualRecorder(bucket="fg-rec", uploader=uploader)
    # 1 second of PCM16 zeros.
    r.push_clean_pcm16(b"\x00\x00" * 8000)
    await r.finalise("CA_wav")
    key = "clean.wav"
    body = next(put[2] for put in uploader.puts if put[1].endswith(key))

    with wave.open(io.BytesIO(body), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 8000
        assert wf.getnframes() == 8000  # 1 second


@pytest.mark.asyncio
async def test_wav_wrapper_ulaw_widens_to_pcm16():
    """The raw.wav upload widens μ-law to PCM16 for universal
    playability — verify the WAV is readable + the sample rate is
    preserved."""
    uploader = InMemoryS3Uploader()
    r = S3DualRecorder(bucket="fg-rec", uploader=uploader)
    # 0.5 seconds of μ-law silence (0x7F).
    r.push_raw_ulaw(b"\x7f" * 4000)
    await r.finalise("CA_ulaw")
    key = "raw.wav"
    body = next(put[2] for put in uploader.puts if put[1].endswith(key))

    with wave.open(io.BytesIO(body), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 8000


# ─── Factory ─────────────────────────────────────────────────────────


def test_build_dual_recorder_disabled_returns_noop():
    r = build_dual_recorder(enabled=False, bucket="ignored", uploader=None)
    assert isinstance(r, NoOpDualRecorder)


def test_build_dual_recorder_enabled_requires_uploader():
    with pytest.raises(ValueError, match="requires an uploader"):
        build_dual_recorder(enabled=True, bucket="fg-rec", uploader=None)


def test_build_dual_recorder_enabled_returns_s3_impl():
    r = build_dual_recorder(enabled=True, bucket="fg-rec", uploader=InMemoryS3Uploader())
    assert isinstance(r, S3DualRecorder)
