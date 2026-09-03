"""Noise sweep driver — spec §9.5.

Iterates the eval corpus x noise types x SNR levels, pushes each
mixed frame through denoise + frontend, hands the transcript to a
mock or real STT, extracts the slot, records metrics. Emits a JSON
report + prints a per-cell table.

Design:

- **Corpus format.** `data/eval/noise/manifest.json` shape:
    {
      "utterances": [
        {"id": "u001", "wav": "utterances/u001.wav",
         "transcript": "yes", "slot_extractor": "intent",
         "expected_slot": "yes"},
        ...
      ],
      "noise_samples": [
        {"type": "rain", "wav": "noise/rain_01.wav"},
        {"type": "wind", "wav": "noise/wind_01.wav"},
        ...
      ]
    }

- **STT backend.** Two options:
    - `mock`: pass the reference transcript through unchanged (isolates
      the denoise / extractor effect from STT errors). Use to prove
      the harness runs without a Deepgram key.
    - `deepgram`: real Flux WS call. Requires DEEPGRAM_API_KEY. Not
      wired here as the default — the harness stays runnable in CI.

- **Extraction.** Uses `keyword_rules.extract_*` (same path as the
  production runner). LLM extraction lands with P4/P6.

- **Report shape.** Written to `data/eval/noise/reports/<timestamp>.json`:
    {
      "ran_at": "2026-08-17T...",
      "harness_version": "1",
      "denoise_provider": "rnnoise",
      "cells": [{"noise_type": "rain", "snr_db": 10, ...},
                ...]
    }
"""

from __future__ import annotations

import json
import time
import wave
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fg_voice.audio.denoise import Denoiser, NoOpDenoiser
from fg_voice.audio.frontend import AudioFrontend
from fg_voice.extraction import keyword_rules

from .metrics import CellSummary, TrialResult
from .mixer import mix_at_snr

DEFAULT_SNR_LEVELS_DB: tuple[float, ...] = (20.0, 15.0, 10.0, 5.0, 0.0)

# Registry of extractor callables keyed by slot name. Same dispatch
# shape the runner uses.
_EXTRACTORS: dict[str, Callable[[str], str | None]] = {
    "intent": lambda t: (
        keyword_rules.extract_intent(t).value
        if keyword_rules.extract_intent(t).value != "unclear"
        else None
    ),
    "hazard_type": lambda t: (
        keyword_rules.extract_hazard_type(t).value
        if keyword_rules.extract_hazard_type(t).value != "unclear"
        else None
    ),
    "severity": lambda t: (
        keyword_rules.extract_severity(t).value
        if keyword_rules.extract_severity(t).value != "unclear"
        else None
    ),
    "confirmation": lambda t: (
        keyword_rules.extract_confirmation(t).value
        if keyword_rules.extract_confirmation(t).value != "unclear"
        else None
    ),
}


class SttBackend:
    """Protocol-lite base — subclass to plug in Deepgram or a mock."""

    def transcribe(self, pcm16: bytes, reference: str) -> str:  # pragma: no cover
        raise NotImplementedError


@dataclass
class MockSttBackend(SttBackend):
    """Returns the reference transcript verbatim. Use to isolate the
    denoise/extractor contribution from STT error."""

    def transcribe(self, pcm16: bytes, reference: str) -> str:
        return reference


@dataclass
class ThresholdedNoiseSttBackend(SttBackend):
    """Simulates STT degradation with SNR: if the achieved SNR at the
    input dropped below `snr_floor_db`, the transcript is empty (dead
    audio); otherwise return the reference. Used by the harness self-
    test to prove the metric aggregation works with varied inputs."""

    snr_floor_db: float = 3.0
    _last_snr: float | None = None

    def set_snr(self, snr: float) -> None:
        self._last_snr = snr

    def transcribe(self, pcm16: bytes, reference: str) -> str:
        if self._last_snr is not None and self._last_snr < self.snr_floor_db:
            return ""
        return reference


@dataclass(slots=True)
class UtteranceSpec:
    id: str
    wav_path: Path
    transcript: str
    slot_extractor: str
    expected_slot: str


@dataclass(slots=True)
class NoiseSpec:
    type: str
    wav_path: Path


@dataclass(slots=True)
class CorpusManifest:
    utterances: list[UtteranceSpec]
    noise_samples: list[NoiseSpec]

    @classmethod
    def from_json(cls, path: Path) -> CorpusManifest:
        raw = json.loads(path.read_text(encoding="utf-8"))
        root = path.parent
        utterances = [
            UtteranceSpec(
                id=u["id"],
                wav_path=root / u["wav"],
                transcript=u["transcript"],
                slot_extractor=u["slot_extractor"],
                expected_slot=u["expected_slot"],
            )
            for u in raw["utterances"]
        ]
        noises = [NoiseSpec(type=n["type"], wav_path=root / n["wav"]) for n in raw["noise_samples"]]
        return cls(utterances=utterances, noise_samples=noises)


def _load_wav_pcm16(path: Path) -> bytes:
    """Read a mono 8 kHz PCM16 WAV. Raises ValueError on any other
    shape so the corpus contract is loud rather than silent."""
    with wave.open(str(path), "rb") as wf:
        if wf.getnchannels() != 1:
            raise ValueError(f"{path}: expected mono, got {wf.getnchannels()} channels")
        if wf.getsampwidth() != 2:
            raise ValueError(f"{path}: expected 16-bit, got {wf.getsampwidth() * 8}-bit")
        if wf.getframerate() != 8000:
            raise ValueError(f"{path}: expected 8000 Hz, got {wf.getframerate()} Hz")
        return wf.readframes(wf.getnframes())


def _apply_pipeline(
    pcm16: bytes,
    *,
    denoiser: Denoiser,
    frontend: AudioFrontend,
) -> bytes:
    """Push pcm16 through denoise + frontend one 20 ms frame at a
    time. Returns the concatenated PCM16 output."""
    frame_bytes = 320  # 160 samples * 2 bytes
    out_parts: list[bytes] = []
    for i in range(0, len(pcm16), frame_bytes):
        frame = pcm16[i : i + frame_bytes]
        if not frame:
            break
        cleaned = denoiser.process_pcm16(frame)
        processed = frontend.process_pcm16(cleaned)
        # AudioFrontend returns numpy int16 — coerce back to bytes.
        if isinstance(processed, bytes):
            out_parts.append(processed)
        else:
            out_parts.append(processed.tobytes())
    return b"".join(out_parts)


def run_one_trial(
    utterance: UtteranceSpec,
    noise: NoiseSpec,
    snr_db: float,
    *,
    stt: SttBackend,
    denoiser_factory: Callable[[], Denoiser] | None = None,
) -> TrialResult:
    """Run one utterance x noise x SNR through the pipeline."""
    signal_pcm = _load_wav_pcm16(utterance.wav_path)
    noise_pcm = _load_wav_pcm16(noise.wav_path)
    mix = mix_at_snr(signal_pcm, noise_pcm, snr_db=snr_db)

    denoiser = (denoiser_factory or NoOpDenoiser)()
    frontend = AudioFrontend()
    cleaned = _apply_pipeline(mix.mixed, denoiser=denoiser, frontend=frontend)

    if isinstance(stt, ThresholdedNoiseSttBackend):
        stt.set_snr(mix.achieved_snr_db)
    transcript = stt.transcribe(cleaned, utterance.transcript)

    extractor = _EXTRACTORS.get(utterance.slot_extractor)
    extracted = extractor(transcript) if extractor is not None else None

    return TrialResult(
        utterance_id=utterance.id,
        ref_transcript=utterance.transcript,
        hyp_transcript=transcript,
        expected_slot=utterance.expected_slot,
        extracted_slot=extracted,
        turns_used=1,  # extractor runs once per trial in the harness
        barge_in_count=0,  # not simulated at the harness level
        barge_in_false_count=0,
        achieved_snr_db=mix.achieved_snr_db,
    )


def run_sweep(
    manifest: CorpusManifest,
    *,
    snr_levels_db: Iterable[float] = DEFAULT_SNR_LEVELS_DB,
    stt: SttBackend | None = None,
    denoiser_factory: Callable[[], Denoiser] | None = None,
) -> list[CellSummary]:
    """Iterate corpus x noise x snr; return per-cell summaries.
    Ordering: (noise_type ascending, snr_db descending)."""
    stt = stt or MockSttBackend()
    cells: dict[tuple[str, float], CellSummary] = {}
    for noise in manifest.noise_samples:
        for snr in snr_levels_db:
            key = (noise.type, float(snr))
            cell = cells.setdefault(key, CellSummary(noise_type=noise.type, snr_db=float(snr)))
            for utterance in manifest.utterances:
                trial = run_one_trial(
                    utterance,
                    noise,
                    float(snr),
                    stt=stt,
                    denoiser_factory=denoiser_factory,
                )
                cell.add(trial)
    return sorted(cells.values(), key=lambda c: (c.noise_type, -c.snr_db))


def write_report(
    cells: list[CellSummary],
    output_dir: Path,
    *,
    denoise_provider: str,
    harness_version: str = "1",
    stt_backend_name: str = "mock",
) -> Path:
    """Serialise cells + metadata to a timestamped JSON file. Returns
    the path so the caller can echo it in logs."""
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = output_dir / f"noise_sweep_{ts}.json"
    payload: dict[str, Any] = {
        "ran_at_utc": ts,
        "harness_version": harness_version,
        "denoise_provider": denoise_provider,
        "stt_backend": stt_backend_name,
        "cells": [c.to_dict() for c in cells],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def check_exit_gate(
    cells: list[CellSummary],
    *,
    min_slot_accuracy_at_10db: float = 0.92,
    min_slot_accuracy_at_5db: float = 0.85,
    max_false_barge_in_rate: float = 0.02,
    max_premature_cutoff_rate: float = 0.03,
    quality_gate_min_snr_db: float = 5.0,
) -> tuple[bool, list[str]]:
    """P3 exit gate check. Returns (passed, list_of_failures). Uses
    the spec §9.5 targets as defaults.

    The false-barge-in + premature-cutoff limits only apply at
    `quality_gate_min_snr_db` and above — below that, the spec says
    "graceful DTMF fallback", i.e. the pipeline is EXPECTED to
    degrade past those thresholds."""
    failures: list[str] = []
    for cell in cells:
        if cell.snr_db == 10.0 and cell.slot_accuracy < min_slot_accuracy_at_10db:
            failures.append(
                f"{cell.noise_type}@10dB slot_accuracy={cell.slot_accuracy:.3f} "
                f"< target={min_slot_accuracy_at_10db}"
            )
        if cell.snr_db == 5.0 and cell.slot_accuracy < min_slot_accuracy_at_5db:
            failures.append(
                f"{cell.noise_type}@5dB slot_accuracy={cell.slot_accuracy:.3f} "
                f"< target={min_slot_accuracy_at_5db}"
            )
        # Quality gates only apply at supported SNR (>= 5 dB per spec).
        if cell.snr_db < quality_gate_min_snr_db:
            continue
        if cell.false_barge_in_rate > max_false_barge_in_rate:
            failures.append(
                f"{cell.noise_type}@{cell.snr_db}dB false_barge_in_rate="
                f"{cell.false_barge_in_rate:.3f} > max={max_false_barge_in_rate}"
            )
        if cell.premature_cutoff_rate > max_premature_cutoff_rate:
            failures.append(
                f"{cell.noise_type}@{cell.snr_db}dB premature_cutoff_rate="
                f"{cell.premature_cutoff_rate:.3f} > max={max_premature_cutoff_rate}"
            )
    return (len(failures) == 0, failures)


SttBackendName = Literal["mock", "thresholded", "deepgram"]


def make_stt_backend(name: SttBackendName) -> SttBackend:
    if name == "mock":
        return MockSttBackend()
    if name == "thresholded":
        return ThresholdedNoiseSttBackend()
    raise NotImplementedError(f"stt backend {name!r} not wired (Deepgram lands with real key)")


__all__ = [
    "DEFAULT_SNR_LEVELS_DB",
    "CorpusManifest",
    "MockSttBackend",
    "NoiseSpec",
    "SttBackend",
    "SttBackendName",
    "ThresholdedNoiseSttBackend",
    "UtteranceSpec",
    "check_exit_gate",
    "make_stt_backend",
    "run_one_trial",
    "run_sweep",
    "write_report",
]
