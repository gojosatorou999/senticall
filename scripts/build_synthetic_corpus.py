"""Generate the synthetic P3 noise-sweep corpus.

Uses macOS `say` with the en_IN voices (Aman, Rishi, Tara — Apple's
Indian-English TTS) to produce utterance WAVs, and numpy to synthesise
noise samples. All output is mono 16-bit PCM at 8 kHz to match the
Twilio wire format.

This corpus is a STAND-IN for the real one (~20 real speakers x
real field recordings) that ops has to procure. Numbers produced
against this corpus prove the harness runs end-to-end and give a
first-pass exit-gate verdict; they DO NOT clear the gate for
production readiness. See `data/eval/noise/README.md`.

Usage:
    python -m scripts.build_synthetic_corpus \\
        --out-dir data/eval/noise/synthetic \\
        --duration-sec 3
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import numpy as np

# Three en_IN voices shipped with macOS. If a run happens on a
# machine without these voices (older macOS, Linux), the script
# raises loudly rather than silently falling back to en_US.
_EN_IN_VOICES: Final[tuple[str, ...]] = ("Aman", "Rishi", "Tara")


@dataclass(frozen=True, slots=True)
class UtteranceRow:
    """One synthesised utterance. `slot_extractor` matches the
    harness's extractor registry."""

    id: str
    text: str
    slot_extractor: str
    expected_slot: str


# Coverage: every slot the P3 keyword extractor handles. Each row
# gets synthesised in all three en_IN voices, so the corpus is
# (rows x voices) utterances total.
_UTTERANCE_ROWS: Final[tuple[UtteranceRow, ...]] = (
    # ─── intent ────────────────────────────────────────────────────
    UtteranceRow("intent_yes", "yes", "intent", "yes"),
    UtteranceRow("intent_yes_v2", "yes I am reporting", "intent", "yes"),
    UtteranceRow("intent_no", "no", "intent", "no"),
    UtteranceRow("intent_no_v2", "no I am not reporting", "intent", "no"),
    # ─── hazard_type ───────────────────────────────────────────────
    UtteranceRow("hazard_storm", "there is a storm", "hazard_type", "storm"),
    UtteranceRow("hazard_storm_v2", "cyclone damage here", "hazard_type", "storm"),
    UtteranceRow("hazard_sludge", "there is oil sludge on the beach", "hazard_type", "sludge_oil"),
    UtteranceRow("hazard_tide", "the tide is very unusual", "hazard_type", "abnormal_tide"),
    UtteranceRow("hazard_erosion", "there is erosion at the coast", "hazard_type", "erosion"),
    # ─── severity ──────────────────────────────────────────────────
    UtteranceRow("sev_light", "it is light", "severity", "light"),
    UtteranceRow("sev_moderate", "moderate damage", "severity", "moderate"),
    UtteranceRow("sev_extreme", "extreme damage everywhere", "severity", "extreme"),
    # ─── confirmation ──────────────────────────────────────────────
    UtteranceRow("conf_yes", "yes that is correct", "confirmation", "yes"),
    UtteranceRow("conf_no", "no it is not", "confirmation", "no"),
)


def _run_say(text: str, voice: str, out_path: Path) -> None:
    """Invoke macOS `say` writing mono PCM16 WAV at 8 kHz. Raises
    with the underlying stderr if the voice is missing or the
    process errors — synthesis silently substituting the wrong
    voice would corrupt the corpus without a signal."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Word-list args: `say -v <voice> -o <file> --file-format=WAVE
    # --data-format=LEI16@8000 "<text>"`. Not shell-quoted — subprocess
    # handles the exec.
    result = subprocess.run(
        [
            "say",
            "-v",
            voice,
            "-o",
            str(out_path),
            "--file-format=WAVE",
            "--data-format=LEI16@8000",
            text,
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"say failed for voice={voice} text={text!r}: "
            f"{result.stderr.decode('utf-8', errors='replace')}"
        )
    if not out_path.exists():
        raise RuntimeError(f"say did not write {out_path}")


def _write_wav(path: Path, samples: np.ndarray, sample_rate: int = 8000) -> None:
    """Mono PCM16 WAV writer for the noise samples."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        pcm16 = np.clip(samples * 32768.0, -32768, 32767).astype(np.int16)
        wf.writeframes(pcm16.tobytes())


def _synthesise_noise(kind: str, duration_sec: float, seed: int) -> np.ndarray:
    """Return a float32 array in [-1, 1] approximating each noise
    type's spectral character. Not identical to real field
    recordings; realistic enough for the harness to give a
    first-pass signal on the sweep's shape."""
    rng = np.random.default_rng(seed)
    n = int(8000 * duration_sec)

    if kind == "rain":
        # Broadband white noise, moderate amplitude — proxies the
        # dense hiss of steady rain on a phone mic.
        return rng.normal(0, 0.15, n).astype(np.float32)

    if kind == "wind":
        # Brown noise: cumulative white noise, low-frequency dominated.
        # Proxies the low-freq buffeting of wind on the mic capsule.
        w = rng.normal(0, 1, n)
        brown = np.cumsum(w)
        brown /= np.max(np.abs(brown)) + 1e-9
        return (brown * 0.3).astype(np.float32)

    if kind == "traffic":
        # Amplitude-modulated pink-ish noise: periodic swells as
        # vehicles pass. Base is bandwidth-limited noise; the AM
        # modulator gives the "whoosh-whoosh" character.
        base = rng.normal(0, 1, n)
        # Simple 1-pole LP to knock the highs out.
        alpha = 0.15
        filtered = np.zeros_like(base)
        acc = 0.0
        for i in range(n):
            acc = alpha * base[i] + (1 - alpha) * acc
            filtered[i] = acc
        filtered /= np.max(np.abs(filtered)) + 1e-9
        t = np.arange(n) / 8000
        modulator = 0.5 + 0.5 * np.sin(2 * np.pi * 0.7 * t)  # 0.7 Hz swell
        return (filtered * modulator * 0.4).astype(np.float32)

    if kind == "crowd":
        # Band-limited noise centred around speech frequencies (200-
        # 3000 Hz), lightly modulated. Proxies market babble.
        base = rng.normal(0, 1, n)
        # Two 1-pole filters cascaded as a very rough band-pass.
        hp_acc = 0.0
        lp_acc = 0.0
        hp = np.zeros_like(base)
        for i in range(n):
            hp_acc = 0.9 * hp_acc + base[i] - (base[i - 1] if i > 0 else 0)
            hp[i] = hp_acc
        for i in range(n):
            lp_acc = 0.3 * hp[i] + 0.7 * lp_acc
            hp[i] = lp_acc
        hp /= np.max(np.abs(hp)) + 1e-9
        return (hp * 0.35).astype(np.float32)

    if kind == "sea":
        # Slow-modulated broadband noise: wave crash cadence at
        # ~0.15 Hz. Real sea recordings have transient peaks per
        # crash; the modulator here approximates the envelope.
        base = rng.normal(0, 1, n)
        t = np.arange(n) / 8000
        envelope = 0.3 + 0.7 * np.abs(np.sin(2 * np.pi * 0.15 * t)) ** 3
        return (base * envelope * 0.25).astype(np.float32)

    if kind == "handling":
        # Sparse impulsive noise: proxies phone-handling knocks.
        signal = np.zeros(n, dtype=np.float32)
        n_knocks = int(duration_sec * 3)  # ~3 knocks per second
        knock_positions = rng.integers(0, n, size=n_knocks)
        for pos in knock_positions:
            # Exponentially decaying spike, ~20 ms duration.
            spike_len = min(160, n - pos)
            decay = np.exp(-np.arange(spike_len) / 20)
            polarity = 1.0 if rng.random() > 0.5 else -1.0
            signal[pos : pos + spike_len] += polarity * decay * 0.5
        return signal

    raise ValueError(f"unknown noise kind: {kind!r}")


_NOISE_KINDS: Final[tuple[str, ...]] = (
    "rain",
    "wind",
    "traffic",
    "crowd",
    "sea",
    "handling",
)


def build_corpus(out_dir: Path, noise_duration_sec: float = 3.0) -> Path:
    """Regenerate the whole synthetic corpus under `out_dir`. Wipes
    the target directory first so a fresh run leaves no stale files.
    Returns the manifest path."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "utterances").mkdir(parents=True)
    (out_dir / "noise").mkdir(parents=True)

    # Synthesize utterances across the three en_IN voices.
    manifest_utterances: list[dict[str, str]] = []
    for row in _UTTERANCE_ROWS:
        for voice in _EN_IN_VOICES:
            uid = f"{row.id}_{voice.lower()}"
            wav_rel = f"utterances/{uid}.wav"
            _run_say(row.text, voice, out_dir / wav_rel)
            manifest_utterances.append(
                {
                    "id": uid,
                    "wav": wav_rel,
                    "transcript": row.text,
                    "slot_extractor": row.slot_extractor,
                    "expected_slot": row.expected_slot,
                }
            )

    # Synthesize noise samples.
    manifest_noise: list[dict[str, str]] = []
    for i, kind in enumerate(_NOISE_KINDS):
        samples = _synthesise_noise(kind, noise_duration_sec, seed=i * 17)
        wav_rel = f"noise/{kind}.wav"
        _write_wav(out_dir / wav_rel, samples)
        manifest_noise.append({"type": kind, "wav": wav_rel})

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {"utterances": manifest_utterances, "noise_samples": manifest_noise},
            indent=2,
        ),
        encoding="utf-8",
    )
    return manifest_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build synthetic P3 noise-sweep corpus")
    parser.add_argument("--out-dir", type=Path, default=Path("./data/eval/noise/synthetic"))
    parser.add_argument("--duration-sec", type=float, default=3.0, help="noise sample duration")
    args = parser.parse_args(argv)

    if sys.platform != "darwin":
        print("error: this script uses macOS `say` — run on macOS", file=sys.stderr)
        return 2

    manifest = build_corpus(args.out_dir, noise_duration_sec=args.duration_sec)
    n_utts = len(json.loads(manifest.read_text())["utterances"])
    n_noise = len(json.loads(manifest.read_text())["noise_samples"])
    print(f"corpus written: {manifest}")
    print(f"  utterances: {n_utts} ({len(_UTTERANCE_ROWS)} rows x {len(_EN_IN_VOICES)} voices)")
    print(f"  noise samples: {n_noise}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
