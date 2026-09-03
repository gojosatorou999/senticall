"""SNR-controlled noise mixing at 8 kHz PCM16 — spec §9.5.

Given a clean speech signal and a noise signal, produce a mixed
signal at a target signal-to-noise ratio in dB. Kept pure-numpy so
the harness has no external audio-lib dependencies.

Definitions:
- SNR (dB) = 10 * log10(power_signal / power_noise)
- We hold the clean-signal RMS fixed and scale the noise to hit the
  target SNR. The alternative (scaling the signal) shifts the
  denoiser's absolute-level operating point, which is unfair to the
  algorithm's noise-floor estimate. Fixed-signal-scale is the
  standard convention in speech-enhancement benchmarks.

Noise looping: if `noise` is shorter than `signal`, tile it. If it's
longer, take a deterministic slice starting at `offset_sec` so the
sweep is reproducible across runs.

Peak clipping: after mixing, values can exceed [-1, 1] briefly at
high signal levels + high noise floors. The mixer soft-clips to
[-0.99, 0.99] and reports the clipping ratio so downstream metrics
can flag runs where the noise is unrealistically hot."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# All internal math is float32 in [-1, 1]. Callers pass and receive
# int16 PCM at 8 kHz for compatibility with the Twilio-side pipeline.

_INT16_SCALE: float = 32768.0


@dataclass(frozen=True, slots=True)
class MixResult:
    """Output of one mixing operation. `mixed` is int16 PCM at the
    same sample rate as the inputs. `achieved_snr_db` is what actually
    landed after level scaling; `clip_ratio` is the fraction of
    samples that hit the soft-clip ceiling."""

    mixed: bytes  # PCM16, same length as the input signal
    achieved_snr_db: float
    clip_ratio: float


def _pcm16_to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / _INT16_SCALE


def _float_to_pcm16(signal: np.ndarray) -> bytes:
    clipped = np.clip(signal * _INT16_SCALE, -_INT16_SCALE, _INT16_SCALE - 1)
    return clipped.astype(np.int16).tobytes()


def _rms(signal: np.ndarray) -> float:
    """Root-mean-square. Silence returns 0.0 without warnings."""
    if signal.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(signal.astype(np.float64) ** 2)))


def _tile_or_slice(noise: np.ndarray, target_len: int, *, offset: int = 0) -> np.ndarray:
    """Return a noise buffer of exactly `target_len` samples. Tiles a
    short buffer; deterministic-offset-slices a long one."""
    if noise.size == 0:
        return np.zeros(target_len, dtype=np.float32)
    if noise.size >= target_len:
        # Wrap-around slice if offset+target overshoots — avoids
        # exceptions on edge cases where the offset was chosen for
        # a longer buffer than the one at hand.
        start = offset % noise.size
        if start + target_len <= noise.size:
            return noise[start : start + target_len]
        first = noise[start:]
        second = noise[: target_len - first.size]
        return np.concatenate([first, second])
    # Short — tile repeatedly.
    repeats = (target_len // noise.size) + 1
    tiled = np.tile(noise, repeats)
    return tiled[:target_len]


def mix_at_snr(
    signal_pcm16: bytes,
    noise_pcm16: bytes,
    *,
    snr_db: float,
    noise_offset_sec: float = 0.0,
    sample_rate: int = 8000,
) -> MixResult:
    """Mix `signal` and `noise` to a target SNR in dB. Returns a new
    PCM16 buffer of the same length as `signal`.

    Silence handling:
    - Zero-signal input → returns zero output; achieved_snr is `-inf`
      (or reported as -120.0 to keep JSON-serialisable).
    - Zero-noise input → returns the signal unchanged; achieved_snr
      is `+inf` (reported as +120.0).
    """
    signal = _pcm16_to_float(signal_pcm16)
    noise = _pcm16_to_float(noise_pcm16)

    sig_rms = _rms(signal)
    if sig_rms <= 1e-9:
        return MixResult(mixed=signal_pcm16, achieved_snr_db=-120.0, clip_ratio=0.0)

    noise_slice = _tile_or_slice(
        noise, target_len=signal.size, offset=int(noise_offset_sec * sample_rate)
    )
    noise_rms = _rms(noise_slice)
    if noise_rms <= 1e-9:
        # No noise energy — the signal is already at infinite SNR.
        return MixResult(mixed=signal_pcm16, achieved_snr_db=120.0, clip_ratio=0.0)

    # Solve for the scale that puts the noise at the requested SNR:
    #     sig_rms / (scale * noise_rms) = 10^(snr_db/20)
    # =>  scale = sig_rms / (noise_rms * 10^(snr_db/20))
    target_noise_scale = sig_rms / (noise_rms * (10 ** (snr_db / 20.0)))
    scaled_noise = noise_slice * target_noise_scale

    mixed = signal + scaled_noise

    # Soft clip and report ratio.
    ceiling = 0.99
    over_mask = np.abs(mixed) > ceiling
    clip_ratio = float(np.mean(over_mask))
    mixed = np.clip(mixed, -ceiling, ceiling)

    # Recompute achieved SNR from the actual mixed vs. actual noise.
    achieved_noise_rms = _rms(scaled_noise)
    if achieved_noise_rms <= 1e-9:
        achieved_snr = 120.0
    else:
        achieved_snr = 20.0 * float(np.log10(sig_rms / achieved_noise_rms))

    return MixResult(
        mixed=_float_to_pcm16(mixed),
        achieved_snr_db=achieved_snr,
        clip_ratio=clip_ratio,
    )


def synthetic_tone_pcm16(
    freq_hz: int = 440,
    duration_ms: int = 1000,
    sample_rate: int = 8000,
    amplitude: float = 0.3,
) -> bytes:
    """Deterministic sine tone at `freq_hz` — used as a stand-in for
    speech in synthetic-corpus smoke tests. Real corpora replace this
    with actual utterance recordings."""
    n = int(sample_rate * duration_ms / 1000)
    t = np.arange(n) / sample_rate
    signal = amplitude * np.sin(2 * np.pi * freq_hz * t)
    return _float_to_pcm16(signal)


def synthetic_white_noise_pcm16(
    duration_ms: int = 1000,
    sample_rate: int = 8000,
    amplitude: float = 0.2,
    seed: int = 0,
) -> bytes:
    """Deterministic white noise — synthetic-corpus smoke tests only.
    Real noise samples land in `data/eval/noise/samples/`."""
    n = int(sample_rate * duration_ms / 1000)
    rng = np.random.default_rng(seed)
    noise = rng.uniform(-amplitude, amplitude, size=n).astype(np.float32)
    return _float_to_pcm16(noise)


__all__ = [
    "MixResult",
    "mix_at_snr",
    "synthetic_tone_pcm16",
    "synthetic_white_noise_pcm16",
]
