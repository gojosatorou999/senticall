"""Per-question end-of-turn timing for the recovered gather route.

`api/routes_gather` is sourceless bytecode (see graph_patch.py for why).
It never passes `speech_timeout` to `render_gather_step`, so every
question inherits the module default and no question can be tuned on its
own. This wraps the call to set one per question.

Which way round the values go
-----------------------------
Twilio's <Gather> docs: `speechTimeout="auto"` stops recognising at the
FIRST pause in speech; a number waits that many seconds of silence. So
"auto" is the fast setting and a number is the patient one.

That makes "auto" correct for closed answers — "yes", "moderate", "knee
deep" are complete the moment they land, there is no second clause
coming, and stopping at the first pause is exactly the snappy
turn-taking a caller expects.

It is wrong for the open-ended questions. ask_description and
ask_location invite a sentence, and a caller drawing breath mid-sentence
hands "auto" a pause to stop on. The half-captured answer then fails
extraction and costs a reprompt, so the truncation is heard twice: once
as a wrong transcription, once as another slow turn. Those questions get
a numeric window instead.

An earlier revision of this module had it backwards — numeric on the
closed questions, "auto" on the open ones — which lengthened exactly the
turns that should have been quickest.

Classification is by prompt text, the only signal the compiled caller
gives us. Open-ended anchors are checked first so a prompt matching both
(reprompt_description_2, "Even a few words helps. What are you seeing?",
shares wording with ask_hazard_type) resolves to the patient side.
Anything unrecognised falls through to "auto".

Remove this once routes_gather has real source again and can pick the
timeout per node itself.
"""

from __future__ import annotations

from typing import Final

from fg_voice.config import get_settings
from fg_voice.obs.logging import get_logger

log = get_logger(__name__)

_AUTO: Final[str] = "auto"

# Checked first — a match here always wins. These questions invite a
# sentence, so stopping at the first breath would truncate the answer.
_OPEN_ENDED_ANCHORS: Final[tuple[str, ...]] = (
    # ask_description / reprompt_description_1 / reprompt_description_2
    "tell me what you can see",
    "what's happening there",
    "even a few words",
    # ask_location / reprompt_location_1 / reprompt_location_2
    "where is this",
    "which area or landmark",
    "tell me the nearest town",
)


def speech_timeout_for_prompt(prompt_text: str, open_timeout_sec: int) -> str:
    """`speechTimeout` for the question `prompt_text` asks.

    `"auto"` (stop at the first pause) for closed answers and for
    anything unrecognised; a numeric silence window for the questions
    that invite a sentence. `open_timeout_sec` of 0 forces "auto"
    everywhere."""
    if open_timeout_sec <= 0:
        return _AUTO
    lowered = prompt_text.lower()
    if any(a in lowered for a in _OPEN_ENDED_ANCHORS):
        return str(open_timeout_sec)
    return _AUTO


def apply() -> None:
    """Wrap `routes_gather.render_gather_step` to set `speech_timeout`
    and the configured speech model."""
    from fg_voice.api import routes_gather

    original = routes_gather.render_gather_step

    def render_gather_step_timed(*args, **kwargs):  # type: ignore[no-untyped-def]
        # Tolerate say_texts arriving positionally or by keyword — the
        # caller is compiled code we cannot read.
        say_texts = args[0] if args else kwargs.get("say_texts")
        settings = get_settings()
        # Only touch turns that actually open a Gather. A terminal step
        # has no gather_action and nothing to listen for.
        if say_texts and kwargs.get("gather_action"):
            if "speech_timeout" not in kwargs:
                # The question the caller must answer is the last
                # utterance; earlier ones are lead-ins (consent notice,
                # acknowledgements).
                kwargs["speech_timeout"] = speech_timeout_for_prompt(
                    say_texts[-1], settings.gather_open_speech_timeout_sec
                )
            # The compiled caller never passes a model, so the only way
            # to move off Twilio's v1 "phone_call" default is here.
            if "speech_model" not in kwargs and settings.gather_speech_model:
                kwargs["speech_model"] = settings.gather_speech_model
        return original(*args, **kwargs)

    routes_gather.render_gather_step = render_gather_step_timed


__all__ = ["apply", "speech_timeout_for_prompt"]
