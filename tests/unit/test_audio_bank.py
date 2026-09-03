"""Audio bank loader + render_audio_bank silence engine round-trip."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from fg_voice.audio.bank import (
    AudioBankError,
    ManifestCorruptError,
    ManifestMissingError,
    load_audio_bank,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RENDER_SCRIPT = _REPO_ROOT / "scripts" / "render_audio_bank.py"


def _load_render_module():
    """Import scripts/render_audio_bank.py by path (it isn't a package
    member). Kept as a helper so this file doesn't fight the import
    system if the scripts/ layout ever changes."""
    spec = importlib.util.spec_from_file_location("render_audio_bank", _RENDER_SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["render_audio_bank"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_missing_manifest_raises(tmp_path):
    with pytest.raises(ManifestMissingError):
        load_audio_bank(tmp_path, locale="en-IN")


def test_render_silence_engine_produces_loadable_bank(tmp_path):
    render = _load_render_module()
    manifest = render.render_bank(tmp_path, "en-IN", "silence", "Aditi")

    assert "entries" in manifest
    assert manifest["engine"] == "silence"
    # At least the ask_intent + consent + backchannels should be present.
    entries = manifest["entries"]
    assert isinstance(entries, dict)
    assert "consent_notice" in entries
    assert "ask_intent" in entries
    assert any(k.startswith("backchannel_") for k in entries)

    # And the loader can read them back without errors.
    bank = load_audio_bank(tmp_path, locale="en-IN")
    assert bank.get("consent_notice") is not None
    assert bank.get("ask_intent") is not None
    assert bank.get("nonexistent_prompt") is None


def test_render_silence_engine_skips_dynamic_prompts(tmp_path):
    """`prerender: false` prompts (with variables) must not appear."""
    render = _load_render_module()
    manifest = render.render_bank(tmp_path, "en-IN", "silence", "Aditi")
    assert "confirm_summary" not in manifest["entries"]
    assert "submitted" not in manifest["entries"]


def test_manifest_sha1_mismatch_raises(tmp_path):
    render = _load_render_module()
    render.render_bank(tmp_path, "en-IN", "silence", "Aditi")

    # Corrupt one clip on disk without updating its sha1.
    locale_dir = tmp_path / "en-IN"
    clip = locale_dir / "consent_notice.ulaw"
    clip.write_bytes(b"\x00" * 320)  # different content

    with pytest.raises(ManifestCorruptError):
        load_audio_bank(tmp_path, locale="en-IN")


def test_load_with_verify_sha1_false_tolerates_corruption(tmp_path):
    render = _load_render_module()
    render.render_bank(tmp_path, "en-IN", "silence", "Aditi")
    (tmp_path / "en-IN" / "consent_notice.ulaw").write_bytes(b"\x00" * 320)
    bank = load_audio_bank(tmp_path, locale="en-IN", verify_sha1=False)
    assert bank.get("consent_notice") is not None


def test_clip_duration_estimation_reasonable(tmp_path):
    render = _load_render_module()
    render.render_bank(tmp_path, "en-IN", "silence", "Aditi")
    bank = load_audio_bank(tmp_path, locale="en-IN")
    consent = bank.get("consent_notice")
    assert consent is not None
    # Consent is ~12 words → ≥ ~5 s worst case, but the estimate should
    # at least be over the min-clip floor of 600 ms.
    assert consent.duration_ms >= 600


def test_manifest_missing_file_raises(tmp_path):
    """Simulate a manifest referring to a clip file that isn't there."""
    root = tmp_path / "en-IN"
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "version": "0",
                "entries": {
                    "ghost": {
                        "file": "ghost.ulaw",
                        "sha1": hashlib.sha1(b"", usedforsecurity=False).hexdigest(),
                        "duration_ms": 100,
                        "voice_id": "Aditi",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(AudioBankError):
        load_audio_bank(tmp_path, locale="en-IN")
