"""TwiML builders must produce valid XML with the exact structure Twilio
expects. Regressions here manifest as calls that connect but never see a
stream open."""

from __future__ import annotations

from xml.etree.ElementTree import fromstring

from fg_voice.telephony.twilio_twiml import (
    connect_stream_twiml,
    fallback_twiml,
    fatal_hangup_twiml,
)


def test_connect_stream_structure() -> None:
    xml = connect_stream_twiml(
        wss_url="wss://voice.sentinel.in/ws/media",
        report_id="abcd-1234",
        caller_hash="deadbeef",
    )
    assert xml.startswith(b'<?xml version="1.0" encoding="UTF-8"?>')
    root = fromstring(xml[xml.index(b"<Response") :])
    assert root.tag == "Response"
    connect = root.find("Connect")
    assert connect is not None
    stream = connect.find("Stream")
    assert stream is not None
    assert stream.attrib["url"] == "wss://voice.sentinel.in/ws/media"

    params = {p.attrib["name"]: p.attrib["value"] for p in stream.findall("Parameter")}
    assert params["report_id"] == "abcd-1234"
    assert params["caller_hash"] == "deadbeef"
    assert params["locale"] == "en-IN"
    assert params["entrypoint"] == "inbound_hotline"


def test_fallback_no_sms() -> None:
    xml = fallback_twiml()
    root = fromstring(xml[xml.index(b"<Response") :])
    say = root.find("Say")
    assert say is not None
    assert "temporarily unavailable" in (say.text or "")
    assert root.find("Sms") is None
    assert root.find("Hangup") is not None


def test_fallback_with_sms() -> None:
    xml = fallback_twiml("https://app.sentinel.in/report")
    root = fromstring(xml[xml.index(b"<Response") :])
    sms = root.find("Sms")
    assert sms is not None
    assert "app.sentinel.in/report" in (sms.text or "")


def test_fatal_hangup_includes_reason_comment() -> None:
    xml = fatal_hangup_twiml("db_unreachable")
    assert b"reason: db_unreachable" in xml
    root = fromstring(xml[xml.index(b"<Response") :])
    assert root.find("Hangup") is not None
