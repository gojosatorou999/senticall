"""Load test — N concurrent synthetic calls (spec §18.6).

Verifies:
- No submission failures across the entire run
- p95 runner-layer latency stays within budget under load
- Tasks-per-call ceiling estimated for the autoscaling target (§14.3)

Tiers:
    10   concurrent calls  — fast smoke test (runs in unit CI)
    50   concurrent calls  — integration smoke
    200  concurrent calls  — pre-staging load gate (marked slow)
    500  concurrent calls  — reserved for staging environment (requires real infra)

The 10-call tier runs by default in unit CI. Higher tiers require
`pytest -m slow` or the dedicated `make load` target.

Note: these tests drive the ConversationRunner only (no real Deepgram /
TTS / LLM). They measure the code path that the application owns. Real
concurrency under live telephony load is tested in the staging
environment after a `make deploy ENV=staging`.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
from pathlib import Path
from typing import NamedTuple

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
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

_RENDER_SCRIPT = _REPO_ROOT / "scripts" / "render_audio_bank.py"

# ── Latency SLOs for the runner-layer (no network) ───────────────────
_P50_BUDGET_MS = 50.0
_P95_BUDGET_MS = 100.0


@pytest.fixture(scope="module")
def audio_bank(tmp_path_factory):
    render_dir = tmp_path_factory.mktemp("load_bank")
    spec = importlib.util.spec_from_file_location("_render_bank_load", _RENDER_SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_render_bank_load"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    mod.render_bank(render_dir, "en-IN", "silence", "Aditi")
    return load_audio_bank(render_dir, locale="en-IN")


class _CallResult(NamedTuple):
    submitted: bool
    turn_latencies_ms: list[float]
    terminal_node: str


def _happy_path_script() -> ScriptedTurnInput:
    inp = ScriptedTurnInput()
    inp.push_transcript("yes")
    inp.push_transcript("oil spill on the beach")
    inp.push_transcript("Oil spill at the fishing harbour")
    inp.push_transcript("Kakinada harbour")
    inp.push_transcript("yes")  # confirm_location_low_conf (confidence=0.6 always)
    inp.push_transcript("moderate")
    inp.push_transcript("yes")  # confirm_summary
    return inp


async def _run_one_call(audio_bank) -> _CallResult:
    inp = _happy_path_script()
    graph = build_graph()
    prompt_bank = load_prompt_bank()
    store = InMemoryCallStateStore()
    sink = RecordingAudioSink()
    state = CallState(call_sid="CA_load", caller_hash="load_hash")

    turn_latencies: list[float] = []

    runner = ConversationRunner(
        call_state=state,
        graph=graph,
        prompt_bank=prompt_bank,
        audio_bank=audio_bank,
        turn_input=inp,
        audio_sink=sink,
        state_store=store,
        config=RunnerConfig(stream_sid="MZ_load", max_call_duration_sec=60),
    )

    # Instrument _collect_turn for per-turn timing.
    _orig = runner._collect_turn  # type: ignore[attr-defined]

    async def _timed(*args, **kwargs):  # type: ignore[no-untyped-def]
        t0 = time.perf_counter()
        result = await _orig(*args, **kwargs)
        turn_latencies.append((time.perf_counter() - t0) * 1000)
        return result

    runner._collect_turn = _timed  # type: ignore[method-assign]

    result = await runner.run()
    submitted = result.state.current_node == NodeId.SUBMITTED
    return _CallResult(
        submitted=submitted,
        turn_latencies_ms=turn_latencies,
        terminal_node=str(result.state.current_node),
    )


async def _load_run(n: int, audio_bank) -> tuple[int, float, float]:
    """Run n concurrent calls, return (failures, p50_ms, p95_ms)."""
    results = await asyncio.gather(
        *[_run_one_call(audio_bank) for _ in range(n)],
        return_exceptions=True,
    )

    failures = sum(
        1
        for r in results
        if isinstance(r, Exception) or (isinstance(r, _CallResult) and not r.submitted)
    )
    all_latencies: list[float] = []
    for r in results:
        if isinstance(r, _CallResult):
            all_latencies.extend(r.turn_latencies_ms)

    def _pct(data: list[float], p: float) -> float:
        if not data:
            return 0.0
        s = sorted(data)
        k = (len(s) - 1) * p / 100
        lo, hi = int(k), min(int(k) + 1, len(s) - 1)
        return s[lo] + (s[hi] - s[lo]) * (k - lo)

    p50 = _pct(all_latencies, 50)
    p95 = _pct(all_latencies, 95)
    return failures, p50, p95


# ── Test tiers ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_10_concurrent_calls_no_failures(audio_bank) -> None:
    """10 concurrent calls — fast smoke test (runs in CI)."""
    failures, p50, p95 = await _load_run(10, audio_bank)
    assert failures == 0, f"Submission failures at 10 concurrent calls: {failures}"
    assert p50 <= _P50_BUDGET_MS, f"p50 {p50:.1f} ms > budget {_P50_BUDGET_MS} ms"
    assert p95 <= _P95_BUDGET_MS, f"p95 {p95:.1f} ms > budget {_P95_BUDGET_MS} ms"


@pytest.mark.asyncio
@pytest.mark.slow
async def test_50_concurrent_calls_no_failures(audio_bank) -> None:
    """50 concurrent calls — integration smoke (requires `pytest -m slow`)."""
    failures, p50, p95 = await _load_run(50, audio_bank)
    assert failures == 0, f"Submission failures at 50 concurrent calls: {failures}"
    assert p50 <= _P50_BUDGET_MS, f"p50 {p50:.1f} ms > budget {_P50_BUDGET_MS} ms"
    assert p95 <= _P95_BUDGET_MS, f"p95 {p95:.1f} ms > budget {_P95_BUDGET_MS} ms"


@pytest.mark.asyncio
@pytest.mark.slow
async def test_200_concurrent_calls_no_failures(audio_bank) -> None:
    """200 concurrent calls — pre-staging load gate."""
    failures, p50, p95 = await _load_run(200, audio_bank)
    assert failures == 0, f"Submission failures at 200 concurrent calls: {failures}"
    assert p50 <= _P50_BUDGET_MS
    assert p95 <= _P95_BUDGET_MS


@pytest.mark.asyncio
@pytest.mark.slow
async def test_zero_submission_failures_invariant(audio_bank) -> None:
    """Zero submission failures is a hard requirement (§16.2 fg_voice_submission_failures_total)."""
    failures, _, _ = await _load_run(50, audio_bank)
    assert failures == 0, (
        "Submission failures detected — this counter must be 0 in production. "
        "Every call must reach SUBMITTED (outbox durable write) or a valid exit node."
    )
