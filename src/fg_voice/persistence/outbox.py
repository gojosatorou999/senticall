"""Transactional outbox helper.

Splits the *append* concern from `repo.py` so a future consumer that
needs to write outbox rows for non-report events (e.g. moderator
overrides) can reach for the same helper without pulling the report
write path along.

The dispatcher / relay worker lives elsewhere and is a P5 second-half
concern — this module is just the write side."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from fg_voice.persistence.models import OutboxEntry


class OutboxEventType:
    """Enum-like namespace for event types on the outbox table."""

    REPORT_SUBMITTED = "report.submitted"
    REPORT_ENRICHED = "report.enriched"
    REPORT_MODERATED = "report.moderated"


async def append(
    session: AsyncSession,
    *,
    event_type: str,
    payload: dict[str, Any],
    report_id: UUID | None = None,
) -> OutboxEntry:
    """Insert one outbox row inside `session`. Does NOT commit — the
    caller commits as part of the same transaction as the domain write,
    which is the whole point of the outbox pattern."""
    entry = OutboxEntry(
        report_id=report_id,
        event_type=event_type,
        payload=payload,
    )
    session.add(entry)
    # `flush` gives the row an id inside the transaction so the caller
    # can log it or wire up a downstream reference before commit.
    await session.flush()
    return entry


__all__ = ["OutboxEventType", "append"]
