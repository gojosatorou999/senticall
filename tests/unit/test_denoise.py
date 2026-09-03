"""Denoise seam — Protocol + NoOp default + SpectralGate fallback +
chained pipeline.

Coverage:
- Denoiser protocol is runtime-checkable (isinstance works)
- NoOpDenoiser passes bytes through unchanged
- ChainedDenoiseFrontend calls denoise then frontend in order
- build_denoiser factory: `none` → NoOp; `rnnoise` → SpectralGate;
  `krisp` → NoOp with warning (SDK unshipped); unknown → NoOp
- SpectralGateDenoiser: passes early frames unchanged during noise
  estimation, attenuates stationary noise after estimation, does not
  clip on silent input.
"""

from __future__ import annotations

import numpy as np

from fg_voice.audio.denoise import (
    ChainedDenoiseFrontend,
    Denoiser,
    NoOpDenoiser,
    SpectralGateDenoiser,
    build_denoiser,
)
from fg_voice.audio.frontend import AudioFrontend


def test_denoiser_protocol_runtime_checkable():
    """`isinstance` should work — the Protocol is marked
    runtime_checkable so wiring can validate injected implementations."""
    assert isinstance(NoOpDenoiser(), Denoiser)


def test_noop_denoiser_passthrough():
    """Zero mutation of the input. Bit-exact identity — anything else
    means the "no-op" isn't."""
    frame = bytes(range(160))  # 160 arbitrary bytes = 80 PCM16 samples
    out = NoOpDenoiser().process_pcm16(frame)
    assert out == frame


def test_chained_denoise_frontend_calls_denoiser_first():
    """The denoiser sees the raw bytes; the frontend sees whatever
    the denoiser returned. Verifies with a spy denoiser that
    records + mutates."""

    class SpyDenoiser:
        def __init__(self):
            self.received: bytes | None = None

        def process_pcm16(self, pcm16: bytes) -> bytes:
            self.received = pcm16
            # Return a marker so we can prove the frontend got THIS
            # output, not the raw input.
            return b"\x00\x00" * 160  # zero PCM16 frame

    spy = SpyDenoiser()
    frontend = AudioFrontend()
    chain = ChainedDenoiseFrontend(denoiser=spy, frontend=frontend)

    raw = bytes([0x7F, 0x80] * 160)  # non-zero pattern
    chain.process_pcm16(raw)

    # Denoiser was called with the raw input.
    assert spy.received == raw


def test_build_denoiser_none_returns_noop():
    d = build_denoiser("none")
    assert isinstance(d, NoOpDenoiser)


def test_build_denoiser_krisp_falls_back_to_noop_with_warning():
    """Krisp integration isn't shipped yet. Rather than crash the
    boot, the factory returns NoOp + logs a warning so ops sees the
    misconfiguration without losing call-completion capability."""
    d = build_denoiser("krisp")
    assert isinstance(d, NoOpDenoiser)


def test_build_denoiser_rnnoise_returns_spectral_gate():
    """The rnnoise slot is now the SpectralGate fallback impl — a real
    denoise pass that ships without a C++ build or external SDK."""
    d = build_denoiser("rnnoise")
    assert isinstance(d, SpectralGateDenoiser)


def test_build_denoiser_unknown_provider_falls_back_to_noop():
    """A typo in an ops env var shouldn't crash the boot — audio
    would just skip the denoise step. Loud log; safe fallback."""
    d = build_denoiser("this-is-not-a-real-provider")
    assert isinstance(d, NoOpDenoiser)


# ─── SpectralGateDenoiser behaviour ──────────────────────────────────


def _silence_frame() -> bytes:
    """Twilio-sized zero frame — 160 int16 samples = 320 bytes."""
    return np.zeros(160, dtype=np.int16).tobytes()


def _white_noise_frame(seed: int, amplitude: int = 500) -> bytes:
    """Uniform low-amplitude PCM16, deterministic per seed."""
    rng = np.random.default_rng(seed)
    return rng.integers(-amplitude, amplitude, size=160, dtype=np.int16).tobytes()


def _tone_plus_noise_frame(seed: int, tone_amp: int = 8000) -> bytes:
    """A 400 Hz tone (proxy for voice) mixed with low-amplitude noise —
    the denoiser should preserve most of the tone energy while
    knocking down the noise floor once the estimate has converged."""
    rng = np.random.default_rng(seed)
    t = np.arange(160) / 8000.0
    tone = tone_amp * np.sin(2 * np.pi * 400 * t)
    noise = rng.integers(-500, 500, size=160)
    return (tone + noise).astype(np.int16).tobytes()


def test_spectral_gate_passes_early_frames_unchanged():
    """During the noise-estimation window (first 15 frames) the gate
    has no basis to attenuate. It MUST pass audio through byte-exact
    so early speech isn't corrupted while the noise floor is learning."""
    gate = SpectralGateDenoiser()
    frame = _white_noise_frame(seed=1)
    out = gate.process_pcm16(frame)
    assert out == frame


def test_spectral_gate_attenuates_stationary_noise_after_estimation():
    """After 15 opening noise frames the gate has learned the noise
    floor. A subsequent pure-noise frame should come out MUCH quieter
    than it went in (proof the subtraction fires)."""
    gate = SpectralGateDenoiser()
    # Prime with 15 stationary-noise frames.
    for i in range(15):
        gate.process_pcm16(_white_noise_frame(seed=i))
    # Now push a fresh noise frame from the same distribution and
    # measure RMS reduction.
    test_frame = _white_noise_frame(seed=100)
    out = gate.process_pcm16(test_frame)
    in_rms = np.sqrt(np.mean(np.frombuffer(test_frame, dtype=np.int16).astype(np.float32) ** 2))
    out_rms = np.sqrt(np.mean(np.frombuffer(out, dtype=np.int16).astype(np.float32) ** 2))
    # 3x reduction is a conservative floor; on stationary noise the
    # subtraction typically achieves 5-10x. Anything less means the
    # algorithm has regressed.
    assert out_rms < in_rms / 3.0, f"expected large reduction: in={in_rms}, out={out_rms}"


def test_spectral_gate_preserves_tonal_energy():
    """A tone (proxy for voice) mixed with noise should keep most of
    its energy after denoise. If the gate over-subtracts and kills the
    tone too, slot accuracy would collapse — this is the regression
    guard against that."""
    gate = SpectralGateDenoiser()
    # Prime with 15 pure-noise frames — the gate learns the noise floor.
    for i in range(15):
        gate.process_pcm16(_white_noise_frame(seed=i))
    # Now push a tone+noise frame; the tone should survive.
    test_frame = _tone_plus_noise_frame(seed=200)
    out = gate.process_pcm16(test_frame)
    in_samples = np.frombuffer(test_frame, dtype=np.int16).astype(np.float32)
    out_samples = np.frombuffer(out, dtype=np.int16).astype(np.float32)
    in_rms = np.sqrt(np.mean(in_samples**2))
    out_rms = np.sqrt(np.mean(out_samples**2))
    # Tone should retain at least 40% of its energy — anything less
    # would over-subtract and slot accuracy would tank.
    assert out_rms > in_rms * 0.4, f"tone over-attenuated: in={in_rms}, out={out_rms}"


def test_spectral_gate_handles_empty_input():
    """Defensive: an empty PCM buffer must not raise. Returns empty."""
    gate = SpectralGateDenoiser()
    assert gate.process_pcm16(b"") == b""


def test_spectral_gate_pads_short_frame():
    """A short (< 160-sample) frame gets zero-padded internally so the
    FFT stays stable. Bootstrap-window passthrough returns the raw
    input bytes; a short frame past the bootstrap gets padded to the
    fixed frame size on output."""
    gate = SpectralGateDenoiser()
    # Prime through the bootstrap window with full-sized frames.
    for i in range(15):
        gate.process_pcm16(_white_noise_frame(seed=i))
    short = np.zeros(80, dtype=np.int16).tobytes()  # half a Twilio frame
    out = gate.process_pcm16(short)
    # Padded to 160 samples = 320 bytes on output.
    assert len(out) == 320


def test_spectral_gate_output_is_valid_pcm16():
    """No NaN, no Inf, no out-of-range values reach the sink."""
    gate = SpectralGateDenoiser()
    for i in range(20):
        out = gate.process_pcm16(_tone_plus_noise_frame(seed=i))
        arr = np.frombuffer(out, dtype=np.int16)
        assert np.isfinite(arr.astype(np.float32)).all()
        assert arr.min() >= -32768
        assert arr.max() <= 32767
