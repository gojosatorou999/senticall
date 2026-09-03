"""render_gather_step + gather_redirect_twiml TwiML builders."""

from __future__ import annotations

from xml.etree.ElementTree import fromstring

from fg_voice.telephony.twilio_twiml import gather_redirect_twiml, render_gather_step


def _parse(body: bytes):
    # Strip XML declaration so ElementTree can parse.
    prefix = b'<?xml version="1.0" encoding="UTF-8"?>'
    assert body.startswith(prefix)
    return fromstring(body[len(prefix) :])


def test_gather_redirect_produces_response_with_redirect():
    body = gather_redirect_twiml("/voice/gather/start")
    root = _parse(body)
    assert root.tag == "Response"
    redirect = root.find("Redirect")
    assert redirect is not None
    assert redirect.text == "/voice/gather/start"
    assert redirect.attrib["method"] == "POST"


def test_render_gather_hangup_ends_with_hangup_verb():
    body = render_gather_step(
        say_texts=["Understood. Stay safe."],
        gather_action=None,
        dtmf_map=None,
    )
    root = _parse(body)
    verbs = [child.tag for child in root]
    assert verbs == ["Say", "Hangup"]


def test_render_gather_multi_say_only_last_is_in_gather():
    body = render_gather_step(
        say_texts=["Consent text.", "Are you reporting a hazard?"],
        gather_action="/voice/gather/next",
        dtmf_map=None,
    )
    root = _parse(body)
    # First: Say (leading). Second: Gather (wraps last say). Third: Redirect.
    tags = [c.tag for c in root]
    assert tags[0] == "Say"
    assert tags[1] == "Gather"
    assert tags[2] == "Redirect"

    gather = root.find("Gather")
    assert gather is not None
    inner_say = gather.find("Say")
    assert inner_say is not None
    assert inner_say.text == "Are you reporting a hazard?"


def test_gather_dtmf_map_sets_num_digits_1():
    body = render_gather_step(
        say_texts=["Press 1 for yes, 2 for no."],
        gather_action="/voice/gather/next",
        dtmf_map={"1": "yes", "2": "no"},
    )
    root = _parse(body)
    gather = root.find("Gather")
    assert gather is not None
    assert gather.attrib.get("numDigits") == "1"


def test_gather_speech_only_omits_num_digits():
    body = render_gather_step(
        say_texts=["Tell me what you're seeing."],
        gather_action="/voice/gather/next",
        dtmf_map=None,
    )
    root = _parse(body)
    gather = root.find("Gather")
    assert gather is not None
    assert "numDigits" not in gather.attrib


def test_gather_input_accepts_speech_and_dtmf():
    body = render_gather_step(
        say_texts=["Which is closest?"],
        gather_action="/voice/gather/next",
        dtmf_map={"1": "storm"},
    )
    root = _parse(body)
    gather = root.find("Gather")
    assert gather is not None
    assert gather.attrib["input"] == "speech dtmf"
    assert gather.attrib["action"] == "/voice/gather/next"
    assert gather.attrib["language"] == "en-IN"


def test_no_say_texts_produces_hangup_only():
    body = render_gather_step(say_texts=[], gather_action=None, dtmf_map=None)
    root = _parse(body)
    assert [c.tag for c in root] == ["Hangup"]


def test_gather_redirect_fallback_present_for_no_input():
    """A `<Redirect>` is emitted after the Gather so a no-input timeout
    re-enters our state machine rather than dropping the call."""
    body = render_gather_step(
        say_texts=["Are you reporting a hazard?"],
        gather_action="/voice/gather/next",
        dtmf_map=None,
    )
    root = _parse(body)
    redirect = root.find("Redirect")
    assert redirect is not None
    assert redirect.text == "/voice/gather/next"
