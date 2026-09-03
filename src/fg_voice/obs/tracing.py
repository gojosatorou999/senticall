"""OpenTelemetry span helpers (§16.1 span tree per turn).

Initialises the OTel SDK once at process start if
`OTEL_EXPORTER_OTLP_ENDPOINT` is set; otherwise falls back to the
no-op tracer so the rest of the code is identical regardless of
whether OTel is wired up.

Span hierarchy per spec §16.1:

    call (call_sid, report_id)
    └── turn[n] (node_id, attempt)
        ├── stt.eot
        ├── safety.tripwire
        ├── llm.extract
        ├── rag.resolve
        ├── graph.transition
        └── tts
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import NonRecordingSpan, Span, StatusCode

_TRACER_NAME = "fg_voice"
_initialized = False


def configure_tracing(
    service_name: str = "fg_voice",
    environment: str = "dev",
    agent_version: str = "dev-local",
) -> None:
    """Idempotent. Call once from main.py lifespan.

    If OTEL_EXPORTER_OTLP_ENDPOINT is unset the global tracer stays
    as a no-op; spans are created but never exported — zero overhead
    on the hot path."""
    global _initialized
    if _initialized:
        return
    _initialized = True

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        resource = Resource.create(
            {
                "service.name": service_name,
                "service.version": agent_version,
                "deployment.environment": environment,
            }
        )
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
    except Exception as exc:
        # OTel setup must never prevent the application from starting.
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "OTel setup failed (continuing without tracing): %s", exc
        )


def get_tracer() -> trace.Tracer:
    return trace.get_tracer(_TRACER_NAME)


# ── Context managers for the call / turn span tree ───────────────────


@contextmanager
def call_span(
    call_sid: str,
    report_id: str,
    caller_hash: str | None = None,
) -> Iterator[Span]:
    """Root span for an entire call. All turn spans nest under this."""
    tracer = get_tracer()
    with tracer.start_as_current_span(
        "call",
        attributes={
            "call_sid": call_sid,
            "report_id": report_id,
            **({"caller_hash": caller_hash} if caller_hash else {}),
        },
    ) as span:
        yield span


@contextmanager
def turn_span(
    node_id: str,
    attempt: int,
    turn_index: int | None = None,
) -> Iterator[Span]:
    """Span for one conversation turn. Must be nested inside call_span."""
    tracer = get_tracer()
    attrs: dict[str, Any] = {"node_id": node_id, "attempt": attempt}
    if turn_index is not None:
        attrs["turn_index"] = turn_index
    with tracer.start_as_current_span("turn", attributes=attrs) as span:
        yield span


@contextmanager
def stt_eot_span(
    eot_ms: int | None = None,
    confidence: float | None = None,
    eager_used: bool = False,
    eager_wasted: bool = False,
) -> Iterator[Span]:
    tracer = get_tracer()
    attrs: dict[str, Any] = {"eager_used": eager_used, "eager_wasted": eager_wasted}
    if eot_ms is not None:
        attrs["eot_ms"] = eot_ms
    if confidence is not None:
        attrs["confidence"] = confidence
    with tracer.start_as_current_span("stt.eot", attributes=attrs) as span:
        yield span


@contextmanager
def safety_tripwire_span(triggered: bool = False) -> Iterator[Span]:
    tracer = get_tracer()
    with tracer.start_as_current_span(
        "safety.tripwire",
        attributes={"triggered": triggered},
    ) as span:
        yield span


@contextmanager
def llm_extract_span(
    ttft_ms: int | None = None,
    total_ms: int | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cache_hit: bool = False,
) -> Iterator[Span]:
    tracer = get_tracer()
    attrs: dict[str, Any] = {"cache_hit": cache_hit}
    for k, v in [
        ("ttft_ms", ttft_ms),
        ("total_ms", total_ms),
        ("tokens_in", tokens_in),
        ("tokens_out", tokens_out),
    ]:
        if v is not None:
            attrs[k] = v
    with tracer.start_as_current_span("llm.extract", attributes=attrs) as span:
        yield span


@contextmanager
def rag_resolve_span(
    index: str = "",
    method: str = "",
    top1_score: float | None = None,
    margin: float | None = None,
    latency_ms: int | None = None,
) -> Iterator[Span]:
    tracer = get_tracer()
    attrs: dict[str, Any] = {"index": index, "method": method}
    for k, v in [
        ("top1_score", top1_score),
        ("margin", margin),
        ("latency_ms", latency_ms),
    ]:
        if v is not None:
            attrs[k] = v
    with tracer.start_as_current_span("rag.resolve", attributes=attrs) as span:
        yield span


@contextmanager
def graph_transition_span(
    from_node: str,
    to_node: str,
    guard: str = "",
) -> Iterator[Span]:
    tracer = get_tracer()
    with tracer.start_as_current_span(
        "graph.transition",
        attributes={"from_node": from_node, "to_node": to_node, "guard": guard},
    ) as span:
        yield span


@contextmanager
def tts_span(
    source: str = "bank",
    ttfb_ms: int | None = None,
    chars: int | None = None,
) -> Iterator[Span]:
    """source: bank | cache | stream"""
    tracer = get_tracer()
    attrs: dict[str, Any] = {"source": source}
    if ttfb_ms is not None:
        attrs["ttfb_ms"] = ttfb_ms
    if chars is not None:
        attrs["chars"] = chars
    with tracer.start_as_current_span("tts", attributes=attrs) as span:
        yield span


def record_error(span: Span, exc: BaseException) -> None:
    """Mark a span as errored with the exception. No-op on non-recording spans."""
    if isinstance(span, NonRecordingSpan):
        return
    span.set_status(StatusCode.ERROR, str(exc))
    span.record_exception(exc)


__all__ = [
    "call_span",
    "configure_tracing",
    "get_tracer",
    "graph_transition_span",
    "llm_extract_span",
    "rag_resolve_span",
    "record_error",
    "safety_tripwire_span",
    "stt_eot_span",
    "tts_span",
    "turn_span",
]
