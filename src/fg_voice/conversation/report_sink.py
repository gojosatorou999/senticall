"""ReportSink protocol — what the driver calls when a call reaches SUBMIT.

Defined at the conversation layer, not persistence, because the
import-linter contract forbids `conversation.*` from importing
`persistence.repo`. Concrete implementations live under `persistence/`
and get injected into the driver at construction time.

The sink is intentionally narrow — one `write` method — because that
matches the invariant from spec §2.3: the caller must never hear
"failed", so the write is one atomic step and the caller-facing
`short_ref` is what the sink returns.

Also ships an `InMemoryReportSink` for tests, so the runner /
driver / gather-routes tests don't need a database."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Literal, Protocol
from uuid import UUID

from fg_voice.conversation.state import CallState

ReportStatus = Literal[
    "pending_enrichment",
    "reviewed",
    "published",
    "suppressed",
]

# Alphabet chosen to avoid look-alikes over a noisy phone line — no 0/O
# and no 1/I. See §12 short_ref rationale.
SHORT_REF_ALPHABET: Final[str] = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
SHORT_REF_PREFIX: Final[str] = "FG-"
SHORT_REF_BODY_LEN: Final[int] = 4


def format_short_ref(value: int) -> str:
    """Base-32 (unambiguous alphabet) encode `value` into an FG-XXXX
    short ref. Used both by the SQL sink and the in-memory sink so
    the on-air format stays identical across environments."""
    n = value
    body = []
    for _ in range(SHORT_REF_BODY_LEN):
        body.append(SHORT_REF_ALPHABET[n % len(SHORT_REF_ALPHABET)])
        n //= len(SHORT_REF_ALPHABET)
    return SHORT_REF_PREFIX + "".join(body)


@dataclass(frozen=True, slots=True)
class SubmittedReport:
    """What the sink returns after a successful write. The driver
    stashes `short_ref` on the CallState so the terminal `submitted`
    prompt can render it."""

    report_id: UUID
    short_ref: str
    written_at: datetime


class ReportSinkError(Exception):
    """Base for sink-level failures. `RetryableWriteError` MUST NOT be
    surfaced to the caller — the caller hears success either way,
    and the outbox worker retries."""


class RetryableWriteError(ReportSinkError):
    """Downstream write failed but the outbox row was accepted; the
    caller hears success via the queued short_ref and the relay will
    finish the write in the background."""


class ReportSink(Protocol):
    """Called once per call when the driver reaches SUBMIT."""

    async def write(self, state: CallState) -> SubmittedReport: ...


class InMemoryReportSink:
    """Test double. Not used in the request path. Preserves insertion
    order so tests can assert on the last-written report."""

    def __init__(self) -> None:
        self._reports: list[tuple[CallState, SubmittedReport]] = []
        self._counter = itertools.count(1)

    async def write(self, state: CallState) -> SubmittedReport:
        # Deterministic short_ref from the running counter so tests
        # can assert stable values.
        short_ref = format_short_ref(next(self._counter))
        rep = SubmittedReport(
            report_id=state.report_id,
            short_ref=short_ref,
            written_at=datetime.now(UTC),
        )
        # Snapshot the state so later mutations don't rewrite history.
        self._reports.append((state.model_copy(deep=True), rep))
        return rep

    @property
    def reports(self) -> list[tuple[CallState, SubmittedReport]]:
        return list(self._reports)

    def latest(self) -> tuple[CallState, SubmittedReport] | None:
        if not self._reports:
            return None
        return self._reports[-1]


@dataclass(frozen=True, slots=True)
class NoopReportSink:
    """For code paths that must accept a sink but have no DB — the
    driver's tripwire path uses it during hangup TwiML rendering so
    the last-turn state can still be captured without a real write.
    Not the same as InMemoryReportSink (which records)."""

    _placeholder: str = field(default="", repr=False)

    async def write(self, state: CallState) -> SubmittedReport:
        return SubmittedReport(
            report_id=state.report_id,
            short_ref=format_short_ref(state.report_id.int),
            written_at=datetime.now(UTC),
        )


__all__ = [
    "SHORT_REF_ALPHABET",
    "SHORT_REF_BODY_LEN",
    "SHORT_REF_PREFIX",
    "InMemoryReportSink",
    "NoopReportSink",
    "ReportSink",
    "ReportSinkError",
    "ReportStatus",
    "RetryableWriteError",
    "SubmittedReport",
    "format_short_ref",
]
