"""Golden audio → slot regression tests (spec §18.2).

Loads every fixture from `data/eval/golden/` and drives it through
the ConversationRunner with the ScriptedTurnInput double, asserting
that the terminal node and slot values match expectations.

Every production call reviewed by ops that surfaced a bug must be
added to data/eval/golden/ BEFORE the bug is fixed (spec §18.2 rule).

Running: `make golden` or `pytest tests/golden/ -v`
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

# Allow the test to locate test doubles from the integration dir.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_GOLDEN_DIR = _REPO_ROOT / "data" / "eval" / "golden"
_DOUBLES_DIR = _REPO_ROOT / "tests" / "integration"

if str(_DOUBLES_DIR) not in sys.path:
    sys.path.insert(0, str(_DOUBLES_DIR))

from _doubles import RecordingAudioSink, ScriptedTurnInput  # type: ignore[import-not-found]

from fg_voice.audio.bank import load_audio_bank
from fg_voice.conversation.graph import build_graph
from fg_voice.conversation.prompt_bank import load_prompt_bank
from fg_voice.conversation.runner import ConversationRunner, RunnerConfig
from fg_voice.conversation.state import CallState, NodeId
from fg_voice.conversation.state_store import InMemoryCallStateStore

# ── Module-scoped audio bank (rendered once) ─────────────────────────

_RENDER_SCRIPT = _REPO_ROOT / "scripts" / "render_audio_bank.py"


@pytest.fixture(scope="module")
def audio_bank(tmp_path_factory):
    render_dir = tmp_path_factory.mktemp("golden_bank")
    spec = importlib.util.spec_from_file_location("_render_bank_golden", _RENDER_SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_render_bank_golden"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    mod.render_bank(render_dir, "en-IN", "silence", "Aditi")
    return load_audio_bank(render_dir, locale="en-IN")


@pytest.fixture(scope="module")
def graph():
    return build_graph()


@pytest.fixture(scope="module")
def prompt_bank():
    return load_prompt_bank()


# ── Fixture loading ──────────────────────────────────────────────────


def _load_fixtures() -> list[dict[str, Any]]:
    fixtures = []
    for path in sorted(_GOLDEN_DIR.glob("*.json")):
        with path.open() as f:
            fixtures.append(json.load(f))
    return fixtures


def _build_input(script: list[dict]) -> ScriptedTurnInput:
    inp = ScriptedTurnInput()
    for step in script:
        kind = step["kind"]
        if kind == "transcript":
            inp.push_transcript(step["text"], confidence=step.get("confidence", 0.9))
        elif kind == "dtmf":
            inp.push_dtmf(step["digit"])
        elif kind == "no_input":
            inp.push_no_input()
        else:
            raise ValueError(f"Unknown script step kind: {kind!r}")
    return inp


def _make_runner(
    inp: ScriptedTurnInput,
    audio_bank,
    graph,
    prompt_bank,
) -> tuple[ConversationRunner, RecordingAudioSink]:
    store = InMemoryCallStateStore()
    sink = RecordingAudioSink()
    state = CallState(call_sid="CA_golden", caller_hash="golden_hash")
    runner = ConversationRunner(
        call_state=state,
        graph=graph,
        prompt_bank=prompt_bank,
        audio_bank=audio_bank,
        turn_input=inp,
        audio_sink=sink,
        state_store=store,
        config=RunnerConfig(stream_sid="MZ_golden", max_call_duration_sec=120),
    )
    return runner, sink


# ── Parameterised test ───────────────────────────────────────────────

_FIXTURES = _load_fixtures()


@pytest.mark.parametrize("fixture", _FIXTURES, ids=[f["id"] for f in _FIXTURES])
@pytest.mark.asyncio
async def test_golden(fixture: dict[str, Any], audio_bank, graph, prompt_bank) -> None:
    """Run one golden fixture and assert terminal node + slot expectations."""
    inp = _build_input(fixture["script"])
    runner, sink = _make_runner(inp, audio_bank, graph, prompt_bank)

    result = await runner.run()

    expect = fixture["expect"]
    expected_terminal = NodeId(expect["terminal_node"])

    assert result.state.current_node == expected_terminal, (
        f"[{fixture['id']}] expected terminal={expected_terminal}, got={result.state.current_node}"
    )

    # Check mandatory slot values.
    for slot_name, expected_value in expect.get("slots", {}).items():
        actual = result.state.slots.get(slot_name)
        actual_value = actual.value if actual is not None else None
        assert actual_value == expected_value, (
            f"[{fixture['id']}] slot {slot_name!r}: "
            f"expected {expected_value!r}, got {actual_value!r}"
        )

    # Check that required prompts were played (safety_tripwire fixture).
    for prompt_fragment in expect.get("must_play_prompts_containing", []):
        played = sink.played_prompts
        assert any(prompt_fragment in p for p in played), (
            f"[{fixture['id']}] expected a prompt containing {prompt_fragment!r} "
            f"to be played, but played: {played}"
        )


# ── Sanity: at least one fixture must be present ─────────────────────


def test_golden_fixtures_not_empty() -> None:
    """CI catches an empty golden dir early."""
    assert _FIXTURES, (
        "data/eval/golden/ is empty — add at least one fixture before running "
        "the golden test suite (spec §18.2)"
    )
