"""Flux event parsing + action mapping (§9.2)."""

from __future__ import annotations

import json

import pytest

from fg_voice.pipeline.stt_flux import (
    FluxAction,
    FluxEvent,
    FluxEventKind,
    FluxProtocolError,
    action_for,
    parse_event,
)


def test_parse_start_of_turn():
    event = parse_event({"type": "StartOfTurn"})
    assert event.kind is FluxEventKind.START_OF_TURN
    assert action_for(event) is FluxAction.BARGE_IN


def test_parse_end_of_turn_with_transcript():
    event = parse_event({"type": "EndOfTurn", "transcript": "moderate", "confidence": 0.87})
    assert event.kind is FluxEventKind.END_OF_TURN
    assert event.transcript == "moderate"
    assert event.confidence == pytest.approx(0.87)
    assert action_for(event) is FluxAction.COMMIT_TURN


def test_parse_eager_end_of_turn():
    e = parse_event({"type": "EagerEndOfTurn", "transcript": "waist"})
    assert action_for(e) is FluxAction.SPECULATE


def test_parse_turn_resumed():
    e = parse_event({"type": "TurnResumed"})
    assert action_for(e) is FluxAction.CANCEL_SPECULATION


def test_parse_turn_info_maps_to_draft():
    e = parse_event({"type": "TurnInfo", "transcript": "moderate flood"})
    assert action_for(e) is FluxAction.UPDATE_TRANSCRIPT_DRAFT


def test_parse_metadata_is_noop():
    assert action_for(parse_event({"type": "Metadata"})) is FluxAction.NOOP


def test_bytes_frame_parses():
    frame = json.dumps({"type": "StartOfTurn"}).encode("utf-8")
    assert parse_event(frame).kind is FluxEventKind.START_OF_TURN


def test_string_frame_parses():
    assert parse_event('{"type":"EndOfTurn"}').kind is FluxEventKind.END_OF_TURN


def test_alias_event_field_supported():
    """Some SDKs surface the kind as `event`, not `type`."""
    assert parse_event({"event": "StartOfTurn"}).kind is FluxEventKind.START_OF_TURN


def test_invalid_json_raises():
    with pytest.raises(FluxProtocolError):
        parse_event(b"{not json")


def test_missing_type_raises():
    with pytest.raises(FluxProtocolError, match="missing `type`"):
        parse_event({"transcript": "hi"})


def test_unknown_kind_raises():
    with pytest.raises(FluxProtocolError, match="unknown Flux event"):
        parse_event({"type": "SomethingNew"})


def test_transcript_type_validated():
    with pytest.raises(FluxProtocolError):
        parse_event({"type": "EndOfTurn", "transcript": 42})


def test_confidence_type_validated():
    with pytest.raises(FluxProtocolError):
        parse_event({"type": "EndOfTurn", "confidence": "high"})


def test_action_map_exhaustive():
    """Every FluxEventKind must have an action — this catches a new
    kind being added to the enum without a mapping entry."""
    for kind in FluxEventKind:
        assert action_for(FluxEvent(kind=kind)) in FluxAction
