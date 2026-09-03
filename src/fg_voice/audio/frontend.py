"""Audio front-end — the DSP layer that sits between the μ-law codec
and the STT (Deepgram) input.

Twilio Media Streams hand us 20 ms μ-law frames at 8 kHz mono. Before
the STT ever sees them we run four cleanup passes, in this order:

    μ-law → PCM16 → [DC removal → HPF → AGC → noise gate] → STT

Each pass is a small, testable function. Ordering matters:

1. **DC removal** — removes a constant DC bias (some phone lines /
   codecs introduce one). Must run first so the HPF's transient
   response isn't wasted on absorbing DC.
2. **High-pass filter** — first-order biquad, cutoff ~80 Hz. Kills
   telephony rumble (mains hum at 50/60 Hz, HVAC low-frequency
   modulation) that the ASR has zero use for and that only makes
   AGC pump harder.
3. **AGC** — target -20 dBFS envelope with a fast attack + slow
   release. Callers speak at wildly different levels; feeding the
   ASR a normalised signal materially improves noise robustness.
4. **Noise gate** — hard zero on frames whose RMS is below the gate
   threshold (default -50 dBFS). Prevents the AGC from amplifying
   pure noise during silences into detectable "speech".

All state (HPF filter memory, AGC envelope tracker) lives on the
`AudioFrontend` instance — one instance per call. The individual
step functions are pure (state passed in + returned out) so they're
trivially testable in isolation.

Numerics: we work in float32 in [-1.0, 1.0] internally. The int16 →
float32 → int16 round-trip is stable enough for a downstream 8-bit
μ-law encoding — we're well under the ASR's noise floor.

Deliberately NOT here:
- Denoise (Krisp / rnnoise) — that's a separate module because it
  has an external dependency and a big latency cost that ops needs
  to be able to gate independently.
- Resampling — Twilio delivers 8 kHz, we hand 8 kHz to Deepgram;
  no rate change is needed on this leg. Resample once at the P3+
  denoise stage if needed.
- VAD — Deepgram Flux does turn-taking (§9.2); a client-side VAD
  would double-decide.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Final, cast

import numpy as np
import numpy.typing as npt

from fg_voice.audio.codec import TWILIO_SAMPLE_RATE_HZ

# Type aliases — keeps signatures readable and gives mypy the
# generic parameters it wants for `np.ndarray`. PEP 695 `type`
# statement (Python 3.12+; the pyproject already pins 3.12).
type Float32Array = npt.NDArray[np.float32]
type Int16Array = npt.NDArray[np.int16]

# Defaults tuned for the 8 kHz mono telephony pipeline.
DEFAULT_HPF_CUTOFF_HZ: Final[float] = 80.0
DEFAULT_AGC_TARGET_DBFS: Final[float] = -20.0
DEFAULT_AGC_ATTACK_MS: Final[float] = 10.0
DEFAULT_AGC_RELEASE_MS: Final[float] = 250.0
DEFAULT_AGC_MAX_GAIN_DB: Final[float] = 30.0  # cap so the gate doesn't fight the AGC
DEFAULT_GATE_THRESHOLD_DBFS: Final[float] = -50.0
# Full-scale reference for the dBFS conversions. `1.0` in float32.
_DBFS_REF: Final[float] = 1.0
_EPS: Final[float] = 1e-9  # log10(0) guard


# ─── Conversions ────────────────────────────────────────────────────


def pcm16_to_float32(pcm: bytes | npt.NDArray[np.int16]) -> Float32Array:
    """Interpret bytes as little-endian int16 PCM or accept a
    ready-made int16 array; return float32 samples scaled to
    [-1.0, 1.0). The `32768` divisor matches PCM16's negative-side
    range so `-32768` maps to `-1.0` exactly."""
    if isinstance(pcm, bytes | bytearray | memoryview):
        arr = np.frombuffer(pcm, dtype=np.int16)
    else:
        arr = np.asarray(pcm, dtype=np.int16)
    return arr.astype(np.float32) / 32768.0


def float32_to_pcm16(samples: Float32Array) -> Int16Array:
    """Clip to [-1, 1) and quantise back to int16. Returns a numpy
    int16 array (byte serialisation is the caller's concern)."""
    clipped = np.clip(samples, -1.0, 32767.0 / 32768.0)
    return cast(Int16Array, (clipped * 32768.0).astype(np.int16))


def rms_dbfs(samples: Float32Array) -> float:
    """RMS level of a float32 frame in dBFS. Returns -inf-like value
    (large negative) for a fully-silent frame so downstream `if
    rms < threshold` comparisons stay well-behaved."""
    if samples.size == 0:
        return -float("inf")
    rms = math.sqrt(float(np.mean(np.square(samples, dtype=np.float64))))
    return 20.0 * math.log10(max(rms, _EPS) / _DBFS_REF)


# ─── DC removal ─────────────────────────────────────────────────────


def remove_dc(samples: Float32Array) -> Float32Array:
    """Subtract per-frame mean. Per-frame (not cumulative) is fine
    for 20 ms frames — the DC drift within one frame is negligible;
    running DC across the whole stream would need a leaky integrator
    which just replicates what the HPF does anyway."""
    if samples.size == 0:
        return samples
    return samples - np.mean(samples, dtype=np.float32)


# ─── High-pass filter (first-order biquad) ──────────────────────────


@dataclass(slots=True)
class HpfState:
    """One-tap biquad memory. `y1` is the previous output sample;
    `x1` the previous input. Per-stream state — reset when the call
    ends."""

    x1: float = 0.0
    y1: float = 0.0


def highpass(
    samples: Float32Array,
    state: HpfState,
    *,
    sample_rate: int = TWILIO_SAMPLE_RATE_HZ,
    cutoff_hz: float = DEFAULT_HPF_CUTOFF_HZ,
) -> Float32Array:
    """First-order high-pass biquad. Approximation:

        y[n] = a * (y[n-1] + x[n] - x[n-1])
        a    = RC / (RC + dt)
        RC   = 1 / (2π * cutoff)

    Mutates `state` in place so consecutive frames stitch cleanly.
    Chosen over a higher-order Butterworth because (a) 8 kHz phone
    audio has no useful content below 80 Hz anyway; (b) first-order
    is 3 multiplies/sample which is essentially free."""
    if samples.size == 0:
        return samples
    dt = 1.0 / sample_rate
    rc = 1.0 / (2.0 * math.pi * cutoff_hz)
    a = rc / (rc + dt)

    y = np.empty_like(samples)
    y1 = state.y1
    x1 = state.x1
    # Loop is 8000 samples/s x 20 ms = 160 iters; pure-Python is
    # fine (numpy vectorisation of a recursive filter needs scipy's
    # lfilter, which adds a dep for a filter we can write inline).
    for i, x in enumerate(samples):
        y_val = a * (y1 + float(x) - x1)
        y[i] = y_val
        y1 = y_val
        x1 = float(x)
    state.y1 = y1
    state.x1 = x1
    return y


# ─── Automatic gain control ─────────────────────────────────────────


@dataclass(slots=True)
class AgcState:
    """AGC envelope + gain state. `envelope` tracks the peak signal
    level; `gain_db` is the current gain applied (before clipping to
    the per-frame max_gain_db)."""

    envelope: float = 0.0
    gain_db: float = 0.0


def agc(
    samples: Float32Array,
    state: AgcState,
    *,
    sample_rate: int = TWILIO_SAMPLE_RATE_HZ,
    target_dbfs: float = DEFAULT_AGC_TARGET_DBFS,
    attack_ms: float = DEFAULT_AGC_ATTACK_MS,
    release_ms: float = DEFAULT_AGC_RELEASE_MS,
    max_gain_db: float = DEFAULT_AGC_MAX_GAIN_DB,
) -> Float32Array:
    """Broadcast-style AGC — fast attack so we don't clip a sudden
    loud transient; slow release so short pauses don't pump the
    background noise up. Applies ONE gain per frame (based on the
    frame's RMS + prior envelope), not per-sample — a per-sample gain
    would distort the waveform on short vowels.

    Mutates `state` so consecutive frames evolve the envelope
    smoothly."""
    if samples.size == 0:
        return samples

    # Frame RMS in linear scale.
    frame_rms = math.sqrt(float(np.mean(np.square(samples, dtype=np.float64))))

    # Envelope tracker (single-pole low-pass on the linear RMS).
    # `attack` when the frame is above the current envelope,
    # `release` when below. Per-frame time constant, not per-sample:
    # the frame IS the time step.
    frame_dur_ms = 1000.0 * samples.size / sample_rate
    if frame_rms > state.envelope:
        alpha = _one_pole_alpha(attack_ms, frame_dur_ms)
    else:
        alpha = _one_pole_alpha(release_ms, frame_dur_ms)
    state.envelope = (1.0 - alpha) * state.envelope + alpha * frame_rms

    # Compute target gain from envelope. Clamp under `max_gain_db`
    # so a near-silent frame isn't amplified into pure noise (the
    # noise gate is the primary defence, this is belt-and-suspenders).
    envelope_dbfs = 20.0 * math.log10(max(state.envelope, _EPS))
    target_gain_db = target_dbfs - envelope_dbfs
    target_gain_db = min(target_gain_db, max_gain_db)
    target_gain_db = max(target_gain_db, -max_gain_db)

    # Smooth the applied gain across frames — jumping straight to
    # target on frame 1 would sound like a compressor knee.
    state.gain_db = 0.5 * state.gain_db + 0.5 * target_gain_db
    gain_linear = 10.0 ** (state.gain_db / 20.0)

    # `cast` keeps mypy happy — numpy's multiply on a Float32Array
    # returns `ndarray[Any, dtype[floating[_32Bit]]]` which the
    # strict return-any check doesn't accept as `Float32Array` sans
    # explicit narrowing.
    return cast(Float32Array, (samples * gain_linear).astype(np.float32))


def _one_pole_alpha(tau_ms: float, dt_ms: float) -> float:
    """`alpha` for a one-pole low-pass with time constant `tau_ms` sampled
    at `dt_ms` interval. Bounded to [0, 1] so an extreme parameter
    ratio doesn't blow up."""
    if tau_ms <= 0.0:
        return 1.0
    return min(1.0, max(0.0, 1.0 - math.exp(-dt_ms / tau_ms)))


# ─── Noise gate ─────────────────────────────────────────────────────


def noise_gate(
    samples: Float32Array,
    *,
    threshold_dbfs: float = DEFAULT_GATE_THRESHOLD_DBFS,
) -> Float32Array:
    """Hard gate — frames whose RMS is below `threshold_dbfs` get
    zeroed. Runs AFTER the AGC because the AGC's amplification of
    silence is exactly what makes gating necessary at all.

    Stateless per frame. A soft-knee smooth-envelope gate would
    reduce the audible chattering-gate artifact on partial-silence
    frames, but 20 ms frames are short enough that the artifact is
    subaudible in practice."""
    if samples.size == 0:
        return samples
    level_dbfs = rms_dbfs(samples)
    if level_dbfs < threshold_dbfs:
        return np.zeros_like(samples)
    return samples


# ─── Composed pipeline ─────────────────────────────────────────────


@dataclass(slots=True)
class FrontendConfig:
    """One place to override any knob. Instantiate with kwargs at
    boot; every stage reads from here."""

    sample_rate: int = TWILIO_SAMPLE_RATE_HZ
    hpf_cutoff_hz: float = DEFAULT_HPF_CUTOFF_HZ
    agc_target_dbfs: float = DEFAULT_AGC_TARGET_DBFS
    agc_attack_ms: float = DEFAULT_AGC_ATTACK_MS
    agc_release_ms: float = DEFAULT_AGC_RELEASE_MS
    agc_max_gain_db: float = DEFAULT_AGC_MAX_GAIN_DB
    gate_threshold_dbfs: float = DEFAULT_GATE_THRESHOLD_DBFS


@dataclass(slots=True)
class AudioFrontend:
    """Composes DC → HPF → AGC → gate into a single per-frame call.
    One instance per call session — the HPF + AGC state is per-stream,
    resetting mid-call would sound like a click at the seam."""

    config: FrontendConfig = field(default_factory=FrontendConfig)
    _hpf: HpfState = field(default_factory=HpfState, init=False)
    _agc: AgcState = field(default_factory=AgcState, init=False)

    def process_pcm16(self, pcm: bytes | npt.NDArray[np.int16]) -> Int16Array:
        """PCM16 bytes/array in → cleaned int16 numpy array out.
        Returns an int16 array (not bytes) so callers can pass it
        straight to Deepgram's WS transport or re-encode to μ-law
        via `codec.pcm16_to_ulaw` without a byte-round-trip."""
        samples = pcm16_to_float32(pcm)
        samples = remove_dc(samples)
        samples = highpass(
            samples,
            self._hpf,
            sample_rate=self.config.sample_rate,
            cutoff_hz=self.config.hpf_cutoff_hz,
        )
        samples = agc(
            samples,
            self._agc,
            sample_rate=self.config.sample_rate,
            target_dbfs=self.config.agc_target_dbfs,
            attack_ms=self.config.agc_attack_ms,
            release_ms=self.config.agc_release_ms,
            max_gain_db=self.config.agc_max_gain_db,
        )
        samples = noise_gate(samples, threshold_dbfs=self.config.gate_threshold_dbfs)
        return float32_to_pcm16(samples)


__all__ = [
    "DEFAULT_AGC_ATTACK_MS",
    "DEFAULT_AGC_MAX_GAIN_DB",
    "DEFAULT_AGC_RELEASE_MS",
    "DEFAULT_AGC_TARGET_DBFS",
    "DEFAULT_GATE_THRESHOLD_DBFS",
    "DEFAULT_HPF_CUTOFF_HZ",
    "AgcState",
    "AudioFrontend",
    "FrontendConfig",
    "HpfState",
    "agc",
    "float32_to_pcm16",
    "highpass",
    "noise_gate",
    "pcm16_to_float32",
    "remove_dc",
    "rms_dbfs",
]
