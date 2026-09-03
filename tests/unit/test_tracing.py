"""Unit tests for obs/tracing.py.

Tests focus on the span creation API — that context managers yield a
Span, that attributes are set correctly, and that configure_tracing is
idempotent. We do NOT test that spans are exported (that's an
integration concern); we verify the no-op path and the API shape."""

from __future__ import annotations

from fg_voice.obs import tracing as _tracing_mod
from fg_voice.obs.tracing import (
    call_span,
    configure_tracing,
    graph_transition_span,
    llm_extract_span,
    rag_resolve_span,
    record_error,
    safety_tripwire_span,
    stt_eot_span,
    tts_span,
    turn_span,
)

# ── configure_tracing idempotency ────────────────────────────────────


def test_configure_tracing_is_idempotent(monkeypatch) -> None:
    """Calling configure_tracing twice must not raise."""
    monkeypatch.setattr(_tracing_mod, "_initialized", False)
    configure_tracing(service_name="test", environment="dev", agent_version="0.0.0")
    configure_tracing(service_name="test", environment="dev", agent_version="0.0.0")


def test_configure_tracing_no_endpoint_stays_noop(monkeypatch) -> None:
    """Without OTEL_EXPORTER_OTLP_ENDPOINT the global tracer is still no-op."""
    monkeypatch.setattr(_tracing_mod, "_initialized", False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    configure_tracing()

    from opentelemetry.trace import NonRecordingSpan

    with call_span("CA_TEST", "00000000-0000-0000-0000-000000000001") as span:
        assert isinstance(span, NonRecordingSpan)


# ── Span context managers yield Span objects ─────────────────────────


def test_call_span_yields_span() -> None:
    from opentelemetry.trace import Span

    with call_span("CA_ABC", "report-123") as span:
        assert isinstance(span, Span)


def test_turn_span_yields_span() -> None:
    from opentelemetry.trace import Span

    with turn_span("ASK_HAZARD_TYPE", attempt=1, turn_index=2) as span:
        assert isinstance(span, Span)


def test_stt_eot_span_yields_span() -> None:
    from opentelemetry.trace import Span

    with stt_eot_span(eot_ms=320, confidence=0.88, eager_used=True) as span:
        assert isinstance(span, Span)


def test_safety_tripwire_span_triggered_false() -> None:
    from opentelemetry.trace import Span

    with safety_tripwire_span(triggered=False) as span:
        assert isinstance(span, Span)


def test_safety_tripwire_span_triggered_true() -> None:
    from opentelemetry.trace import Span

    with safety_tripwire_span(triggered=True) as span:
        assert isinstance(span, Span)


def test_llm_extract_span_all_attrs() -> None:
    from opentelemetry.trace import Span

    with llm_extract_span(
        ttft_ms=180, total_ms=310, tokens_in=600, tokens_out=80, cache_hit=True
    ) as span:
        assert isinstance(span, Span)


def test_llm_extract_span_minimal() -> None:
    from opentelemetry.trace import Span

    with llm_extract_span() as span:
        assert isinstance(span, Span)


def test_rag_resolve_span() -> None:
    from opentelemetry.trace import Span

    with rag_resolve_span(
        index="gazetteer",
        method="rrf",
        top1_score=0.91,
        margin=0.15,
        latency_ms=18,
    ) as span:
        assert isinstance(span, Span)


def test_graph_transition_span() -> None:
    from opentelemetry.trace import Span

    with graph_transition_span(
        "ASK_HAZARD_TYPE", "ASK_DESCRIPTION", guard="hazard_resolved"
    ) as span:
        assert isinstance(span, Span)


def test_tts_span_bank() -> None:
    from opentelemetry.trace import Span

    with tts_span(source="bank", ttfb_ms=5, chars=42) as span:
        assert isinstance(span, Span)


def test_tts_span_stream() -> None:
    from opentelemetry.trace import Span

    with tts_span(source="stream") as span:
        assert isinstance(span, Span)


# ── record_error no-ops on NonRecordingSpan ──────────────────────────


def test_record_error_on_noop_span_does_not_raise() -> None:
    from opentelemetry.trace import NonRecordingSpan
    from opentelemetry.trace.span import INVALID_SPAN_CONTEXT

    span = NonRecordingSpan(INVALID_SPAN_CONTEXT)
    # Must not raise even with a real exception
    record_error(span, ValueError("test error"))


# ── Nested spans (call → turn → llm) ────────────────────────────────


def test_nested_spans_do_not_raise() -> None:
    with (
        call_span("CA_NESTED", "report-456"),
        turn_span("ASK_LOCATION", attempt=0),
        llm_extract_span(ttft_ms=200, cache_hit=False),
    ):
        pass  # just check nothing explodes
