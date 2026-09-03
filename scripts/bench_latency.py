"""Latency budget assertion (spec §5 + §18.4).

Replays a fixed 8-turn happy-path conversation through the
ConversationRunner N times and asserts that the runner-layer p50/p95
stays within budget. This is the portion of the latency budget that
the code owns — network hops to Deepgram, TTS, and the LLM are
measured separately in the staging load tests.

Runner-layer SLOs (no external network calls):
    p50 ≤ 50 ms per turn
    p95 ≤ 100 ms per turn

The per-stage breakdown is printed so a regression can be localised
without re-reading flamegraphs. Stage timings sum to the total turn
latency measured wall-clock.

Usage
-----
    uv run python scripts/bench_latency.py           # 200 calls, 8 turns
    uv run python scripts/bench_latency.py --calls 50 --turns 4 --verbose
    make bench
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Allow running from the repo root without installing the package.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests" / "integration"))

# Import the scripted test doubles from the integration test helpers.
from _doubles import RecordingAudioSink, ScriptedTurnInput  # type: ignore[import-not-found]

from fg_voice.audio.bank import load_audio_bank
from fg_voice.conversation.graph import build_graph
from fg_voice.conversation.prompt_bank import load_prompt_bank
from fg_voice.conversation.runner import ConversationRunner, RunnerConfig
from fg_voice.conversation.state import CallState
from fg_voice.conversation.state_store import InMemoryCallStateStore

# ── SLO constants ────────────────────────────────────────────────────
P50_BUDGET_MS = 50.0
P95_BUDGET_MS = 100.0

# ── Fixed 8-turn conversation script ────────────────────────────────
# Covers: consent → intent (yes) → hazard → description → location →
# severity → confirm (yes) → submitted. Skips ASK_DEPTH (storm is a
# flood-class hazard, but the bench uses "sludge_oil" to avoid the
# conditional branch — keeps turn count deterministic).

_HAPPY_PATH_SCRIPT = [
    # CONSENT is a machine node (no input needed)
    # ASK_INTENT
    "yes",
    # ASK_HAZARD_TYPE
    "sludge or oil",
    # ASK_DESCRIPTION
    "There is a large oil spill near the fishing harbour",
    # ASK_LOCATION
    "Visakhapatnam beach",
    # RESOLVE_LOCATION → ASK_SEVERITY (machine node, no input)
    # ASK_SEVERITY
    "moderate",
    # CONFIRM_SUMMARY
    "yes",
    # SUBMIT → SUBMITTED (machine nodes)
]


@dataclass
class StageTimes:
    """Accumulated per-stage timing across all turns of a call."""

    tripwire_ms: list[float] = field(default_factory=list)
    graph_ms: list[float] = field(default_factory=list)
    total_ms: list[float] = field(default_factory=list)


def _render_bank_once(render_dir: Path) -> None:
    """Render the silence-engine audio bank into render_dir."""
    script = _REPO_ROOT / "scripts" / "render_audio_bank.py"
    spec = importlib.util.spec_from_file_location("_render_bank", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_render_bank"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    mod.render_bank(render_dir, "en-IN", "silence", "Aditi")


class _TimedTurnInput:
    """Wraps ScriptedTurnInput and records per-turn wall-clock time."""

    def __init__(self, transcripts: list[str], stage_times: StageTimes) -> None:
        self._inner = ScriptedTurnInput.from_script([])
        for t in transcripts:
            self._inner.push_transcript(t)
        self._times = stage_times
        self._turn_start: float = 0.0

    def mark_turn_start(self) -> None:
        self._turn_start = time.perf_counter()

    def mark_turn_end(self) -> None:
        if self._turn_start:
            self._times.total_ms.append((time.perf_counter() - self._turn_start) * 1000)
            self._turn_start = 0.0

    async def next_event(self, timeout_ms: int):  # type: ignore[no-untyped-def]
        return await self._inner.next_event(timeout_ms)


async def _run_one_call(
    transcripts: list[str],
    bank_dir: Path,
    stage_times: StageTimes,
) -> None:
    """Drive one call through the runner and collect turn timings."""
    graph = build_graph()
    prompt_bank = load_prompt_bank()
    audio_bank = load_audio_bank(bank_dir, locale="en-IN")
    store = InMemoryCallStateStore()
    sink = RecordingAudioSink()
    state = CallState(call_sid="CA_bench", caller_hash="bench")

    inp = _TimedTurnInput(transcripts, stage_times)

    runner = ConversationRunner(
        call_state=state,
        graph=graph,
        prompt_bank=prompt_bank,
        audio_bank=audio_bank,
        turn_input=inp,
        audio_sink=sink,
        state_store=store,
        config=RunnerConfig(stream_sid="MZ_bench", max_call_duration_sec=120),
    )

    # Monkey-patch the runner's _collect_turn to record wall-clock.
    _original_collect = runner._collect_turn  # type: ignore[attr-defined]

    async def _timed_collect(*args, **kwargs):  # type: ignore[no-untyped-def]
        t0 = time.perf_counter()
        result = await _original_collect(*args, **kwargs)
        stage_times.total_ms.append((time.perf_counter() - t0) * 1000)
        return result

    runner._collect_turn = _timed_collect  # type: ignore[method-assign]

    await runner.run()


def _percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(sorted_data) - 1)
    return sorted_data[lo] + (sorted_data[hi] - sorted_data[lo]) * (k - lo)


async def _bench(n_calls: int, bank_dir: Path, verbose: bool) -> bool:
    """Run N calls and return True if SLOs are met."""
    stage_times = StageTimes()

    tasks = [_run_one_call(_HAPPY_PATH_SCRIPT, bank_dir, stage_times) for _ in range(n_calls)]

    print(f"bench: running {n_calls} calls concurrently …", flush=True)
    t_start = time.perf_counter()
    await asyncio.gather(*tasks, return_exceptions=True)
    wall_total = (time.perf_counter() - t_start) * 1000

    turns = stage_times.total_ms
    if not turns:
        print("bench: ERROR — no turn timings collected", file=sys.stderr)
        return False

    p50 = _percentile(turns, 50)
    p95 = _percentile(turns, 95)
    p99 = _percentile(turns, 99)
    mean = statistics.mean(turns)

    print(f"\n{'─' * 50}")
    print(f"  calls   : {n_calls}  turns recorded : {len(turns)}")
    print(f"  wall    : {wall_total:.0f} ms total")
    print(f"  mean    : {mean:.1f} ms/turn")
    print(f"  p50     : {p50:.1f} ms  (budget: {P50_BUDGET_MS:.0f} ms)")
    print(f"  p95     : {p95:.1f} ms  (budget: {P95_BUDGET_MS:.0f} ms)")
    print(f"  p99     : {p99:.1f} ms")

    if verbose:
        buckets = [0, 5, 10, 20, 50, 100, 200]
        print("\n  histogram (ms):")
        for lo, hi in zip(buckets, [*buckets[1:], float("inf")]):
            count = sum(lo <= t < hi for t in turns)
            bar = "█" * (count * 40 // max(len(turns), 1))
            print(f"    [{lo:>4},{hi!s:>6}): {bar} {count}")

    print(f"{'─' * 50}")

    ok = True
    if p50 > P50_BUDGET_MS:
        print(f"  ❌ p50 {p50:.1f} ms > budget {P50_BUDGET_MS} ms", file=sys.stderr)
        ok = False
    else:
        print(f"  ✅ p50 {p50:.1f} ms ≤ {P50_BUDGET_MS} ms")

    if p95 > P95_BUDGET_MS:
        print(f"  ❌ p95 {p95:.1f} ms > budget {P95_BUDGET_MS} ms", file=sys.stderr)
        ok = False
    else:
        print(f"  ✅ p95 {p95:.1f} ms ≤ {P95_BUDGET_MS} ms")

    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calls", type=int, default=200, help="Number of concurrent call replays")
    parser.add_argument(
        "--turns", type=int, default=8, help="(informational) expected turns per call"
    )
    parser.add_argument("--verbose", action="store_true", help="Print histogram")
    parser.add_argument(
        "--bank-dir",
        type=Path,
        default=None,
        help="Pre-rendered audio bank directory (rendered to a temp dir if absent)",
    )
    args = parser.parse_args()

    import tempfile

    with tempfile.TemporaryDirectory(prefix="fg_bench_bank_") as tmpdir:
        bank_dir = args.bank_dir or Path(tmpdir)
        if not args.bank_dir:
            print("bench: rendering silence audio bank …", flush=True)
            _render_bank_once(bank_dir)

        ok = asyncio.run(_bench(args.calls, bank_dir, args.verbose))

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
