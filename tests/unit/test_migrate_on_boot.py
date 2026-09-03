"""MIGRATE_ON_BOOT lifespan behaviour.

Uses SQLite file URLs (not `:memory:`) because Alembic's engine and
the test's inspector engine are separate connections; the in-memory
DB would vanish between them."""

from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from fg_voice.persistence.db import reset_engine


async def _tables_in(url: str) -> set[str]:
    """Introspect the DB and return the non-alembic table names."""
    eng = create_async_engine(url)
    async with eng.connect() as conn:

        def _fetch(sync_conn: object) -> set[str]:
            insp = inspect(sync_conn)
            return {name for name in insp.get_table_names() if not name.startswith("alembic_")}

        names = await conn.run_sync(_fetch)
    await eng.dispose()
    return names


@pytest.mark.asyncio
async def test_flag_on_creates_all_tables(tmp_path, dev_env: None, monkeypatch):
    db_file = tmp_path / "boot.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("MIGRATE_ON_BOOT", "true")
    monkeypatch.setenv("RELAY_ENABLED", "false")
    monkeypatch.setenv("CALLER_HASH_PEPPER", "test-pepper")

    reset_engine()  # make sure lifespan builds a fresh engine bound to our URL

    from fg_voice.main import app, lifespan

    # Empty file before boot.
    assert not db_file.exists() or await _tables_in(f"sqlite+aiosqlite:///{db_file}") == set()

    async with lifespan(app):
        pass  # boot ran; migrations should have applied

    tables = await _tables_in(f"sqlite+aiosqlite:///{db_file}")
    assert "reports" in tables
    assert "outbox" in tables
    reset_engine()


@pytest.mark.asyncio
async def test_flag_off_leaves_schema_untouched(tmp_path, dev_env: None, monkeypatch):
    db_file = tmp_path / "no_boot.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("MIGRATE_ON_BOOT", "false")
    monkeypatch.setenv("RELAY_ENABLED", "false")
    monkeypatch.setenv("CALLER_HASH_PEPPER", "test-pepper")

    reset_engine()

    from fg_voice.main import app, lifespan

    async with lifespan(app):
        pass

    # No migrations ran, so no tables should have been created.
    tables = await _tables_in(f"sqlite+aiosqlite:///{db_file}")
    assert tables == set()
    reset_engine()


@pytest.mark.asyncio
async def test_flag_on_is_idempotent(tmp_path, dev_env: None, monkeypatch):
    """Booting twice with the flag on shouldn't fail — Alembic
    detects the schema is already at head and no-ops."""
    db_file = tmp_path / "twice.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("MIGRATE_ON_BOOT", "true")
    monkeypatch.setenv("RELAY_ENABLED", "false")
    monkeypatch.setenv("CALLER_HASH_PEPPER", "test-pepper")

    reset_engine()

    from fg_voice.main import app, lifespan

    async with lifespan(app):
        pass

    reset_engine()  # simulate fresh process start

    # Second boot should not raise.
    async with lifespan(app):
        pass

    tables = await _tables_in(f"sqlite+aiosqlite:///{db_file}")
    assert "reports" in tables
    reset_engine()


@pytest.mark.asyncio
async def test_run_migrations_at_boot_returns_head_revision(tmp_path, dev_env: None, monkeypatch):
    """The helper returns the resulting revision id so the lifespan
    log can surface it for operators."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'rev.db'}")
    monkeypatch.setenv("CALLER_HASH_PEPPER", "test-pepper")
    reset_engine()

    from fg_voice.persistence.db import run_migrations_at_boot

    revision = await run_migrations_at_boot()
    # Current head migration id — bump when a new revision is added.
    assert revision == "2026081505"
    reset_engine()
