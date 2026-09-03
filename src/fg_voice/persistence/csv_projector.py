"""CSV projector dispatcher (spec §12.3 fast path).

Appends one row to a shared CSV file per outbox event. Column set +
ordering match the schema in `§12.2` exactly — enrichment-only fields
(latitude, longitude, resolved_place, dedupe_group_id, etc.) are
emitted as empty strings until P4/P6 populate them, so the header
never needs a breaking bump.

Concurrency: uses `fcntl.flock` on the file handle for the write
window. That's enough for multi-process on POSIX; the spec's
"single-writer + Redis leader lock + atomic rename" (§12.3) is for
the full-rewrite mode that lands with the enrichment DAG in P6.

Not part of this module:
- S3 sync — that goes into `s3_sync.py` alongside the CSV rewrite mode
- Redis LISTEN/NOTIFY — we already have an in-process broker doing
  the notification; DB pub/sub only earns its keep with multiple
  worker processes"""

from __future__ import annotations

import csv
import io
import os
import sys

# fcntl is POSIX-only. On Windows we use a no-op lock (single-process dev);
# production runs on Linux ECS tasks where fcntl is always available.
if sys.platform == "win32":
    import msvcrt

    class _FcntlShim:
        LOCK_EX = 1
        LOCK_UN = 2

        def flock(self, fd: int, operation: int) -> None:  # noqa: ARG002
            pass  # no-op on Windows dev; production is Linux

    fcntl = _FcntlShim()  # type: ignore[assignment]
else:
    import fcntl
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

from fg_voice.persistence.models import OutboxEntry, Report

# UTF-8 BOM keeps Excel from mangling non-ASCII names — the spec is
# explicit that district officers open these files in Excel.
_BOM: Final[bytes] = "﻿".encode()

SCHEMA_VERSION: Final[int] = 1

# §12.2 column order — do not reorder. Enrichment fields stay in the
# header even before we can fill them.
COLUMNS: Final[tuple[str, ...]] = (
    "report_id",
    "short_ref",
    "received_at_utc",
    "received_at_ist",
    "source",
    "caller_hash",
    "hazard_type",
    "hazard_type_spoken",
    "description_raw",
    "description_clean",
    "location_text",
    "resolved_place",
    "district",
    "state",
    "latitude",
    "longitude",
    "geo_confidence",
    "severity",
    "severity_score",
    "water_depth_cm",
    "confidence_overall",
    "life_safety_flag",
    "call_sid",
    "call_duration_sec",
    "recording_url",
    "dedupe_group_id",
    "enrichment_status",
    "agent_version",
    "schema_version",
)

_IST: Final[ZoneInfo] = ZoneInfo("Asia/Kolkata")

# Nominal severity → score mapping. Real scoring lands in P6; this
# keeps the CSV column populated so downstream dashboards can start
# using it immediately.
_SEVERITY_SCORE: Final[dict[str, int]] = {
    "light": 25,
    "moderate": 60,
    "extreme": 95,
}


@dataclass(slots=True)
class CsvProjectorDispatcher:
    """One instance per relay. `path` is where the live CSV lives —
    typically an EFS mount in prod, a tmp file in tests."""

    path: Path
    agent_version: str = "dev-local"

    async def dispatch(self, entry: OutboxEntry) -> None:
        # Only `report.submitted` writes a CSV row. Enrichment updates
        # (`report.enriched`) are DELIBERATELY skipped here:
        #
        #   1. The at-write-time CSV contract is "one row per report,
        #      appended at call-end". District officers open these
        #      files in Excel and depend on that shape — a second row
        #      per report_id from enrichment would look like a
        #      duplicate incident.
        #   2. The enriched payload carries an updated snapshot, but
        #      updating the existing row in place needs a full-file
        #      rewrite under a leader lock (§12.3). That mode lands
        #      with the S3-sync work in P7; today the CSV is a
        #      submit-time artifact and enrichment updates surface via
        #      SSE + the JSON API instead.
        #
        # Other future report.* events (moderation, dedup-merge) will
        # need to declare their CSV posture explicitly here too.
        if entry.event_type != _EVENT_REPORT_SUBMITTED:
            return
        row = _build_row(entry, agent_version=self.agent_version)
        _append_row(self.path, row)


_EVENT_REPORT_SUBMITTED = "report.submitted"


def _build_row(entry: OutboxEntry, *, agent_version: str) -> dict[str, str]:
    """Fast-path row: everything the dispatcher has is on the outbox
    payload — no DB re-read. Structure MUST match `row_from_report`
    below field-for-field; a divergence is a bug the parity test in
    tests/unit/test_csv_projector.py should catch."""
    payload = dict(entry.payload or {})
    return _row_from_dict(payload, agent_version=agent_version)


def row_from_report(report: Report, *, agent_version: str) -> dict[str, str]:
    """Export-path row: rebuild the same dict shape from a Report ORM
    instance. Used by the `/reports/export.csv` streaming endpoint so
    a batch export renders identical CSV to what the live projector
    wrote at ingestion time — schema drift stays impossible."""
    payload: dict[str, Any] = {
        "report_id": str(report.report_id),
        "short_ref": report.short_ref,
        "source": report.source,
        "caller_hash": report.caller_hash,
        "hazard_type": report.hazard_type,
        "severity": report.severity,
        "water_depth_cm": report.water_depth_cm,
        "description": report.description,
        "description_clean": report.description_clean,
        "location_raw": report.location_raw,
        "flags": report.flags,
        "call_sid": report.call_sid,
        # Report.created_at is a real tz-aware datetime; pass it through
        # so `_parse_utc` doesn't have to re-parse an isoformat string.
        "created_at": report.created_at,
    }
    return _row_from_dict(payload, agent_version=agent_version)


def _row_from_dict(payload: dict[str, Any], *, agent_version: str) -> dict[str, str]:
    """Single source of truth for the CSV row shape. Called by both
    the outbox-payload path (`_build_row`) and the ORM-report path
    (`row_from_report`)."""
    created_utc = _parse_utc(payload.get("created_at")) or datetime.now(UTC)
    return {
        "report_id": str(payload.get("report_id", "")),
        "short_ref": str(payload.get("short_ref", "")),
        "received_at_utc": created_utc.isoformat().replace("+00:00", "Z"),
        "received_at_ist": created_utc.astimezone(_IST).strftime("%Y-%m-%d %H:%M:%S"),
        "source": str(payload.get("source", "voice")),
        "caller_hash": str(payload.get("caller_hash", "")),
        "hazard_type": _s(payload.get("hazard_type")),
        "hazard_type_spoken": "",  # P6 enrichment
        "description_raw": _clean_text(payload.get("description")),
        "description_clean": _clean_text(payload.get("description_clean")),
        "location_text": _s(payload.get("location_raw")),
        "resolved_place": "",  # P4 RAG
        "district": "",  # P4 RAG
        "state": "",  # P4 RAG
        "latitude": "",  # P4 RAG
        "longitude": "",  # P4 RAG
        "geo_confidence": "",  # P4 RAG
        "severity": _s(payload.get("severity")),
        "severity_score": str(_SEVERITY_SCORE.get(str(payload.get("severity", "")), "") or ""),
        "water_depth_cm": _s(payload.get("water_depth_cm")),
        "confidence_overall": "",  # P6 enrichment
        "life_safety_flag": "true" if _has_flag(payload, "life_safety") else "false",
        "call_sid": _s(payload.get("call_sid")),
        "call_duration_sec": "",  # populated from status webhook post-hoc
        "recording_url": "",  # P7 recording toggle
        "dedupe_group_id": "",  # P6 enrichment
        "enrichment_status": "pending",
        "agent_version": agent_version,
        "schema_version": str(SCHEMA_VERSION),
    }


def _s(value: Any) -> str:
    """Empty string for None so the CSV never gets a literal 'None'
    (a real downstream footgun)."""
    if value is None:
        return ""
    return str(value)


def _clean_text(value: Any) -> str:
    """RFC 4180 quoting handles commas + embedded quotes for us; but
    newlines inside the field are legal and Excel-hostile, so escape
    them to a literal `\\n` per §12.2."""
    if value is None:
        return ""
    return str(value).replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def _has_flag(payload: dict[str, Any], flag: str) -> bool:
    flags = payload.get("flags")
    if flags is None:
        return False
    if isinstance(flags, dict):
        return bool(flags.get(flag))
    if isinstance(flags, (list, tuple, set)):
        return flag in flags
    return False


def _parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            # Accept both `2026-08-15T12:34:56+00:00` and `...Z`.
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _append_row(path: Path, row: dict[str, str]) -> None:
    """Take an exclusive lock, write the header if we're the first
    writer to touch a fresh file, append the row, release. `flock` is
    the coarse-but-correct primitive here — the write is O(1) so
    holding it for the whole operation costs nothing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # `os.open` with O_CREAT gives us an fd we can flock BEFORE any
    # header-vs-append decision, so a racing writer can't stuff its
    # own header in between our stat and our write.
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        needs_header = os.fstat(fd).st_size == 0
        os.lseek(fd, 0, os.SEEK_END)

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
        if needs_header:
            writer.writeheader()
        writer.writerow(row)
        payload = buf.getvalue().encode("utf-8")

        # The BOM is written exactly once — before the header line.
        if needs_header:
            os.write(fd, _BOM)
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


__all__ = [
    "BOM",
    "COLUMNS",
    "SCHEMA_VERSION",
    "CsvProjectorDispatcher",
    "row_from_report",
]


# Re-export for the export endpoint so it doesn't have to import the
# private `_BOM` name.
BOM = _BOM
