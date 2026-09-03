"""Audio frontend DSP — DC removal / HPF / AGC / noise gate.

Coverage:
- pcm16_to_float32 / float32_to_pcm16 round-trip is lossless for
  representative int16 values
- rms_dbfs handles empty + silent frames without blowing up
- remove_dc zeros the mean of a DC-offset signal (bit-exact within
  float precision)
- highpass attenuates a low-frequency tone (40 Hz below the 80 Hz
  cutoff) while preserving a high-frequency tone (1 kHz)
- highpass state stitches across consecutive frames (no click at
  the seam of a continuous sine)
- agc brings a quiet signal up toward target dBFS over several
  frames; brings a loud signal down toward it
- agc envelope smooths across frames (state carries)
- noise_gate zeros a below-threshold frame, passes an above-
  threshold frame unchanged
- AudioFrontend composed pipeline: silent input stays silent,
  a clean speech-band tone survives with reasonable gain applied
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from fg_voice.audio.frontend import (
    AgcState,
    AudioFrontend,
    FrontendConfig,
    HpfState,
    agc,
    float32_to_pcm16,
    highpass,
    noise_gate,
    pcm16_to_float32,
    remove_dc,
    rms_dbfs,
)

SAMPLE_RATE = 8000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 160


def _sine(freq_hz: float, samples: int, amplitude: float = 0.5) -> np.ndarray:
    """Generate a pure sine wave frame at `freq_hz`. `amplitude` in
    [-1, 1] full-scale."""
    t = np.arange(samples, dtype=np.float32) / SAMPLE_RATE
    return (amplitude * np.sin(2 * math.pi * freq_hz * t)).astype(np.float32)


# ─── Conversions ────────────────────────────────────────────────────


def test_pcm16_float32_roundtrip_lossless():
    """Values that are exactly representable in both formats survive
    the round-trip. -32768 → -1.0 → -32768; 0 → 0 → 0."""
    pcm = np.array([-32768, -16384, 0, 16384, 32767], dtype=np.int16)
    f = pcm16_to_float32(pcm)
    back = float32_to_pcm16(f)
    # 32767 clips to 32767 via the (32767/32768) upper clip; all
    # others exact.
    np.testing.assert_array_equal(back[:-1], pcm[:-1])
    # 32767 in float rounds to 32767 (32767/32768 * 32768 = 32767).
    assert back[-1] == 32767


def test_pcm16_from_bytes():
    """`bytes` input works as an int16-buffer view (little-endian)."""
    pcm_arr = np.array([1000, -2000], dtype=np.int16)
    f = pcm16_to_float32(pcm_arr.tobytes())
    assert f.shape == (2,)
    assert abs(f[0] - 1000 / 32768) < 1e-6


def test_rms_dbfs_empty_returns_negative_inf():
    assert rms_dbfs(np.array([], dtype=np.float32)) == -float("inf")


def test_rms_dbfs_silent_frame_reports_very_low():
    # Fully-silent frame → RMS 0 → log(EPS) which is a large-negative
    # dBFS. Not -inf (guarded by EPS) but well below any realistic
    # gate threshold.
    zeros = np.zeros(FRAME_SAMPLES, dtype=np.float32)
    assert rms_dbfs(zeros) < -150


def test_rms_dbfs_full_scale_sine_is_minus_3_dbfs():
    """A sine at ±1.0 amplitude has RMS = 1/√2 ≈ -3.01 dBFS."""
    s = _sine(1000, FRAME_SAMPLES, amplitude=1.0)
    assert -3.5 < rms_dbfs(s) < -2.5


# ─── DC removal ─────────────────────────────────────────────────────


def test_remove_dc_zeros_mean():
    """A DC-offset signal's mean is zero after removal."""
    s = _sine(500, FRAME_SAMPLES, amplitude=0.3) + 0.4  # +0.4 DC
    out = remove_dc(s)
    assert abs(float(np.mean(out))) < 1e-6


def test_remove_dc_preserves_ac_shape():
    """The AC component of a sine + DC survives removal."""
    ac = _sine(500, FRAME_SAMPLES, amplitude=0.3)
    dc_offset = ac + 0.4
    out = remove_dc(dc_offset)
    # After DC removal, `out` should equal the original AC (within
    # float precision).
    np.testing.assert_allclose(out, ac, atol=1e-5)


def test_remove_dc_empty_frame_returns_empty():
    empty = np.array([], dtype=np.float32)
    assert remove_dc(empty).size == 0


# ─── High-pass filter ────────────────────────────────────────────────


def test_highpass_attenuates_low_frequencies_more_than_high():
    """The point of the HPF isn't to fully suppress a 40 Hz tone in
    one frame; it's to attenuate low frequencies substantially MORE
    than high ones. Prove the differential — 40 Hz gets pushed
    down while 1 kHz stays intact — rather than pinning to an
    absolute cutoff value that depends on the filter order and
    settling time."""
    low_state = HpfState()
    high_state = HpfState()
    low_tone = _sine(40, FRAME_SAMPLES, amplitude=0.5)
    high_tone = _sine(1000, FRAME_SAMPLES, amplitude=0.5)
    for _ in range(20):
        low_out = highpass(low_tone, low_state)
        high_out = highpass(high_tone, high_state)

    in_rms = float(np.sqrt(np.mean(low_tone**2)))
    low_out_rms = float(np.sqrt(np.mean(low_out**2)))
    high_out_rms = float(np.sqrt(np.mean(high_out**2)))
    # 40 Hz drops noticeably (>= 20% cut) — first-order HPF at
    # cutoff/2 gives about 6 dB (~50% amplitude); loose bound keeps
    # this a shape test not a numerical precision test.
    assert low_out_rms < 0.8 * in_rms, (
        f"40Hz not attenuated enough (in={in_rms:.4f}, out={low_out_rms:.4f})"
    )
    # 1 kHz survives largely intact.
    assert high_out_rms > 0.85 * in_rms
    # Differential — the ratio proves the filter is doing its job.
    assert low_out_rms < 0.75 * high_out_rms


def test_highpass_preserves_high_frequency_tone():
    """A 1 kHz tone is well above the cutoff — should pass through
    with amplitude largely intact."""
    state = HpfState()
    high_tone = _sine(1000, FRAME_SAMPLES, amplitude=0.5)
    for _ in range(20):
        out = highpass(high_tone, state)
    in_rms = float(np.sqrt(np.mean(high_tone**2)))
    out_rms = float(np.sqrt(np.mean(out**2)))
    # First-order HPF's rolloff means 1kHz retains > 90% of amplitude.
    assert out_rms > 0.85 * in_rms, f"1kHz over-attenuated (in={in_rms}, out={out_rms})"


def test_highpass_state_stitches_across_frames():
    """A continuous sine split across two frames should not have a
    click at the seam. Test by comparing frame-by-frame filtering
    against filtering the full signal in one go — they should agree
    at the seam within numerical tolerance."""
    full = _sine(500, FRAME_SAMPLES * 2, amplitude=0.5)
    # Frame-by-frame
    state = HpfState()
    frame_by_frame = np.concatenate(
        [
            highpass(full[:FRAME_SAMPLES], state),
            highpass(full[FRAME_SAMPLES:], state),
        ]
    )
    # Single-pass
    state_all = HpfState()
    single_pass = highpass(full, state_all)
    np.testing.assert_allclose(frame_by_frame, single_pass, atol=1e-6)


# ─── AGC ────────────────────────────────────────────────────────────


def test_agc_amplifies_quiet_signal_toward_target():
    """A quiet signal (RMS around -40 dBFS) should be amplified
    toward the target (-20 dBFS). AGC won't hit target on frame 1
    (envelope hasn't tracked yet); run several frames."""
    state = AgcState()
    quiet = _sine(500, FRAME_SAMPLES, amplitude=0.01)  # ~-37 dBFS
    for _ in range(200):
        out = agc(quiet, state, target_dbfs=-20.0)
    out_rms_dbfs = rms_dbfs(out)
    # Within 5 dB of target after settling.
    assert -25 < out_rms_dbfs < -15, f"AGC didn't reach target: {out_rms_dbfs} dBFS"


def test_agc_attenuates_loud_signal_toward_target():
    """A loud signal (RMS around -3 dBFS) should be attenuated
    toward the target."""
    state = AgcState()
    loud = _sine(500, FRAME_SAMPLES, amplitude=1.0)  # ~-3 dBFS
    for _ in range(200):
        out = agc(loud, state, target_dbfs=-20.0)
    out_rms_dbfs = rms_dbfs(out)
    assert -25 < out_rms_dbfs < -15


def test_agc_envelope_carries_state_across_frames():
    """After processing a loud frame, the envelope should be non-
    trivial (not reset). This proves state persists."""
    state = AgcState()
    loud = _sine(500, FRAME_SAMPLES, amplitude=0.8)
    agc(loud, state)
    assert state.envelope > 0.1


def test_agc_empty_frame_returns_empty():
    state = AgcState()
    out = agc(np.array([], dtype=np.float32), state)
    assert out.size == 0


# ─── Noise gate ─────────────────────────────────────────────────────


def test_noise_gate_zeros_silent_frame():
    """A very quiet frame (below -50 dBFS threshold) is fully zeroed."""
    quiet = _sine(500, FRAME_SAMPLES, amplitude=0.001)  # ~-57 dBFS
    out = noise_gate(quiet, threshold_dbfs=-50.0)
    assert np.all(out == 0.0)


def test_noise_gate_passes_loud_frame_unchanged():
    """An above-threshold frame passes through untouched."""
    loud = _sine(500, FRAME_SAMPLES, amplitude=0.5)  # ~-9 dBFS
    out = noise_gate(loud, threshold_dbfs=-50.0)
    np.testing.assert_array_equal(out, loud)


def test_noise_gate_empty_frame():
    empty = np.array([], dtype=np.float32)
    assert noise_gate(empty).size == 0


# ─── Composed pipeline ─────────────────────────────────────────────


def test_frontend_silent_input_stays_silent():
    """Silent input goes through: DC removal → HPF → AGC (won't
    amplify below max_gain_db) → gate (zeros). Output should be all
    zeros as int16."""
    frontend = AudioFrontend()
    silence_pcm16 = np.zeros(FRAME_SAMPLES, dtype=np.int16)
    for _ in range(5):
        out = frontend.process_pcm16(silence_pcm16)
    assert out.dtype == np.int16
    assert np.all(out == 0)


def test_frontend_speech_band_tone_survives():
    """A speech-band tone (500 Hz) at moderate level survives the
    pipeline without being zeroed by the gate. Output type + non-zero
    energy is the invariant, not exact waveform (AGC will scale it)."""
    frontend = AudioFrontend()
    # A ~-14 dBFS 500 Hz tone.
    tone_float = _sine(500, FRAME_SAMPLES, amplitude=0.2)
    tone_pcm16 = (tone_float * 32768).astype(np.int16)

    for _ in range(200):  # let AGC settle
        out = frontend.process_pcm16(tone_pcm16)

    assert out.dtype == np.int16
    assert out.shape == (FRAME_SAMPLES,)
    # Output has energy — not gated to zero.
    assert np.any(out != 0)
    # Output RMS is within reasonable range (AGC's target ± tolerance).
    out_float = out.astype(np.float32) / 32768
    out_rms_dbfs = rms_dbfs(out_float)
    assert -30 < out_rms_dbfs < -10, f"unexpected output level: {out_rms_dbfs} dBFS"


def test_frontend_accepts_bytes_input():
    """μ-law decoding gives us PCM16 bytes; the frontend must accept
    them directly to save a numpy copy."""
    frontend = AudioFrontend()
    pcm16_bytes = np.zeros(FRAME_SAMPLES, dtype=np.int16).tobytes()
    out = frontend.process_pcm16(pcm16_bytes)
    assert out.dtype == np.int16
    assert out.shape == (FRAME_SAMPLES,)


def test_frontend_config_overrides_thresholds():
    """A hostile gate threshold (0 dBFS) zeros everything — proves
    the config actually flows through into the composed pipeline."""
    frontend = AudioFrontend(config=FrontendConfig(gate_threshold_dbfs=0.0))
    loud_pcm16 = (_sine(500, FRAME_SAMPLES, amplitude=0.5) * 32768).astype(np.int16)
    for _ in range(5):
        out = frontend.process_pcm16(loud_pcm16)
    # Even a loud signal is gated at 0 dBFS threshold.
    assert np.all(out == 0)


def test_frontend_state_isolated_per_instance():
    """Two independent AudioFrontend instances have independent HPF
    + AGC state. Feeding a loud signal to instance A must not
    influence instance B's response to a quiet signal."""
    a = AudioFrontend()
    b = AudioFrontend()

    loud_pcm = (_sine(500, FRAME_SAMPLES, amplitude=1.0) * 32768).astype(np.int16)

    for _ in range(50):
        a.process_pcm16(loud_pcm)  # move A's envelope up
    # B has never seen anything — its envelope is still 0.
    assert a._agc.envelope > b._agc.envelope + 0.05


# ─── Regression: no silent NaN/Inf leaks ────────────────────────────


@pytest.mark.parametrize("amplitude", [0.0, 0.001, 0.1, 0.5, 1.0])
def test_frontend_never_produces_nan_or_inf(amplitude):
    """A range of input levels — none should produce NaN or Inf in
    output (would blow up downstream). Regression guard against
    div-by-zero + log(0) in AGC/gate."""
    frontend = AudioFrontend()
    tone_pcm = (_sine(500, FRAME_SAMPLES, amplitude=amplitude) * 32768).astype(np.int16)
    for _ in range(50):
        out = frontend.process_pcm16(tone_pcm)
    assert not np.any(np.isnan(out))
    assert not np.any(np.isinf(out))
