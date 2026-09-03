"""Value objects passed between enrichment tasks.

Deliberately dumb dataclasses — no behaviour, no ORM, no session.
Each task takes a `EnrichmentContext`, mutates it (via a new field or
by returning a new instance) and hands it to the next task. `persist`
is the one task that flushes the accumulated state back to the DB.

Why not just carry the ORM Report row through? Because the flow may
run on a different worker than the one that wrote the row (P7), and
we do NOT want a live SQLAlchemy session held across LLM calls that
can take seconds. Load once at the top (`assemble`), pass an immutable
snapshot through the middle, load again at the bottom (`persist`) to
reconcile."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(slots=True)
class ReportSnapshot:
    """Immutable view of the `reports` row at the top of the flow.
    Enrichment tasks read from this; they never mutate it (the mutable
    accumulator is `EnrichmentResult`)."""

    report_id: UUID
    short_ref: str
    source: str
    call_sid: str
    caller_hash: str
    hazard_type: str | None
    severity: str | None
    water_depth_cm: int | None
    description: str | None
    description_clean: str | None
    location_raw: str | None
    flags: dict[str, Any]
    created_at: datetime


@dataclass(slots=True)
class EnrichmentResult:
    """Accumulator — each task fills in the fields it produces. Only
    fields explicitly set here are written back by `persist`; the rest
    stay whatever they were on the row (including NULL).

    Rationale: enrichment is *additive* — a task that can't run (e.g.
    geocoder unavailable) leaves its field unset and the row keeps
    whatever it had, rather than getting nulled out."""

    location_resolved: str | None = None
    dedupe_group_id: str | None = None
    confidence_score: int | None = None
    priority_score: int | None = None
    # If the deep-extract task reruns extraction and revises a slot,
    # it goes here. persist reconciles: only overwrite the raw column
    # if the new value has higher confidence than what's stored.
    revised_slots: dict[str, Any] = field(default_factory=dict)
    # Notes accumulated by any task — audit trail for the QA console.
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class EnrichmentContext:
    """Passed task-to-task through the flow."""

    snapshot: ReportSnapshot
    result: EnrichmentResult = field(default_factory=EnrichmentResult)


__all__ = ["EnrichmentContext", "EnrichmentResult", "ReportSnapshot"]
