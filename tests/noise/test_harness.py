"""Noise-sweep harness self-tests.

These prove the harness itself runs end-to-end with a synthetic
corpus. A REAL corpus (Indian-English voices + real noise) lives in
`data/eval/noise/manifest.json` when ops provisions it; the numbers
this test asserts are algorithmically trivial (mock STT returns the
reference verbatim) and don't clear the P3 exit gate on their own.

The point here is to prove:

1. The corpus manifest loads.
2. Mixing produces valid PCM16 at the requested SNR.
3. The pipeline (denoise → frontend → STT → extractor) doesn't crash.
4. Metrics aggregate correctly.
5. The exit-gate checker fires on synthetic pass/fail conditions.
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest

from fg_voice.audio.denoise import NoOpDenoiser, SpectralGateDenoiser

from .harness import (
    CorpusManifest,
    MockSttBackend,
    NoiseSpec,
    ThresholdedNoiseSttBackend,
    UtteranceSpec,
    check_exit_gate,
    make_stt_backend,
    run_one_trial,
    run_sweep,
    write_report,
)
from .metrics import CellSummary, TrialResult, word_error_rate
from .mixer import (
    mix_at_snr,
    synthetic_tone_pcm16,
    synthetic_white_noise_pcm16,
)

# ─── Fixture helpers ────────────────────────────────────────────────


def _write_wav(path: Path, pcm16: bytes, sample_rate: int = 8000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16)


@pytest.fixture
def synthetic_corpus(tmp_path: Path) -> CorpusManifest:
    """Two utterances, two noise samples — small enough to run in
    tens of milliseconds, large enough to prove the harness works."""
    # Utterances: reference transcripts + expected slot values.
    _write_wav(tmp_path / "utterances/u_yes.wav", synthetic_tone_pcm16(freq_hz=300))
    _write_wav(tmp_path / "utterances/u_no.wav", synthetic_tone_pcm16(freq_hz=500))
    # Noise samples: white noise + tone (proxy for structured noise).
    _write_wav(tmp_path / "noise/rain.wav", synthetic_white_noise_pcm16(seed=1))
    _write_wav(tmp_path / "noise/wind.wav", synthetic_white_noise_pcm16(seed=2))

    manifest = {
        "utterances": [
            {
                "id": "u001",
                "wav": "utterances/u_yes.wav",
                "transcript": "yes",
                "slot_extractor": "intent",
                "expected_slot": "yes",
            },
            {
                "id": "u002",
                "wav": "utterances/u_no.wav",
                "transcript": "no",
                "slot_extractor": "intent",
                "expected_slot": "no",
            },
        ],
        "noise_samples": [
            {"type": "rain", "wav": "noise/rain.wav"},
            {"type": "wind", "wav": "noise/wind.wav"},
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return CorpusManifest.from_json(manifest_path)


# ─── Manifest loading ───────────────────────────────────────────────


def test_manifest_from_json_loads(synthetic_corpus: CorpusManifest) -> None:
    assert len(synthetic_corpus.utterances) == 2
    assert len(synthetic_corpus.noise_samples) == 2
    assert synthetic_corpus.utterances[0].expected_slot == "yes"


def test_manifest_paths_resolve(synthetic_corpus: CorpusManifest) -> None:
    """Every wav path referenced by the manifest should exist on disk
    after loading — the loader resolves relative to the manifest dir."""
    for u in synthetic_corpus.utterances:
        assert u.wav_path.exists(), f"utterance wav missing: {u.wav_path}"
    for n in synthetic_corpus.noise_samples:
        assert n.wav_path.exists(), f"noise wav missing: {n.wav_path}"


# ─── Mixer behaviour ────────────────────────────────────────────────


def test_mix_at_snr_hits_target_within_tolerance() -> None:
    """Achieved SNR should sit within 1 dB of the requested value for
    non-clipping inputs. Wider tolerance hides mixer bugs; tighter
    fails legitimately on rounding at edge amplitudes."""
    signal = synthetic_tone_pcm16(duration_ms=500)
    noise = synthetic_white_noise_pcm16(duration_ms=500, seed=7)
    for target in (0.0, 5.0, 10.0, 20.0):
        result = mix_at_snr(signal, noise, snr_db=target)
        assert abs(result.achieved_snr_db - target) < 1.0, (
            f"target={target} achieved={result.achieved_snr_db}"
        )


def test_mix_at_snr_returns_same_length() -> None:
    signal = synthetic_tone_pcm16(duration_ms=800)
    noise = synthetic_white_noise_pcm16(duration_ms=200, seed=3)
    result = mix_at_snr(signal, noise, snr_db=10.0)
    assert len(result.mixed) == len(signal)


def test_mix_handles_zero_signal() -> None:
    """Silent input → passthrough. Should not divide by zero."""
    signal = b"\x00" * 320
    noise = synthetic_white_noise_pcm16(duration_ms=20, seed=1)
    result = mix_at_snr(signal, noise, snr_db=10.0)
    assert result.mixed == signal
    assert result.achieved_snr_db < 0


def test_mix_handles_zero_noise() -> None:
    """Silent noise → signal passes through at "infinite" SNR."""
    signal = synthetic_tone_pcm16(duration_ms=20)
    noise = b"\x00" * 320
    result = mix_at_snr(signal, noise, snr_db=10.0)
    assert result.mixed == signal
    assert result.achieved_snr_db > 100  # sentinel for infinite


# ─── Trial runner ───────────────────────────────────────────────────


def test_run_one_trial_with_noop_denoiser(synthetic_corpus: CorpusManifest) -> None:
    utterance = synthetic_corpus.utterances[0]
    noise = synthetic_corpus.noise_samples[0]
    trial = run_one_trial(
        utterance,
        noise,
        snr_db=15.0,
        stt=MockSttBackend(),
        denoiser_factory=NoOpDenoiser,
    )
    assert trial.utterance_id == "u001"
    # Mock STT returned the reference, keyword-rule extracted "yes".
    assert trial.slot_correct


def test_run_one_trial_with_spectral_gate(synthetic_corpus: CorpusManifest) -> None:
    """The spectral-gate denoiser should not crash on synthetic inputs
    and should not change the fundamental slot-correctness outcome
    (mock STT feeds the reference regardless)."""
    utterance = synthetic_corpus.utterances[0]
    noise = synthetic_corpus.noise_samples[0]
    trial = run_one_trial(
        utterance,
        noise,
        snr_db=10.0,
        stt=MockSttBackend(),
        denoiser_factory=SpectralGateDenoiser,
    )
    assert trial.slot_correct


# ─── Sweep + report ─────────────────────────────────────────────────


def test_sweep_produces_cells_for_every_pair(synthetic_corpus: CorpusManifest) -> None:
    cells = run_sweep(synthetic_corpus, snr_levels_db=(10.0, 5.0))
    # 2 noise types x 2 SNRs = 4 cells
    assert len(cells) == 4
    # Each cell has all utterances (2)
    for c in cells:
        assert c.n == 2


def test_sweep_report_written_and_readable(
    tmp_path: Path, synthetic_corpus: CorpusManifest
) -> None:
    cells = run_sweep(synthetic_corpus, snr_levels_db=(10.0,))
    report = write_report(cells, tmp_path / "reports", denoise_provider="none")
    payload = json.loads(report.read_text())
    assert payload["denoise_provider"] == "none"
    assert len(payload["cells"]) == len(cells)


# ─── Thresholded STT + degradation curve ────────────────────────────


def test_thresholded_stt_degrades_slot_accuracy_at_low_snr(
    synthetic_corpus: CorpusManifest,
) -> None:
    """With the thresholded STT (empty transcript below 3 dB), a
    0 dB sweep should show significantly lower slot accuracy than
    a 20 dB sweep. Proves the harness aggregates a degradation
    curve correctly across SNR levels."""
    stt = ThresholdedNoiseSttBackend(snr_floor_db=3.0)
    cells_high = run_sweep(synthetic_corpus, snr_levels_db=(20.0,), stt=stt)
    cells_low = run_sweep(synthetic_corpus, snr_levels_db=(0.0,), stt=stt)
    assert cells_high[0].slot_accuracy > cells_low[0].slot_accuracy


# ─── Exit gate ──────────────────────────────────────────────────────


def _make_cell(
    noise_type: str,
    snr: float,
    *,
    slot_acc: float = 1.0,
    false_bi: float = 0.0,
    cutoff: float = 0.0,
) -> CellSummary:
    """Synthesise a cell with the given aggregate metrics. Bypasses
    the real harness so we can test the exit-gate logic in isolation."""
    cell = CellSummary(noise_type=noise_type, snr_db=snr)
    total = 100
    correct = int(slot_acc * total)
    for i in range(total):
        cell.add(
            TrialResult(
                utterance_id=f"u{i}",
                ref_transcript="yes",
                hyp_transcript="yes" if i < correct else "no",
                expected_slot="yes",
                extracted_slot="yes" if i < correct else "no",
                turns_used=1,
                # Distribute barge-ins so the aggregate lands on false_bi.
                barge_in_count=1,
                barge_in_false_count=1 if i < int(false_bi * total) else 0,
                achieved_snr_db=snr,
            )
        )
    if cutoff > 0:
        # Overwrite a portion of trials' hyp_transcript to trigger
        # premature_cutoff_rate.
        n_cutoff = int(cutoff * total)
        for i in range(n_cutoff):
            trial = cell.trials[i]
            # Empty hyp → premature_cutoff True.
            cell.trials[i] = TrialResult(
                utterance_id=trial.utterance_id,
                ref_transcript="yes indeed the water rose",
                hyp_transcript="",
                expected_slot=trial.expected_slot,
                extracted_slot=trial.extracted_slot,
                turns_used=trial.turns_used,
                barge_in_count=trial.barge_in_count,
                barge_in_false_count=trial.barge_in_false_count,
                achieved_snr_db=trial.achieved_snr_db,
            )
    return cell


def test_exit_gate_passes_on_spec_targets() -> None:
    cells = [_make_cell("rain", 10.0, slot_acc=0.93), _make_cell("rain", 5.0, slot_acc=0.86)]
    passed, failures = check_exit_gate(cells)
    assert passed, failures


def test_exit_gate_fails_on_low_slot_accuracy_at_10db() -> None:
    cells = [_make_cell("rain", 10.0, slot_acc=0.85)]  # below 0.92
    passed, failures = check_exit_gate(cells)
    assert not passed
    assert any("10dB" in f for f in failures)


def test_exit_gate_fails_on_high_false_barge_in() -> None:
    cells = [_make_cell("rain", 10.0, slot_acc=1.0, false_bi=0.05)]
    passed, failures = check_exit_gate(cells)
    assert not passed
    assert any("false_barge_in_rate" in f for f in failures)


def test_exit_gate_fails_on_high_premature_cutoff() -> None:
    cells = [_make_cell("rain", 10.0, slot_acc=1.0, cutoff=0.10)]
    passed, failures = check_exit_gate(cells)
    assert not passed
    assert any("premature_cutoff_rate" in f for f in failures)


# ─── Backend factory ────────────────────────────────────────────────


def test_make_stt_backend_supports_mock_and_thresholded() -> None:
    assert isinstance(make_stt_backend("mock"), MockSttBackend)
    assert isinstance(make_stt_backend("thresholded"), ThresholdedNoiseSttBackend)


def test_make_stt_backend_rejects_unwired_deepgram() -> None:
    with pytest.raises(NotImplementedError, match="Deepgram"):
        make_stt_backend("deepgram")


# ─── WER sanity ─────────────────────────────────────────────────────


def test_wer_is_zero_on_exact_match() -> None:
    assert word_error_rate("yes I am reporting", "yes I am reporting") == 0.0


def test_wer_is_one_on_total_substitution() -> None:
    assert word_error_rate("yes", "no") == 1.0


def test_wer_ignores_case_and_punctuation() -> None:
    assert word_error_rate("Yes, I am!", "yes i am") == 0.0


# ─── Convenience: NoiseSpec / UtteranceSpec are dataclasses ─────────


def test_specs_have_expected_fields() -> None:
    u = UtteranceSpec(
        id="u", wav_path=Path("."), transcript="", slot_extractor="intent", expected_slot="yes"
    )
    assert u.id == "u"
    assert u.slot_extractor == "intent"
    n = NoiseSpec(type="rain", wav_path=Path("."))
    assert n.type == "rain"
