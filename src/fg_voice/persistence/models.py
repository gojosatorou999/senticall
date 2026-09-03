"""SQLAlchemy models for the reports feed + transactional outbox.

Enrichment fields (resolved coordinates, dedupe group id, priority
score, moderation status) are P6 territory and land as nullable
columns so P5 can ship without knowing their exact shape yet.

Kept driver-agnostic — no PostGIS types in the model layer, so the
same models can spin up on SQLite in tests. When P4/P6 add real
`geometry(Point, 4326)` columns, they get their own migration."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fg_voice.persistence.db import Base


def _now_utc() -> datetime:
    return datetime.now(UTC)


class Report(Base):
    """One report per submitted call. `source` is 'voice' for anything
    coming through this repo; the app/web/whatsapp paths (out of scope
    for the voice agent) share the same table via the unified feed."""

    __tablename__ = "reports"

    report_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    short_ref: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="voice")
    call_sid: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    caller_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)

    # Structured slots collected by the driver.
    hazard_type: Mapped[str | None] = mapped_column(String(32))
    severity: Mapped[str | None] = mapped_column(String(16))
    water_depth_cm: Mapped[int | None] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(String(2000))
    # PII-scrubbed twin of `description`. Populated synchronously by
    # SqlReportSink at write time (regex-based). Consumed by every
    # outbound artifact (CSV row, alert body, SSE frame) — raw
    # `description` stays on the row for admin review + P6 deep NER.
    description_clean: Mapped[str | None] = mapped_column(String(2000))
    location_raw: Mapped[str | None] = mapped_column(String(500))

    # Enrichment output (populated by P6). Nullable on creation so the
    # P5 write is a single INSERT with no side-tables required.
    location_resolved: Mapped[str | None] = mapped_column(String(500))
    dedupe_group_id: Mapped[str | None] = mapped_column(String(64))
    # P6 score.py: heuristic in [0, 100]. Nullable pre-enrichment;
    # populated on the enrichment flow's persist step.
    confidence_score: Mapped[int | None] = mapped_column(Integer)
    priority_score: Mapped[int | None] = mapped_column(Integer)
    # Set by the enrichment flow's persist step. NULL until the flow
    # runs; a re-run overwrites with the new run's timestamp so ops
    # can see whether the row was recently re-enriched.
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    flags: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="pending_enrichment")

    # QA sampling — spec §11.11 "5% QA sampling queue". At write time,
    # SqlReportSink flips `sampled_for_qa=true` for ~QA_SAMPLING_RATE
    # of reports (deterministic hash of report_id so Twilio retries
    # never disagree with the first pass). Reviewed entries carry the
    # timestamp + free-text notes; unreviewed samples are what the
    # QA queue endpoint returns.
    sampled_for_qa: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    qa_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    qa_notes: Mapped[str | None] = mapped_column(String(1000))

    # DPDP Act 2023 — set when a caller exercises the right of erasure.
    # PII fields (description, location_raw, caller_hash) are zeroed;
    # the anonymised hazard record (hazard_type, severity, location_resolved)
    # is retained as legitimate public-safety data. See §17.2.
    pii_erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, onupdate=_now_utc
    )

    outbox_entries: Mapped[list[OutboxEntry]] = relationship(
        back_populates="report",
        cascade="all, delete-orphan",
        lazy="noload",
    )

    __table_args__ = (
        UniqueConstraint("short_ref", name="uq_reports_short_ref"),
        Index("ix_reports_created_at", "created_at"),
    )


class OutboxEntry(Base):
    """Transactional outbox row (§2.3). Written in the same session as
    the Report, so either both land or neither does — the caller-facing
    "submitted" is only ever true after the row is durable.

    A background relay (P5 second half) polls unclaimed rows and
    dispatches them (SSE fan-out, CSV projection, alerts). P5 minimum
    just captures the payload — no relay yet."""

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("reports.report_id", ondelete="CASCADE"),
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(1000))

    report: Mapped[Report | None] = relationship(back_populates="outbox_entries")

    __table_args__ = (Index("ix_outbox_pending", "dispatched_at"),)


__all__ = ["OutboxEntry", "Report"]
