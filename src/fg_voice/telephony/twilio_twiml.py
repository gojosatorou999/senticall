"""TwiML response builders.

Every string a caller could hear on a static Twilio path (fallback,
error) lives here — never generated ad-hoc in a route. Prompts spoken
by the agent during a live call are in `conversation/prompts.yaml`
per invariant §2.2; TwiML is only for the connection edge."""

from __future__ import annotations

from urllib.parse import quote
from xml.etree.ElementTree import Element, SubElement, tostring

# Caller-facing voice, in one place so every TwiML path stays consistent.
#
# Tiers available through Twilio <Say>, least to most human-sounding:
#   Polly.Aditi                    standard  - flat, obviously an IVR (free)
#   Polly.Kajal-Neural             neural    - better, still reads as TTS
#   Google.en-IN-Chirp3-HD-Aoede   generative- closest to a real person
#
# The default is the NEURAL tier, not the generative one, and that is a
# latency decision rather than a cost one. Chirp3-HD is generative: Twilio
# runs synthesis before playback can begin, on every turn, and prompts
# carrying a variable (confirm_summary, submitted) can never be cached
# across calls. The caller hears that synthesis as dead air in exactly the
# place they are already waiting - right after they finished answering.
# Neural synthesis starts fast enough not to show.
#
# Override with TWILIO_VOICE to trade the latency back for tone. If a call
# fails with Twilio error 13520 (invalid voice), this string is the cause.
# For zero per-call TTS cost AND lower latency still, pre-render the prompt
# bank (AUDIO_BANK_PATH, scripts/render_audio_bank.py) and serve <Play>.
LANGUAGE: str = "en-IN"


def _voice() -> str:
    """Configured <Say> voice. Read per call rather than bound at import
    so TWILIO_VOICE can be changed without a code edit."""
    from fg_voice.config import get_settings

    return get_settings().twilio_voice


VOICE: str = "Polly.Kajal-Neural"


def connect_stream_twiml(
    wss_url: str,
    report_id: str,
    caller_hash: str,
    locale: str = "en-IN",
    entrypoint: str = "inbound_hotline",
) -> bytes:
    """<Response><Connect><Stream url="…"><Parameter …/></Stream></Connect></Response>

    `report_id` is minted before the stream opens — it is the idempotency
    key for the whole call (§15.1). A Twilio reconnect never creates a
    duplicate report."""
    root = Element("Response")
    connect = SubElement(root, "Connect")
    stream = SubElement(connect, "Stream", {"url": wss_url})
    for name, value in [
        ("report_id", report_id),
        ("caller_hash", caller_hash),
        ("locale", locale),
        ("entrypoint", entrypoint),
    ]:
        SubElement(stream, "Parameter", {"name": name, "value": value})
    return b'<?xml version="1.0" encoding="UTF-8"?>' + bytes(tostring(root, encoding="utf-8"))


def fallback_twiml(sms_link: str | None = None) -> bytes:
    """Static TwiML served on the Twilio number's Fallback URL. If the
    app is down entirely, the caller still gets a coherent response and
    an alternative channel."""
    root = Element("Response")
    say = SubElement(root, "Say", {"voice": _voice(), "language": LANGUAGE})
    if sms_link:
        say.text = (
            "Sorry, our voice line is temporarily unavailable. "
            "We're sending you a link to the FloodGuard web form now. "
            "Please try calling again in a few minutes."
        )
        SubElement(root, "Sms", {}).text = f"Report a coastal hazard: {sms_link}"
    else:
        say.text = (
            "Sorry, our voice line is temporarily unavailable. "
            "Please try calling again in a few minutes, "
            "or use the FloodGuard app."
        )
    SubElement(root, "Hangup")
    return b'<?xml version="1.0" encoding="UTF-8"?>' + bytes(tostring(root, encoding="utf-8"))


def gather_redirect_twiml(gather_start_path: str) -> bytes:
    """Response to `/voice/inbound` when RUNNER_MODE is on: skip the
    Media Streams `<Connect>` and jump straight into the Gather flow.
    A `<Redirect>` is used rather than embedding TwiML inline so the
    gather routes remain the single source of prompt sequencing —
    including consent, which must play before ask_intent."""
    root = Element("Response")
    redirect = SubElement(root, "Redirect", {"method": "POST"})
    redirect.text = gather_start_path
    return b'<?xml version="1.0" encoding="UTF-8"?>' + bytes(tostring(root, encoding="utf-8"))


def render_gather_step(
    say_texts: list[str],
    *,
    gather_action: str | None,
    dtmf_map: dict[str, str] | None,
    voice: str | None = None,
    language: str = LANGUAGE,
    # The safe default, and what every unrecognised question still gets.
    #
    # A numeric value reads as "finalise after N seconds of silence", but on a
    # speech+dtmf Gather Twilio also applies it before speech onset, so it
    # doubles as the "caller hasn't started talking yet" budget. At
    # speechTimeout="1" globally that ended each Gather ~1 s after the prompt
    # finished, giving the caller barely a second to begin talking. Measured:
    # a 4.5 s prompt returned an empty result after 4.7 s despite a 10 s
    # no-input timeout, and callers' "yes" arrived as "yas"/"itne" or nothing.
    #
    # "auto" ends recognition at the first natural pause instead, but it is
    # conservative about what counts as a pause — that is the multi-second
    # dead air callers hear after they have plainly finished, and it is not
    # app latency (the app answers each turn in 30-70 ms).
    #
    # Do not put a global number here. `conversation/eot_patch.py` sets a
    # short numeric value per question, and only where the whole answer is
    # one word ("yes", "moderate", "knee deep") so there is no mid-sentence
    # pause to truncate; open-ended questions keep "auto". Tune it with
    # GATHER_SPEECH_TIMEOUT_SEC, or set that to 0 to disable it entirely.
    speech_timeout: str = "auto",
    timeout_sec: int = 6,
    hints: str | None = None,
    speech_model: str = "phone_call",
) -> bytes:
    """Turn a driver `TurnStepResult` into TwiML.

    - `say_texts` is the ordered list of prompt texts (already rendered
      from the prompt bank). All but the last are plain `<Say>`; the
      last is either wrapped in a `<Gather>` (when `gather_action` is
      set) or followed by a `<Hangup>`.
    - `dtmf_map` sets `numDigits="1"` when non-empty so a single press
      completes the Gather immediately. Speech-only Gathers omit it.
    - No caller-facing text is generated here; texts are pre-rendered
      by the caller of this function via the prompt bank (§2.2)."""
    voice = voice or _voice()
    root = Element("Response")
    if not say_texts:
        SubElement(root, "Hangup")
        return b'<?xml version="1.0" encoding="UTF-8"?>' + bytes(tostring(root, encoding="utf-8"))

    leading = say_texts[:-1]
    trailing = say_texts[-1]

    for text in leading:
        say = SubElement(root, "Say", {"voice": voice, "language": language})
        say.text = text

    if gather_action is None:
        say = SubElement(root, "Say", {"voice": voice, "language": language})
        say.text = trailing
        SubElement(root, "Hangup")
    else:
        gather_attrs: dict[str, str] = {
            "input": "speech dtmf",
            "action": gather_action,
            "method": "POST",
            "language": language,
            "speechTimeout": speech_timeout,
            # Without an explicit `timeout` Twilio applies its own 5 s
            # default, which silently overrides NO_INPUT_TIMEOUT_MS and cuts
            # off any caller who pauses to think. Set it explicitly so the
            # configured no-input budget is the one that actually applies.
            "timeout": str(timeout_sec),
            "actionOnEmptyResult": "true",
            # `phone_call` is Twilio's telephony-tuned model; the default
            # general model does noticeably worse on 8 kHz μ-law.
            "speechModel": speech_model,
        }
        if hints:
            # Biases recognition toward the hazard vocabulary. Without it
            # Twilio has no idea "tsunami" or "nala overflow" are expected
            # words, and returns a plausible-sounding neighbour instead.
            gather_attrs["hints"] = hints
        if dtmf_map:
            gather_attrs["numDigits"] = "1"
        gather = SubElement(root, "Gather", gather_attrs)
        say = SubElement(gather, "Say", {"voice": voice, "language": language})
        say.text = trailing
        # Redirect on no-input timeout so we re-enter the state machine
        # with a timeout event rather than dropping the call.
        redirect = SubElement(root, "Redirect", {"method": "POST"})
        redirect.text = gather_action

    return b'<?xml version="1.0" encoding="UTF-8"?>' + bytes(tostring(root, encoding="utf-8"))


def fatal_hangup_twiml(reason_code: str = "internal_error") -> bytes:
    """Used for hard failures (missing env, DB unreachable at boot). A
    call never leaks a raw stacktrace or a Twilio default error to the
    caller — the caller hears a short apology and hangs up."""
    root = Element("Response")
    say = SubElement(root, "Say", {"voice": _voice(), "language": LANGUAGE})
    say.text = (
        "Sorry, we're unable to take your call right now. Please try again shortly. Stay safe."
    )
    SubElement(root, "Hangup")
    # reason_code is emitted as an XML comment so it shows up in Twilio's
    # inspector without being spoken.
    payload = b'<?xml version="1.0" encoding="UTF-8"?>'
    payload += f"<!-- reason: {quote(reason_code)} -->".encode()
    payload += bytes(tostring(root, encoding="utf-8"))
    return payload
