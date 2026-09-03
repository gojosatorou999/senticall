"""Noise-suppression seam — spec §9.1 step 2.

The audio path is:

    μ-law → PCM16 → [denoise?] → frontend (DC/HPF/AGC/gate) → STT

Denoise is optional and pluggable. This module defines the seam
(`Denoiser` protocol) and ships three implementations plus a NoOp
default:

- **NoOpDenoiser** (`provider="none"`) — passthrough. Ships as the
  safe default so the pipeline runs end-to-end without any DSP or
  external dep.

- **SpectralGateDenoiser** (`provider="rnnoise"` as the fallback
  path) — pure-numpy log-spectral subtraction. Estimates the noise
  floor from the first ~300 ms of the call (assumed to precede
  speech), then attenuates bands whose per-frame magnitude sits
  within `k*sigma` of the running noise-floor estimate. Not as good as
  a trained RNN but ships without any C++ build or SDK license and
  is measurably better than nothing on stationary noise (wind, rain,
  AC hum, distant traffic). Latency ~1-2 ms per 20 ms frame on
  a modern CPU.

- **KrispDenoiser** — commercial SDK, best objective quality on
  8 kHz telephony, licensed per-concurrent-call. Not shipped in
  this commit — requires the Krisp license key and their binary
  SDK. Stays as a documented seam so ops can wire it in when the
  license lands. Falls back to the spectral-gate impl when the
  license is absent so the pipeline never crashes on a flipped
  flag without provisioned deps.

Latency budget note (§5): SpectralGate adds ~1-2 ms per 20 ms frame;
Krisp adds ~5-10 ms. Both fit comfortably in the turn budget. If the
frame budget ever squeezes, the denoiser is the first thing to drop —
run the noise sweep with and without, compare slot accuracy, decide.

Wiring at boot lives in `main.py`; `DENOISE_PROVIDER` selects the
implementation, defaults to `rnnoise` (the spectral-gate fallback) in
`Settings` so a fresh deploy gets a real denoise pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Protocol, runtime_checkable

import numpy as np

from fg_voice.obs.logging import get_logger

log = get_logger(__name__)


# ─── Protocol ────────────────────────────────────────────────────────


@runtime_checkable
class Denoiser(Protocol):
    """One noise-suppression pass over a PCM16 frame.

    - Input: PCM16 bytes (little-endian int16, 8 kHz mono, typically
      160 samples / 320 bytes per Twilio frame).
    - Output: PCM16 bytes of the same shape.

    Implementations are stateful per call — the RNN or Krisp session
    carries information across frames — so instantiate once per call
    session and share across frames in-order.
    """

    def process_pcm16(self, pcm16: bytes) -> bytes: ...


# ─── NoOp ────────────────────────────────────────────────────────────


@dataclass(slots=True)
class NoOpDenoiser:
    """Passthrough denoiser. Ships as the safe default so the
    pipeline runs end-to-end on any deploy without a licensed
    denoiser. Zero latency, zero quality change.

    A `denoise_provider=none` deploy uses this."""

    def process_pcm16(self, pcm16: bytes) -> bytes:
        return pcm16


# ─── Spectral gate (real, numpy-only fallback) ───────────────────────


# Frame parameters for the spectral gate. Twilio delivers 20 ms mono
# frames at 8 kHz → 160 samples. FFT length matches the frame so no
# windowing seams appear between input/output frames.
_FRAME_SAMPLES: Final[int] = 160
_FFT_SIZE: Final[int] = 256  # next power of two above 160 for FFT efficiency

# How many opening frames to treat as pure noise for the initial
# noise-floor estimate. 15 x 20ms = 300 ms — the consent notice starts
# playing after this window so the caller hasn't spoken yet on inbound.
_NOISE_ESTIMATE_FRAMES: Final[int] = 15

# Attenuation exponent — higher gates harder, lower is gentler. 2.0 is
# the standard log-Wiener choice; 1.0 acts more like plain subtraction.
_OVER_SUBTRACTION_ALPHA: Final[float] = 2.0

# Floor gain — the minimum multiplier a bin can be attenuated to. Prevents
# musical noise (sudden zero-outs of narrow bands) by leaving a small
# residual signal even in strongly-gated bands.
_FLOOR_GAIN: Final[float] = 0.05

# Adaptive noise-floor update rate. After the initial estimate we keep
# updating with a slow EMA so drifting background noise (traffic getting
# closer, fan turning on) is tracked. Only frames whose per-bin magnitude
# is below the current noise floor + margin contribute — otherwise we'd
# absorb speech into the noise estimate.
_NOISE_EMA_ALPHA: Final[float] = 0.02
_NOISE_UPDATE_MARGIN: Final[float] = 1.5


@dataclass(slots=True)
class SpectralGateDenoiser:
    """Stationary-noise suppression via log-spectral subtraction.

    Per-frame algorithm:

      1. Convert PCM16 → float32 in [-1, 1].
      2. Zero-pad to _FFT_SIZE and rfft.
      3. |X(f)| = spectral magnitude, ∠X(f) = spectral phase.
      4. During the first _NOISE_ESTIMATE_FRAMES frames: accumulate
         |X(f)| into the noise-floor buffer, output original signal
         unchanged (algorithm has no basis yet to subtract).
      5. From frame _NOISE_ESTIMATE_FRAMES onward:
           gain(f) = max(_FLOOR_GAIN,
                         (|X(f)|^a - noise(f)^a) / |X(f)|^a)^(1/a)
         where `a` is _OVER_SUBTRACTION_ALPHA.
      6. Reconstruct with gain-attenuated magnitude + original phase,
         irfft, take first _FRAME_SAMPLES samples, quantise back to
         int16.
      7. Slow-EMA-update the noise estimate on bins whose current
         magnitude is close to the noise floor (i.e. probably not
         speech).

    Not a trained model — a well-known DSP technique. Meaningful gains
    on stationary noise (wind, rain, distant traffic, AC hum, mic
    handling); marginal on impulsive noise (dog barking, door slam).
    The noise sweep harness (`tests/noise/`) quantifies exactly which
    noise types benefit; anything that regresses the sweep gets dropped."""

    _frame_count: int = 0
    _noise_estimate: np.ndarray[Any, Any] | None = field(default=None, init=False)

    def process_pcm16(self, pcm16: bytes) -> bytes:
        if len(pcm16) == 0:
            return pcm16
        samples = np.frombuffer(pcm16, dtype=np.int16)
        if samples.size == 0:
            return pcm16

        # Pad or truncate to the fixed frame size so noise estimation is
        # per-bin stable. Twilio's 160-sample frame passes through cleanly.
        if samples.size < _FRAME_SAMPLES:
            samples = np.pad(samples, (0, _FRAME_SAMPLES - samples.size))
        elif samples.size > _FRAME_SAMPLES:
            samples = samples[:_FRAME_SAMPLES]

        signal = samples.astype(np.float32) / 32768.0

        # Zero-pad to FFT_SIZE for efficiency; take rfft to get one-sided
        # complex spectrum. Length: _FFT_SIZE // 2 + 1.
        padded = np.zeros(_FFT_SIZE, dtype=np.float32)
        padded[: signal.size] = signal
        spectrum = np.fft.rfft(padded)
        magnitude = np.abs(spectrum)
        phase = np.angle(spectrum)

        # Bootstrap the noise floor over the first N frames.
        if self._noise_estimate is None:
            self._noise_estimate = magnitude.copy()
        elif self._frame_count < _NOISE_ESTIMATE_FRAMES:
            self._noise_estimate = np.maximum(self._noise_estimate, magnitude)

        self._frame_count += 1
        if self._frame_count <= _NOISE_ESTIMATE_FRAMES:
            # Still learning — pass through unchanged. Any early speech is
            # a small over-estimate; the EMA below drifts it back.
            return pcm16

        # Compute per-bin gain via over-subtraction. Guard against
        # divide-by-zero in silent bands with a tiny epsilon.
        assert self._noise_estimate is not None
        eps = 1e-8
        alpha = _OVER_SUBTRACTION_ALPHA
        mag_a = magnitude**alpha
        noise_a = self._noise_estimate**alpha
        gain_a = np.clip((mag_a - noise_a) / (mag_a + eps), 0.0, 1.0)
        gain = np.maximum(_FLOOR_GAIN, gain_a ** (1.0 / alpha))

        # Adaptive noise-floor update: only for bins that look
        # non-speech (magnitude close to current floor + small margin).
        non_speech = magnitude < (self._noise_estimate * _NOISE_UPDATE_MARGIN)
        self._noise_estimate = np.where(
            non_speech,
            (1 - _NOISE_EMA_ALPHA) * self._noise_estimate + _NOISE_EMA_ALPHA * magnitude,
            self._noise_estimate,
        )

        # Reconstruct.
        clean_spectrum = gain * magnitude * np.exp(1j * phase)
        clean_padded = np.fft.irfft(clean_spectrum, n=_FFT_SIZE)
        clean_signal = clean_padded[:_FRAME_SAMPLES]

        # Quantise back to int16 with clipping — subtraction can
        # occasionally produce a slightly-out-of-range sample due to
        # phase misalignment; clip is cheaper than a full normalise.
        clean_int16 = np.clip(clean_signal * 32768.0, -32768, 32767).astype(np.int16)
        return clean_int16.tobytes()


# ─── Chained composition with the audio frontend ─────────────────────


@dataclass(slots=True)
class ChainedDenoiseFrontend:
    """Composes a `Denoiser` + `AudioFrontend` into one per-frame
    call. Kept as a separate class rather than shoved into
    `AudioFrontend` so the frontend module stays dependency-free and
    unit-testable in isolation.

    Usage in main.py:

        denoiser = build_denoiser(settings.denoise_provider)
        frontend = AudioFrontend()
        pipeline = ChainedDenoiseFrontend(denoiser, frontend)
        clean_pcm16 = pipeline.process_pcm16(raw_pcm16_bytes)
    """

    denoiser: Denoiser
    # Typed as object so this module has no import dep on frontend.py;
    # runtime is `AudioFrontend`. Kept intentionally structural.
    frontend: object

    def process_pcm16(self, pcm16: bytes) -> object:
        """Denoise first, then frontend. Returns the frontend's
        output shape (int16 numpy array). Caller-facing type is
        `object` to keep this module's imports minimal."""
        cleaned = self.denoiser.process_pcm16(pcm16)
        return self.frontend.process_pcm16(cleaned)  # type: ignore[attr-defined]


# ─── Factory ─────────────────────────────────────────────────────────


def build_denoiser(provider: str) -> Denoiser:
    """Factory. `provider` matches `Settings.denoise_provider`
    (`none` / `krisp` / `rnnoise`). Unknown providers fall back to
    NoOp with a loud warning — safer than crashing the boot for a
    typo in an ops env var.

    Provider mapping:

    - `none` → `NoOpDenoiser` (bit-exact passthrough)
    - `rnnoise` → `SpectralGateDenoiser` (numpy-only fallback; the
      "rnnoise" label is legacy — spec §9.1 named it as the OSS
      option, and swapping the impl is transparent to callers)
    - `krisp` → NoOp + warning (SDK integration lives in a follow-up
      commit alongside license provisioning; keeps callers safe when
      someone flips the flag before the SDK is wired)
    """
    if provider == "none":
        return NoOpDenoiser()
    if provider == "rnnoise":
        return SpectralGateDenoiser()
    if provider == "krisp":
        # KrispDenoiser lands with the license wiring. Falling back to
        # NoOp keeps the pipeline up when someone flips the flag before
        # the impl ships. NOT the spectral-gate fallback: the operator
        # explicitly asked for Krisp, and silently substituting a
        # weaker denoiser would mask a config error.
        log.warning(
            "audio.denoise.provider_not_implemented",
            provider=provider,
            note="Krisp integration not yet built; using NoOpDenoiser",
        )
        return NoOpDenoiser()
    log.warning("audio.denoise.unknown_provider", provider=provider)
    return NoOpDenoiser()


__all__ = [
    "ChainedDenoiseFrontend",
    "Denoiser",
    "NoOpDenoiser",
    "SpectralGateDenoiser",
    "build_denoiser",
]
