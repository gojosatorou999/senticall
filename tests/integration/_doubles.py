"""Deterministic test doubles for the runner integration tests.

- `ScriptedTurnInput` walks a pre-scripted list of events (or a "None"
  entry to simulate a no-input timeout). Once the script is exhausted
  it raises `Hangup` — the runner catches this at the outer loop.
- `RecordingAudioSink` records every clip payload sha1 and every
  control message; nothing is actually played.

Underscore prefix keeps pytest from collecting this file as tests."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from fg_voice.audio.bank import Clip
from fg_voice.conversation.runner import Hangup, InputEvent
from fg_voice.pipeline.stt_flux import FluxEvent, FluxEventKind


@dataclass(slots=True)
class ScriptedTurnInput:
    """Emit a scripted sequence of events; None entries model a
    no-input timeout on the next `next_event()` call."""

    events: deque[InputEvent | None] = field(default_factory=deque)

    @classmethod
    def from_script(cls, script: list[InputEvent | None]) -> ScriptedTurnInput:
        return cls(events=deque(script))

    def push_transcript(self, transcript: str, confidence: float = 0.9) -> None:
        """Convenience: append a StartOfTurn + EndOfTurn pair for a
        real caller utterance."""
        self.events.append(
            InputEvent(kind="flux", flux_event=FluxEvent(kind=FluxEventKind.START_OF_TURN))
        )
        self.events.append(
            InputEvent(
                kind="flux",
                flux_event=FluxEvent(
                    kind=FluxEventKind.END_OF_TURN,
                    transcript=transcript,
                    confidence=confidence,
                ),
            )
        )

    def push_dtmf(self, digit: str) -> None:
        self.events.append(InputEvent(kind="dtmf", dtmf_digit=digit))

    def push_no_input(self) -> None:
        self.events.append(None)

    async def next_event(self, timeout_ms: int) -> InputEvent | None:
        del timeout_ms
        if not self.events:
            # Script exhausted — treat as caller hangup.
            raise Hangup
        return self.events.popleft()


@dataclass(slots=True)
class RecordingAudioSink:
    """Record every play + control message. Nothing actually plays."""

    played_prompts: list[str] = field(default_factory=list)
    played_sha1s: list[str] = field(default_factory=list)
    control_messages: list[str] = field(default_factory=list)

    async def play_clip(self, clip: Clip) -> None:
        self.played_prompts.append(clip.prompt_id)
        self.played_sha1s.append(clip.sha1)

    async def send_clear(self, message: str) -> None:
        self.control_messages.append(message)
