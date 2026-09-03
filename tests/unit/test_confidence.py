"""Confidence gates from `conversation/confidence.py` (§9.4).

Focused on the pure gate functions; runner-level integration lives in
`tests/integration/test_runner_confidence.py`."""

from __future__ import annotations

from datetime import UTC, datetime

from fg_voice.conversation.confidence import (
    EXTRACTION_MIN_CONFIDENCE,
    STT_MIN_CONFIDENCE,
    GateResult,
    extraction_confidence_gate,
    stt_confidence_gate,
)
from fg_voice.conversation.state import SlotValue

# ─── STT gate ────────────────────────────────────────────────────────


def test_stt_gate_accepts_above_threshold():
    """Above the threshold → pass, no reason."""
    result = stt_confidence_gate(0.9)
    assert result.pass_ is True
    assert result.reason is None


def test_stt_gate_accepts_exactly_at_threshold():
    """Equality is a pass — spec is `< 0.55`, not `<= 0.55`."""
    result = stt_confidence_gate(STT_MIN_CONFIDENCE)
    assert result.pass_ is True


def test_stt_gate_rejects_below_threshold():
    result = stt_confidence_gate(0.4)
    assert result.pass_ is False
    assert result.reason is not None
    assert "stt_confidence" in result.reason


def test_stt_gate_none_passes():
    """When Flux doesn't emit confidence (control frame, metadata),
    we have no basis to reject — pass through."""
    result = stt_confidence_gate(None)
    assert result.pass_ is True


# ─── Extraction gate ─────────────────────────────────────────────────


def _slot(confidence: float) -> SlotValue:
    return SlotValue(
        value="yes",
        confidence=confidence,
        source="asr",
        raw_transcript="yes",
        captured_at=datetime.now(UTC),
    )


def test_extraction_gate_accepts_confident_slot():
    result = extraction_confidence_gate(_slot(0.9))
    assert result.pass_ is True


def test_extraction_gate_accepts_exactly_at_threshold():
    """Equality is a pass — spec is `< 0.60`."""
    result = extraction_confidence_gate(_slot(EXTRACTION_MIN_CONFIDENCE))
    assert result.pass_ is True


def test_extraction_gate_rejects_low_confidence():
    result = extraction_confidence_gate(_slot(0.4))
    assert result.pass_ is False
    assert result.reason is not None
    assert "extraction_confidence" in result.reason


def test_extraction_gate_rejects_none():
    """Extractor returned no value → fail, reason attributes the drop
    to extraction rather than to an upstream empty transcript."""
    result = extraction_confidence_gate(None)
    assert result.pass_ is False
    assert result.reason == "extractor_returned_none"


# ─── GateResult constructors ────────────────────────────────────────


def test_gate_result_ok():
    r = GateResult.ok()
    assert r.pass_ is True and r.reason is None


def test_gate_result_failed():
    r = GateResult.failed("some_reason")
    assert r.pass_ is False and r.reason == "some_reason"
