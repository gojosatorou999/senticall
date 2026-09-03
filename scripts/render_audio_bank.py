"""Render every `prerender: true` prompt in conversation/prompts.yaml
into 8 kHz μ-law and write them to `prompts/audio_bank/{locale}/`,
alongside a `manifest.json`.

Engines
-------
- `--engine silence` (default): writes μ-law silence of an estimated
  duration derived from the text length. This is the P2-ships-without-
  a-TTS-key fallback that lets the manifest, the loader, and the
  media path all work end to end in dev and CI. Not for production.
- `--engine polly`: AWS Polly (`boto3`). Requires AWS creds; produces
  Neural voices. Chosen because Twilio's own `<Say voice="Polly.*">`
  uses the same engine, so pre-rendered clips match the TwiML fallback
  timbre exactly.
- Extension point: add another engine by implementing `Engine` and
  registering it in `_ENGINES`.

Skipped prompts: entries with `prerender: false` (dynamic templates)
are excluded — those go through the streaming TTS provider at runtime
because they contain variables that only exist per call."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

# Wire the src path so the script can import fg_voice without needing
# an editable install first — matches the `uv run` convention used by
# the other scripts.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from fg_voice.audio.codec import (
    TWILIO_SAMPLE_RATE_HZ,
    pcm16_to_ulaw,
    ulaw_silence,
)
from fg_voice.conversation.prompt_bank import Prompt, load_prompt_bank

# Estimating clip duration for the silence engine: normal English
# spoken by a TTS voice averages ~2.5 words/second → 400 ms/word. Round
# up so barge-in gating never underestimates the clip length.
_MS_PER_WORD_ESTIMATE: Final[int] = 420
_MIN_CLIP_MS: Final[int] = 600


class Engine(Protocol):
    name: str

    def synth_pcm16(self, text: str, voice_id: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class SilenceEngine:
    """Dev fallback: emit μ-law silence of an estimated duration. Keeps
    the manifest well-formed without needing a TTS provider key."""

    name: str = "silence"

    def synth_pcm16(self, text: str, voice_id: str) -> bytes:
        # SilenceEngine bypasses PCM: it produces μ-law directly. The
        # renderer detects this via `engine.name == "silence"`.
        del voice_id
        est_ms = max(_MIN_CLIP_MS, len(text.split()) * _MS_PER_WORD_ESTIMATE)
        frames = est_ms // 20
        return ulaw_silence(frames=frames)


@dataclass(frozen=True, slots=True)
class PollyEngine:
    """Amazon Polly Neural. Requires AWS credentials in the environment
    (or a role attached to the runner). Emits 8 kHz PCM."""

    name: str = "polly"

    def synth_pcm16(self, text: str, voice_id: str) -> bytes:
        try:
            import boto3  # local import so `--engine silence` has no boto dep
        except ImportError as exc:
            raise RuntimeError("boto3 required for --engine polly") from exc
        client = boto3.client("polly")
        resp = client.synthesize_speech(
            Text=text,
            VoiceId=voice_id or "Aditi",
            Engine="neural",
            OutputFormat="pcm",
            SampleRate=str(TWILIO_SAMPLE_RATE_HZ),
        )
        return bytes(resp["AudioStream"].read())


_ENGINES: Final[dict[str, type[Engine]]] = {
    "silence": SilenceEngine,
    "polly": PollyEngine,
}


def render_bank(
    output_dir: Path,
    locale: str,
    engine_name: str,
    voice_id: str,
) -> dict[str, object]:
    engine_cls = _ENGINES.get(engine_name)
    if engine_cls is None:
        raise SystemExit(f"unknown engine {engine_name!r}; choose from {sorted(_ENGINES)}")
    engine: Engine = engine_cls()

    bank = load_prompt_bank()
    dst = output_dir / locale
    dst.mkdir(parents=True, exist_ok=True)

    entries: dict[str, dict[str, object]] = {}

    prerender_prompts: list[Prompt] = [p for p in bank.prompts.values() if p.prerender]
    for prompt in prerender_prompts:
        if prompt.variables:
            raise SystemExit(
                f"prompt {prompt.id!r} has variables {sorted(prompt.variables)} but is "
                f"prerender=true — mark it prerender=false or remove the variables"
            )
        ulaw = _synth_to_ulaw(engine, prompt.text, voice_id)
        clip_path = dst / f"{prompt.id}.ulaw"
        clip_path.write_bytes(ulaw)
        entries[prompt.id] = {
            "file": clip_path.name,
            "sha1": hashlib.sha1(ulaw, usedforsecurity=False).hexdigest(),
            "bytes": len(ulaw),
            "duration_ms": (len(ulaw) * 1000) // TWILIO_SAMPLE_RATE_HZ,
            "voice_id": voice_id,
            "engine": engine.name,
        }

    for i, variant in enumerate(bank.backchannels.variants):
        ulaw = _synth_to_ulaw(engine, variant, voice_id)
        pid = f"backchannel_{i}"
        clip_path = dst / f"{pid}.ulaw"
        clip_path.write_bytes(ulaw)
        entries[pid] = {
            "file": clip_path.name,
            "sha1": hashlib.sha1(ulaw, usedforsecurity=False).hexdigest(),
            "bytes": len(ulaw),
            "duration_ms": (len(ulaw) * 1000) // TWILIO_SAMPLE_RATE_HZ,
            "voice_id": voice_id,
            "engine": engine.name,
            "text": variant,
        }

    manifest = {
        "version": _bank_version(entries),
        "locale": locale,
        "voice_id": voice_id,
        "engine": engine.name,
        "entries": entries,
    }
    manifest_path = dst / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def _synth_to_ulaw(engine: Engine, text: str, voice_id: str) -> bytes:
    pcm_or_ulaw = engine.synth_pcm16(text, voice_id)
    if engine.name == "silence":
        return pcm_or_ulaw
    return pcm16_to_ulaw(pcm_or_ulaw)


def _bank_version(entries: dict[str, dict[str, object]]) -> str:
    concatenated = "|".join(f"{k}:{entries[k]['sha1']}" for k in sorted(entries))
    return hashlib.sha1(concatenated.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


def main() -> int:
    parser = argparse.ArgumentParser(description="Render the prerendered TTS audio bank.")
    parser.add_argument("--locale", default="en-IN")
    parser.add_argument(
        "--engine",
        default="silence",
        choices=sorted(_ENGINES),
        help="TTS backend. `silence` is the dev/CI fallback (see module docstring).",
    )
    parser.add_argument("--voice-id", default="Aditi")
    parser.add_argument("--output-dir", default="./prompts/audio_bank")
    args = parser.parse_args()

    manifest = render_bank(
        Path(args.output_dir).resolve(),
        args.locale,
        args.engine,
        args.voice_id,
    )
    print(
        f"render_audio_bank: engine={args.engine} locale={args.locale} "
        f"version={manifest['version']} entries={len(manifest['entries'])}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
