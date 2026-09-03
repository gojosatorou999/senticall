"""Backfill `reports.source` on legacy rows (spec §13.3).

The `source` column ships with `NOT NULL` + `default='voice'` from
migration `2026081501`, so any row written by this repo's `SqlReportSink`
already carries the correct value. This script exists to normalise rows
that were bulk-imported through other paths (a hand-crafted `INSERT`, a
psql `COPY` from a partner dataset, an older CI fixture that predated
the column) and left `source` either NULL or blank.

Runs idempotent: rows already carrying a non-empty `source` are left
alone. Dry-run by default; pass `--apply` to commit.

Usage:
    uv run python scripts/backfill_source.py           # dry run, prints count
    uv run python scripts/backfill_source.py --apply   # commit
    uv run python scripts/backfill_source.py --apply --value voice --batch 500
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import func, or_, select, update

from fg_voice.persistence.db import session_scope
from fg_voice.persistence.models import Report


async def _run(default_source: str, batch_size: int, apply: bool) -> int:
    async with session_scope() as session:
        needs = or_(Report.source.is_(None), Report.source == "")
        total = await session.scalar(select(func.count()).select_from(Report).where(needs))
        total = int(total or 0)

        if total == 0:
            print("backfill_source: 0 rows need updating; nothing to do.")
            return 0

        print(
            f"backfill_source: {total} row(s) with NULL/empty source; "
            f"will set to {default_source!r}"
            + (" (APPLY)" if apply else " (dry run — pass --apply to commit)")
        )

        if not apply:
            return 0

        # Batch the UPDATE so a very large legacy table doesn't take
        # one giant lock. LIMIT-based UPDATE varies by dialect —
        # Postgres supports `UPDATE ... WHERE report_id IN (SELECT ...
        # LIMIT n FOR UPDATE SKIP LOCKED)`; SQLite (test) supports the
        # plain form. Use the portable subquery pattern.
        updated_total = 0
        while updated_total < total:
            picked_ids = (
                await session.scalars(select(Report.report_id).where(needs).limit(batch_size))
            ).all()
            if not picked_ids:
                break
            result = await session.execute(
                update(Report).where(Report.report_id.in_(picked_ids)).values(source=default_source)
            )
            batch_count = result.rowcount or len(picked_ids)
            updated_total += batch_count
            print(f"  batch: {batch_count} rows (running total {updated_total}/{total})")

        print(f"backfill_source: committed {updated_total} row(s).")
        return updated_total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--value",
        default="voice",
        help="Value to set on backfilled rows (default: 'voice')",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=1000,
        help="Batch size for the UPDATE (default: 1000)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually commit the backfill (otherwise dry-run only).",
    )
    args = parser.parse_args()

    asyncio.run(_run(args.value, args.batch, args.apply))
    return 0


if __name__ == "__main__":
    sys.exit(main())
