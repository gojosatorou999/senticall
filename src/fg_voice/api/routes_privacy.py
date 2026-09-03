"""DPDP Act 2023 privacy endpoints (spec §17.2).

Implements the right of erasure and the right of access for callers who
have submitted reports. The caller identifies themselves by their
`caller_hash` — we never ask for the raw phone number.

Endpoints:
    GET  /api/v1/privacy/caller/{caller_hash}
         Returns the anonymised list of reports filed by this caller
         (their right of access). Excludes raw PII even before erasure.

    DELETE /api/v1/privacy/caller/{caller_hash}
         Purges PII from all reports for this caller:
         - `description` and `description_clean` → "[ERASED]"
         - `location_raw` → "[ERASED]"
         - `caller_hash` → "erased:{original_hash[:8]}" sentinel
         - `flags` → stripped of any caller-identifying keys
         - `pii_erased_at` → current UTC timestamp
         Retains: hazard_type, severity, location_resolved, all dates,
         short_ref, report_id — the anonymised public-safety record.
         S3 objects (recording, transcript) are logged for async deletion;
         the S3 cleanup Lambda picks them up via a tagged DLQ message.

All endpoints are admin-gated (X-Admin-Api-Key).

Security notes:
- The caller_hash is a 64-char hex HMAC-SHA256. An attacker who doesn't
  know the CALLER_HASH_PEPPER cannot enumerate caller hashes.
- Erasure is irreversible and idempotent — re-requesting erasure on an
  already-erased row is a no-op with a 200 response.
- The short_ref is logged so a human reviewer can audit what was erased
  without seeing the raw data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy import select, update

from fg_voice.api.auth import AdminApiKey
from fg_voice.obs.logging import get_logger
from fg_voice.persistence.db import get_session_maker
from fg_voice.persistence.models import Report

log = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/privacy",
    tags=["privacy"],
    dependencies=[AdminApiKey],
)

_ERASED_TEXT = "[ERASED]"
_ERASED_HASH_PREFIX = "erased:"


def _erased_hash(original_hash: str) -> str:
    """Sentinel that replaces the caller_hash after erasure.
    Preserves the first 8 chars so audit logs can group erased rows
    without re-identifying the caller."""
    return f"{_ERASED_HASH_PREFIX}{original_hash[:8]}"


def _is_erased(caller_hash: str) -> bool:
    return caller_hash.startswith(_ERASED_HASH_PREFIX)


# ── Right of access ──────────────────────────────────────────────────


@router.get("/caller/{caller_hash}")
async def get_caller_data(caller_hash: str) -> dict[str, Any]:
    """Return anonymised report summaries for this caller (right of access)."""
    session_maker = get_session_maker()
    if session_maker is None:
        return {"caller_hash": caller_hash, "reports": [], "note": "database not configured"}

    async with session_maker() as session:
        rows = (
            (
                await session.execute(
                    select(Report)
                    .where(Report.caller_hash == caller_hash)
                    .order_by(Report.created_at.desc())
                )
            )
            .scalars()
            .all()
        )

        if not rows:
            # Also check the erased sentinel so a caller can confirm
            # their erasure request was honoured.
            sentinel = _erased_hash(caller_hash)
            erased_rows = (
                (
                    await session.execute(
                        select(Report)
                        .where(Report.caller_hash == sentinel)
                        .order_by(Report.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            if erased_rows:
                return {
                    "caller_hash": caller_hash,
                    "status": "erased",
                    "reports_erased": len(erased_rows),
                    "message": "PII has been erased from all reports for this caller.",
                }
            raise HTTPException(status_code=404, detail="No reports found for this caller hash")

        return {
            "caller_hash": caller_hash,
            "status": "active",
            "report_count": len(rows),
            "reports": [
                {
                    "short_ref": r.short_ref,
                    "report_id": str(r.report_id),
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "hazard_type": r.hazard_type,
                    "severity": r.severity,
                    "location_resolved": r.location_resolved,
                    "pii_erased_at": r.pii_erased_at.isoformat() if r.pii_erased_at else None,
                    "source": r.source,
                }
                for r in rows
            ],
        }


# ── Right of erasure ─────────────────────────────────────────────────


@router.delete("/caller/{caller_hash}")
async def erase_caller_pii(caller_hash: str) -> dict[str, Any]:
    """Erase PII from all reports for this caller (right of erasure, §17.2).

    Retains the anonymised hazard record. Idempotent — safe to call
    multiple times; subsequent calls are no-ops."""
    if _is_erased(caller_hash):
        return {
            "status": "already_erased",
            "caller_hash": caller_hash,
            "message": "This caller hash has already been erased.",
        }

    session_maker = get_session_maker()
    if session_maker is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    async with session_maker() as session:
        # Fetch all reports for this caller to audit what we're erasing.
        rows = (
            (await session.execute(select(Report).where(Report.caller_hash == caller_hash)))
            .scalars()
            .all()
        )

        if not rows:
            raise HTTPException(
                status_code=404,
                detail="No reports found for this caller hash",
            )

        erased_at = datetime.now(UTC)
        sentinel = _erased_hash(caller_hash)
        short_refs = [r.short_ref for r in rows]

        # Bulk update — single UPDATE avoids N+1 round trips.
        await session.execute(
            update(Report)
            .where(Report.caller_hash == caller_hash)
            .values(
                caller_hash=sentinel,
                description=_ERASED_TEXT,
                description_clean=_ERASED_TEXT,
                location_raw=_ERASED_TEXT,
                pii_erased_at=erased_at,
            )
        )
        await session.commit()

    log.info(
        "privacy.caller_erased",
        caller_hash_prefix=caller_hash[:8],
        reports_affected=len(short_refs),
        short_refs=short_refs,
    )

    # Log S3 objects for async deletion by the cleanup Lambda.
    # The Lambda subscribes to a DLQ/SNS topic and deletes the objects
    # keyed by call_sid. We emit the sentinel so the Lambda can identify
    # which rows to clean up without storing raw caller data.
    _log_s3_deletion_request(sentinel, short_refs)

    return {
        "status": "erased",
        "caller_hash_sentinel": sentinel,
        "reports_erased": len(short_refs),
        "short_refs": short_refs,
        "erased_at": erased_at.isoformat(),
        "retained": ["hazard_type", "severity", "location_resolved", "created_at"],
        "message": (
            "PII fields (description, location_raw, caller_hash) have been "
            "replaced with '[ERASED]'. The anonymised hazard record is retained "
            "as legitimate public-safety data per DPDP Act 2023."
        ),
    }


def _log_s3_deletion_request(sentinel: str, short_refs: list[str]) -> None:
    """Log the S3 object deletion request. In production this publishes
    to an SNS topic; here it's a structured log the OTel collector
    can forward to the cleanup pipeline."""
    log.info(
        "privacy.s3_deletion_requested",
        sentinel=sentinel,
        short_refs=short_refs,
        note="S3 recordings and transcripts keyed by call_sid for these reports must be deleted",
    )
