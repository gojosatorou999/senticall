"""Backchannel round-robin over the pre-rendered bank."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from fg_voice.audio.bank import load_audio_bank
from fg_voice.pipeline.backchannel import BackchannelPicker

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RENDER_SCRIPT = _REPO_ROOT / "scripts" / "render_audio_bank.py"


def _load_render_module():
    spec = importlib.util.spec_from_file_location("render_audio_bank_bc", _RENDER_SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["render_audio_bank_bc"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_picker_rotates_variants(tmp_path):
    render = _load_render_module()
    render.render_bank(tmp_path, "en-IN", "silence", "Aditi")
    bank = load_audio_bank(tmp_path, locale="en-IN")

    picker = BackchannelPicker()
    picks = [picker.next(bank) for _ in range(8)]
    assert all(p is not None for p in picks)
    # Every clip in the first N picks should be a distinct id before
    # the cycle repeats (matches variants count).
    ids_in_order = [p.prompt_id for p in picks if p is not None]
    variants = sorted({pid for pid in ids_in_order})
    assert len(variants) >= 2  # bank ships with 4 variants; ≥2 confirms rotation
    # Rotation: index N == index N+len(variants).
    stride = len(variants)
    assert ids_in_order[0] == ids_in_order[stride]


def test_picker_returns_none_when_no_backchannels_in_bank(tmp_path):
    """A bank without any backchannel_* entries should degrade cleanly."""
    from fg_voice.audio.bank import AudioBank

    empty = AudioBank(clips={}, root=tmp_path, locale="en-IN", version="0")
    picker = BackchannelPicker()
    assert picker.next(empty) is None
