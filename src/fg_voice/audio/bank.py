"""Loader for the pre-rendered audio bank.

Every `prerender: true` prompt in `prompts.yaml` is rendered offline
(scripts/render_audio_bank.py) into 8 kHz μ-law files under
`prompts/audio_bank/<locale>/`, with a `prompts/manifest.json` mapping
`prompt_id → {file, duration_ms, sha1, voice_id, bytes}`.

At runtime this loader reads the manifest once and hands out immutable
`Clip` objects to the TTS router. On a miss (dynamic prompt or bank
version mismatch) the router falls through to the streaming TTS
provider — that path lives in `pipeline/tts_router.py` (P3+)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

MANIFEST_FILENAME: Final[str] = "manifest.json"


class AudioBankError(Exception):
    """Base for boot-time audio bank problems."""


class ManifestMissingError(AudioBankError):
    def __init__(self, path: Path) -> None:
        super().__init__(f"audio bank manifest not found at {path}")


class ManifestCorruptError(AudioBankError):
    """Raised when an entry's on-disk sha1 doesn't match the manifest."""


@dataclass(frozen=True, slots=True)
class Clip:
    """One prerendered prompt. `payload` is 8 kHz μ-law, ready to
    frame + send on the media WebSocket without further processing."""

    prompt_id: str
    payload: bytes
    duration_ms: int
    voice_id: str
    sha1: str


@dataclass(frozen=True, slots=True)
class AudioBank:
    """Immutable per-process audio bank. Look up via `.get(prompt_id)`;
    missing entries return None (the router decides whether to fall
    through to live TTS or refuse to serve)."""

    clips: dict[str, Clip]
    root: Path
    locale: str
    version: str

    def get(self, prompt_id: str) -> Clip | None:
        return self.clips.get(prompt_id)

    def ids(self) -> frozenset[str]:
        return frozenset(self.clips.keys())


def load_audio_bank(
    bank_root: Path,
    locale: str = "en-IN",
    *,
    verify_sha1: bool = True,
) -> AudioBank:
    """Read manifest + clip files. `verify_sha1=False` skips the
    integrity check — useful in tests where the fixture bank is
    generated on the fly and integrity is not the concern."""
    root = bank_root / locale
    manifest_path = root / MANIFEST_FILENAME
    if not manifest_path.exists():
        raise ManifestMissingError(manifest_path)

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = str(raw.get("version", "0"))
    entries = raw.get("entries", {})
    if not isinstance(entries, dict):
        raise AudioBankError("manifest.entries must be a mapping")

    clips: dict[str, Clip] = {}
    for prompt_id, entry in entries.items():
        if not isinstance(entry, dict):
            raise AudioBankError(f"manifest entry for {prompt_id!r} must be a mapping")
        rel_file = entry.get("file")
        if not isinstance(rel_file, str):
            raise AudioBankError(f"manifest entry for {prompt_id!r} missing `file`")
        clip_path = root / rel_file
        if not clip_path.exists():
            raise AudioBankError(f"clip file missing for {prompt_id!r}: {clip_path}")
        payload = clip_path.read_bytes()
        sha1 = str(entry.get("sha1", ""))
        if verify_sha1 and sha1:
            actual = hashlib.sha1(payload, usedforsecurity=False).hexdigest()
            if actual != sha1:
                raise ManifestCorruptError(
                    f"sha1 mismatch for {prompt_id!r}: manifest={sha1} disk={actual}"
                )
        clips[prompt_id] = Clip(
            prompt_id=prompt_id,
            payload=payload,
            duration_ms=int(entry.get("duration_ms", _estimate_duration_ms_from_bytes(payload))),
            voice_id=str(entry.get("voice_id", "")),
            sha1=sha1 or hashlib.sha1(payload, usedforsecurity=False).hexdigest(),
        )
    return AudioBank(clips=clips, root=root, locale=locale, version=version)


def _estimate_duration_ms_from_bytes(payload: bytes) -> int:
    """μ-law @ 8 kHz = 8000 bytes/sec = 8 bytes/ms."""
    return len(payload) // 8


__all__ = [
    "MANIFEST_FILENAME",
    "AudioBank",
    "AudioBankError",
    "Clip",
    "ManifestCorruptError",
    "ManifestMissingError",
    "load_audio_bank",
]
