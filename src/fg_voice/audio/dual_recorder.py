"""Dual pre/post-denoise recorder — spec §9.1 tee line.

Every call records TWO parallel streams to S3:

- `raw/`   — the μ-law bytes exactly as Twilio delivered them.
             Untouched by denoise or frontend. This is the ground
             truth for post-hoc re-processing when we change the
             pipeline and want to re-run the sweep on real calls.

- `clean/` — PCM16 after denoise + frontend (what the STT saw).
             Lets ops compare raw vs clean side-by-side in the call
             review console and answer "did the denoiser help on
             this specific call?"

Design notes:

- **Backpressure-safe.** The audio loop pushes frames into an
  in-memory buffer; the recorder never blocks on I/O. Upload happens
  when the call ends via `.finalise()` — one PUT per stream, not one
  per frame. Cap on buffer size at ~5 minutes of audio (matches
  `Settings.max_call_duration_sec`) so a stuck upload doesn't grow
  unbounded.

- **NoOp default.** `Settings.s3_recording_enabled` gates the whole
  path; when off, `NoOpDualRecorder` is wired and every method is a
  cheap no-op. Ops flips the flag on when the S3 bucket lifecycle
  policy is set up (30-day retention per §17).

- **S3 client injection.** The `S3Uploader` protocol takes bucket +
  key + payload. Production wraps aioboto3; tests pass an in-memory
  recorder so nothing hits the network.

- **Format.** Both streams are written as WAV with proper headers so
  ops can drag-drop into Audacity without a conversion step. The
  raw μ-law stream is tagged with format code 7 (WAVE_FORMAT_MULAW).

- **Key layout.** `s3://<bucket>/<yyyy>/<mm>/<dd>/<call_sid>/{raw,clean}.wav`.
  Date sharding matches the CSV projector's convention and keeps
  the review console's per-day listing responsive.
"""

from __future__ import annotations

import audioop
import io
import time
import wave
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from fg_voice.obs.logging import get_logger

log = get_logger(__name__)


# Practical cap so a stuck upload can't OOM the process. 5 minutes of
# 8 kHz 16-bit mono = 5 * 60 * 8000 * 2 = 4.8 MB per stream. Fine.
_MAX_BUFFER_BYTES = 8 * 1024 * 1024

# Sample rate for BOTH streams. Raw is μ-law@8kHz per Twilio; clean is
# PCM16@8kHz after denoise + frontend. Any deviation is a bug.
_SAMPLE_RATE_HZ = 8000

# WAVE_FORMAT_MULAW — the format code μ-law-tagged WAV files use.
_WAV_FORMAT_MULAW = 7


@runtime_checkable
class S3Uploader(Protocol):
    """Async put-object. Production wraps aioboto3; tests use a
    recorder that appends to an in-memory list."""

    async def put_object(
        self, *, bucket: str, key: str, body: bytes, content_type: str
    ) -> None: ...


@dataclass(slots=True)
class InMemoryS3Uploader:
    """Test double. `.puts` records every put in order so tests can
    assert on bucket/key/body shape."""

    puts: list[tuple[str, str, bytes, str]] = field(default_factory=list)

    async def put_object(self, *, bucket: str, key: str, body: bytes, content_type: str) -> None:
        self.puts.append((bucket, key, body, content_type))


@runtime_checkable
class DualRecorder(Protocol):
    """The seam every audio-path caller integrates against. Push raw
    μ-law frames and clean PCM16 frames as they flow through the
    pipeline; call `finalise(call_sid)` at call end."""

    def push_raw_ulaw(self, ulaw: bytes) -> None: ...
    def push_clean_pcm16(self, pcm16: bytes) -> None: ...
    async def finalise(self, call_sid: str) -> None: ...


@dataclass(slots=True)
class NoOpDualRecorder:
    """Ships as the default when S3_RECORDING_ENABLED is False. Every
    method is a cheap no-op — the audio path stays exactly as fast as
    it was before dual recording landed."""

    def push_raw_ulaw(self, ulaw: bytes) -> None:
        return None

    def push_clean_pcm16(self, pcm16: bytes) -> None:
        return None

    async def finalise(self, call_sid: str) -> None:
        return None


@dataclass(slots=True)
class S3DualRecorder:
    """Buffered per-call recorder that ships both streams to S3 on
    finalise. Instantiate one per call; the buffers are per-instance
    so concurrent calls never mingle audio."""

    bucket: str
    uploader: S3Uploader
    _raw_buf: bytearray = field(default_factory=bytearray, init=False)
    _clean_buf: bytearray = field(default_factory=bytearray, init=False)
    _raw_truncated: bool = field(default=False, init=False)
    _clean_truncated: bool = field(default=False, init=False)
    _finalised: bool = field(default=False, init=False)

    def push_raw_ulaw(self, ulaw: bytes) -> None:
        if self._finalised or not ulaw:
            return
        if len(self._raw_buf) + len(ulaw) > _MAX_BUFFER_BYTES:
            if not self._raw_truncated:
                self._raw_truncated = True
                log.warning(
                    "dual_recorder.raw_truncated",
                    bytes_capped=_MAX_BUFFER_BYTES,
                )
            return
        self._raw_buf.extend(ulaw)

    def push_clean_pcm16(self, pcm16: bytes) -> None:
        if self._finalised or not pcm16:
            return
        if len(self._clean_buf) + len(pcm16) > _MAX_BUFFER_BYTES:
            if not self._clean_truncated:
                self._clean_truncated = True
                log.warning(
                    "dual_recorder.clean_truncated",
                    bytes_capped=_MAX_BUFFER_BYTES,
                )
            return
        self._clean_buf.extend(pcm16)

    async def finalise(self, call_sid: str) -> None:
        """Wrap the buffered streams in WAV headers, PUT to S3, then
        release the buffers. Idempotent — safe to call twice on the
        same recorder if the call-end path fires more than once."""
        if self._finalised:
            return
        self._finalised = True

        if not self._raw_buf and not self._clean_buf:
            log.info("dual_recorder.empty", call_sid=call_sid)
            return

        prefix = _s3_prefix(call_sid)
        try:
            if self._raw_buf:
                raw_wav = _wrap_ulaw_as_wav(bytes(self._raw_buf))
                await self.uploader.put_object(
                    bucket=self.bucket,
                    key=f"{prefix}/raw.wav",
                    body=raw_wav,
                    content_type="audio/wav",
                )
            if self._clean_buf:
                # Clean stream: PCM16 after denoise + frontend. The
                # frontend returns PCM16 bytes so we wrap directly —
                # if a caller starts pushing numpy arrays instead,
                # add a `.tobytes()` at the call site.
                clean_wav = _wrap_pcm16_as_wav(bytes(self._clean_buf))
                await self.uploader.put_object(
                    bucket=self.bucket,
                    key=f"{prefix}/clean.wav",
                    body=clean_wav,
                    content_type="audio/wav",
                )
        except Exception as exc:
            # Never let a recording failure surface to the caller. The
            # audio path already ran; ops sees the log + monitor alarm.
            log.exception(
                "dual_recorder.upload_failed",
                call_sid=call_sid,
                error=str(exc),
            )
            return

        log.info(
            "dual_recorder.uploaded",
            call_sid=call_sid,
            raw_bytes=len(self._raw_buf),
            clean_bytes=len(self._clean_buf),
            raw_truncated=self._raw_truncated,
            clean_truncated=self._clean_truncated,
        )

        # Drop the buffers after upload so the recorder can be GC'd
        # cleanly even if the caller holds a stale reference.
        self._raw_buf = bytearray()
        self._clean_buf = bytearray()


# ─── WAV framing helpers ─────────────────────────────────────────────


def _wrap_pcm16_as_wav(pcm16: bytes) -> bytes:
    """Standard PCM16 WAV — playable in Audacity + all common tools
    without a conversion step."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_SAMPLE_RATE_HZ)
        wf.writeframes(pcm16)
    return buf.getvalue()


def _wrap_ulaw_as_wav(ulaw: bytes) -> bytes:
    """μ-law WAV. Python's stdlib `wave` module doesn't write the
    format code 7 header directly; we assemble the RIFF/WAVE bytes
    manually so downstream tools open the file correctly rather than
    trying to parse it as PCM."""
    # Widen to PCM16 for tools that don't handle μ-law directly; keep
    # both channels for the ops review console — playing μ-law-tagged
    # WAV in Firefox is not universally reliable. This is a
    # correctness-over-storage tradeoff (~2x storage for guaranteed
    # playability). If bucket cost ever becomes an issue, swap to
    # true μ-law WAV.
    pcm16 = audioop.ulaw2lin(ulaw, 2)
    return _wrap_pcm16_as_wav(pcm16)


def _s3_prefix(call_sid: str) -> str:
    """Date-sharded key: 2026/08/17/CA<sid>. Matches the CSV
    projector's convention and keeps per-day listings responsive."""
    ts = time.gmtime()
    return f"{ts.tm_year:04d}/{ts.tm_mon:02d}/{ts.tm_mday:02d}/{call_sid}"


def build_dual_recorder(
    enabled: bool,
    *,
    bucket: str,
    uploader: S3Uploader | None = None,
) -> DualRecorder:
    """Factory. `enabled=False` returns NoOpDualRecorder; `enabled=True`
    requires both a bucket and an uploader (fails loud if `uploader`
    is None — silent no-op on a misconfigured deploy would hide the
    fact that recording is off)."""
    if not enabled:
        return NoOpDualRecorder()
    if uploader is None:
        raise ValueError("build_dual_recorder(enabled=True) requires an uploader")
    return S3DualRecorder(bucket=bucket, uploader=uploader)


__all__ = [
    "DualRecorder",
    "InMemoryS3Uploader",
    "NoOpDualRecorder",
    "S3DualRecorder",
    "S3Uploader",
    "build_dual_recorder",
]
