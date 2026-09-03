"""Alembic migration schema parity test.

We keep `Base.metadata.create_all` as the dev/test shortcut (so
`make test` doesn't need docker), which means the two schema
sources of truth — the model layer and the migration script — can
silently drift. This test runs the migration against an empty DB
and asserts the resulting schema is table-for-table equivalent to
what `create_all` would produce.

Both schemas are compared table-by-table via SQLAlchemy's
`inspector` so a column-added-only-to-models regression fails here
before it hits staging."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Column, inspect
from sqlalchemy.engine import Inspector
from sqlalchemy.ext.asyncio import create_async_engine

from fg_voice.persistence import models  # noqa: F401 — register tables
from fg_voice.persistence.db import Base

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def _alembic_config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


async def _apply_migration(url: str) -> dict[str, list[dict[str, object]]]:
    """Run `alembic upgrade head` against `url`, then inspect.

    Alembic's env.py uses `asyncio.run()` internally, which conflicts
    with the pytest-asyncio event loop already running here. `to_thread`
    gives it its own loop in a worker thread — safe because Alembic's
    config is read-only from our side."""
    import asyncio as _asyncio

    cfg = _alembic_config(url)
    await _asyncio.to_thread(command.upgrade, cfg, "head")
    return await _inspect_schema(url)


async def _apply_create_all(url: str) -> dict[str, list[dict[str, object]]]:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    schema = await _inspect_schema(url)
    await engine.dispose()
    return schema


async def _inspect_schema(url: str) -> dict[str, list[dict[str, object]]]:
    """Return `{table_name: [normalized column info, ...]}` for every
    non-alembic table in the DB."""
    engine = create_async_engine(url)
    async with engine.connect() as conn:

        def _fetch(sync_conn: object) -> dict[str, list[dict[str, object]]]:
            insp: Inspector = inspect(sync_conn)
            out: dict[str, list[dict[str, object]]] = {}
            for name in sorted(insp.get_table_names()):
                if name.startswith("alembic_"):
                    continue
                cols = []
                for c in insp.get_columns(name):
                    cols.append(
                        {
                            "name": c["name"],
                            "type": str(c["type"]),
                            "nullable": bool(c["nullable"]),
                        }
                    )
                cols.sort(key=lambda x: x["name"])
                out[name] = cols
            return out

        result = await conn.run_sync(_fetch)
    await engine.dispose()
    return result


@pytest.mark.asyncio
async def test_migration_and_create_all_produce_equivalent_schema(tmp_path, monkeypatch):
    """Two fresh SQLite files. One is `alembic upgrade head`; the
    other is `Base.metadata.create_all`. Their tables + columns +
    nullability must match — otherwise the migration drifted."""
    # Alembic env.py reads Settings.database_url when the caller
    # doesn't override sqlalchemy.url; our Config does override, so
    # no env prep is strictly needed. Set the pepper anyway so any
    # incidental Settings load stays happy.
    monkeypatch.setenv("CALLER_HASH_PEPPER", "test-pepper")

    mig_url = f"sqlite+aiosqlite:///{tmp_path / 'mig.db'}"
    ca_url = f"sqlite+aiosqlite:///{tmp_path / 'ca.db'}"

    migrated = await _apply_migration(mig_url)
    created = await _apply_create_all(ca_url)

    assert set(migrated.keys()) == set(created.keys()), (
        f"table set mismatch: migration={sorted(migrated)} vs create_all={sorted(created)}"
    )
    for tbl in migrated:
        # Normalise both sides: types render slightly differently
        # (JSON vs JSON, VARCHAR(16) vs VARCHAR(16)) so string-compare
        # after lowercasing.
        mig_cols = _norm(migrated[tbl])
        ca_cols = _norm(created[tbl])
        assert mig_cols == ca_cols, (
            f"table {tbl!r} drift:\n  migration={mig_cols}\n  create_all={ca_cols}"
        )


def _norm(cols: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {"name": c["name"], "type": str(c["type"]).lower(), "nullable": c["nullable"]} for c in cols
    ]


# Silence the unused-import warning; `Column` is here so a future
# extension can reach for it without a fresh import.
_ = Column
