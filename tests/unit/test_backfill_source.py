"""backfill_source script — sets source='voice' on legacy rows only.

The script is a one-shot ops utility (spec §13.3). Tests pin the two
properties that matter:

1. Rows with a non-empty `source` are NEVER touched (idempotent + safe
   to re-run against a mixed table).
2. Rows with empty `source` get the requested value after `--apply`;
   a dry run leaves the DB unchanged.

Note: this repo's `Report.source` is `NOT NULL` from day one, so the
NULL branch in the script itself is defensive (a rollback migration
that briefly relaxed the constraint would leave rows the script must
still catch). Tests here exercise the empty-string case, which is
reachable via a direct UPDATE against the current schema.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

# scripts/ is a sibling of tests/ — mirror the test_geo_eval.py trick so
# `from scripts.backfill_source import ...` resolves without a package
# marker inside scripts/.
_REPO_ROOT_FOR_SCRIPTS = Path(__file__).parent.parent.parent
if str(_REPO_ROOT_FOR_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT_FOR_SCRIPTS))

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fg_voice.persistence.db import Base, override_engine, reset_engine
from fg_voice.persistence.models import Report


@pytest.fixture
async def _db():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", pool_pre_ping=False)
    override_engine(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(eng, expire_on_commit=False)
    try:
        yield sm
    finally:
        await eng.dispose()
        reset_engine()


async def _seed(sm: async_sessionmaker, source_value: str) -> str:
    """Insert one report row with the given source, returning its
    short_ref for lookup. The `NOT NULL` constraint means we can't
    plant a literal NULL here; empty-string legacy rows are reachable
    via a follow-up UPDATE past the ORM default."""
    short_ref = f"FG-{uuid4().hex[:4].upper()}"
    async with sm() as session, session.begin():
        row = Report(
            short_ref=short_ref,
            source=source_value if source_value else "voice",  # placeholder
            call_sid=f"CA_{short_ref}",
            caller_hash="h",
            status="pending_enrichment",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(row)
    if source_value != "voice":
        async with sm() as session, session.begin():
            await session.execute(
                update(Report).where(Report.short_ref == short_ref).values(source=source_value)
            )
    return short_ref


async def _get_source(sm: async_sessionmaker, short_ref: str) -> str | None:
    async with sm() as session:
        row = await session.scalar(select(Report).where(Report.short_ref == short_ref))
        return None if row is None else row.source


# ─── The behaviours ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_touches_nothing(_db):
    from scripts.backfill_source import _run

    empty_ref = await _seed(_db, "")
    empty2_ref = await _seed(_db, "")
    fine_ref = await _seed(_db, "voice")

    updated = await _run(default_source="voice", batch_size=10, apply=False)

    assert updated == 0
    assert await _get_source(_db, empty_ref) == ""  # unchanged
    assert await _get_source(_db, empty2_ref) == ""
    assert await _get_source(_db, fine_ref) == "voice"


@pytest.mark.asyncio
async def test_apply_backfills_only_empty(_db):
    from scripts.backfill_source import _run

    empty_ref = await _seed(_db, "")
    empty2_ref = await _seed(_db, "")
    fine_ref = await _seed(_db, "voice")
    other_ref = await _seed(_db, "app")  # some other legit source — leave alone

    updated = await _run(default_source="voice", batch_size=10, apply=True)

    assert updated == 2
    assert await _get_source(_db, empty_ref) == "voice"
    assert await _get_source(_db, empty2_ref) == "voice"
    assert await _get_source(_db, fine_ref) == "voice"
    assert await _get_source(_db, other_ref) == "app"


@pytest.mark.asyncio
async def test_apply_is_idempotent(_db):
    from scripts.backfill_source import _run

    await _seed(_db, "")
    await _seed(_db, "")

    first = await _run(default_source="voice", batch_size=10, apply=True)
    second = await _run(default_source="voice", batch_size=10, apply=True)

    assert first == 2
    assert second == 0  # nothing left to backfill


@pytest.mark.asyncio
async def test_batching_covers_all_rows(_db):
    from scripts.backfill_source import _run

    refs = [await _seed(_db, "") for _ in range(7)]

    updated = await _run(default_source="voice", batch_size=3, apply=True)

    assert updated == 7
    for ref in refs:
        assert await _get_source(_db, ref) == "voice"
