"""Per-question speech hints for the recovered gather route.

`api/routes_gather` is sourceless bytecode (see graph_patch.py for why).
It builds one static hint list via `_speech_hints()` and passes it to
`render_gather_step` for every question, which measurably hurts
recognition: the handful of words that can answer the current question
compete against ~146 unrelated ones.

Live-call evidence, all with the full list active:
    ASK_DEPTH     "knee deep" -> "neila Devi"   (rejected)
    ASK_DEPTH     (retry)     -> "enemmy"       (rejected)
    ASK_SEVERITY  "light"     -> "right"        (rejected)

Each rejection burns one of MAX_ATTEMPTS_PER_SLOT, so a caller can run
out of attempts and get hung up on mid-report.

`render_gather_step` receives the prompt text that is about to be spoken,
which is enough to identify the question, so this wraps that call and
swaps in a narrowed hint list. Nothing about the conversation graph, the
extractors, or the TwiML structure changes — only the `hints` attribute.

Remove this once routes_gather has real source again and can select
hints per node itself.
"""

from __future__ import annotations

from fg_voice.obs.logging import get_logger
from fg_voice.rag.keyterms import build_keyterms_for_prompt

log = get_logger(__name__)

# Twilio accepts up to 500 comma-separated entries; we stay far below.
_MAX_HINTS = 300


def apply() -> None:
    """Wrap `routes_gather.render_gather_step` to narrow `hints` per question."""
    from fg_voice.api import routes_gather

    original = routes_gather.render_gather_step

    def render_gather_step_scoped(*args, **kwargs):  # type: ignore[no-untyped-def]
        # Tolerate say_texts arriving positionally or by keyword — the
        # caller is compiled code we cannot read.
        say_texts = args[0] if args else kwargs.get("say_texts")
        # Only narrow when hints were already going to be sent; never
        # introduce them where the caller deliberately passed none.
        if say_texts and kwargs.get("hints"):
            # The question the caller must answer is the last utterance;
            # earlier ones are lead-ins (consent notice, acknowledgements).
            terms = build_keyterms_for_prompt(say_texts[-1])[:_MAX_HINTS]
            kwargs["hints"] = ",".join(terms)
        return original(*args, **kwargs)

    routes_gather.render_gather_step = render_gather_step_scoped


__all__ = ["apply"]
