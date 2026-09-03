"""Async SQLAlchemy engine + session factory.

Kept tiny: one engine, one sessionmaker, one `Base`. Consumed by the
repos in this package and by the migration harness. Nothing else
touches the engine directly."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from fg_voice.config import get_settings


class Base(DeclarativeBase):
    """Single declarative base for every table in the process. Tables
    defined in `models.py` inherit from this so `Base.metadata` sees
    them all — needed both for Alembic autogenerate and for the
    test-time `create_all` call."""


_engine: AsyncEngine | None = None
_session_maker: async_sessionmaker[AsyncSession] | None = None

# Resolved once at import so `run_migrations_at_boot` doesn't do a
# blocking filesystem call inside an async function (ASYNC240). The
# app container's WORKDIR isn't necessarily where alembic.ini lives —
# anchor at the repo root.
_ALEMBIC_INI: Path = Path(__file__).resolve().parents[3] / "alembic.ini"
_ALEMBIC_DIR: Path = Path(__file__).resolve().parents[3] / "alembic"


def _make_engine(database_url: str) -> AsyncEngine:
    """`echo=False` in prod; toggle via LOG_SQL if you need to trace."""
    # `pool_pre_ping` catches stale connections after a Postgres
    # failover — cheap and worth it under RDS Multi-AZ.
    return create_async_engine(database_url, pool_pre_ping=True)


def get_engine() -> AsyncEngine:
    """Process-wide async engine. Lazily built on first call so the
    settings are resolved once, after env is fully loaded."""
    global _engine, _session_maker
    if _engine is None:
        _engine = _make_engine(get_settings().database_url)
        _session_maker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_session_maker() -> async_sessionmaker[AsyncSession]:
    get_engine()  # side-effect: build the maker
    assert _session_maker is not None
    return _session_maker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transaction-scoped session. Commits on clean exit, rolls back
    on exception. Consumers await this in a `async with`."""
    maker = get_session_maker()
    async with maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def override_engine(engine: AsyncEngine) -> None:
    """Test hook — inject an in-memory SQLite engine so tests don't
    reach for the real Postgres. Never called from application code."""
    global _engine, _session_maker
    _engine = engine
    _session_maker = async_sessionmaker(engine, expire_on_commit=False)


def reset_engine() -> None:
    """Test teardown — drop the cached engine so the next test can
    inject its own."""
    global _engine, _session_maker
    _engine = None
    _session_maker = None


async def create_all(engine: AsyncEngine | None = None) -> None:
    """Idempotently create every table declared on `Base`. Used by
    dev-mode boot and by tests; production goes through Alembic
    migrations (P5 second half)."""
    target = engine or get_engine()

    def _create(sync_conn: Any) -> None:
        Base.metadata.create_all(sync_conn)

    async with target.begin() as conn:
        await conn.run_sync(_create)


async def run_migrations_at_boot() -> str:
    """Apply pending Alembic migrations, returning the resulting head
    revision id. Runs `alembic upgrade head` in `asyncio.to_thread` —
    Alembic's env.py uses its own internal `asyncio.run()` so calling
    it from a running event loop would `RuntimeError`.

    Idempotent: applying an already-applied revision is a no-op in
    Alembic. Safe to call every boot.

    Multi-node deploy caveat: N pods calling this at the same time
    race on the version table. Modern Alembic serialises via a table
    lock so at worst you get one waiter per pod, but the safer
    pattern for prod is to run migrations from a separate init
    container / deploy step. See MIGRATE_ON_BOOT docstring in
    config.py."""
    import asyncio

    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)

    await asyncio.to_thread(command.upgrade, cfg, "head")

    # Report which revision we landed on so ops can correlate a
    # startup log with a code deploy.
    script_dir = ScriptDirectory.from_config(cfg)
    head = script_dir.get_current_head() or "unknown"
    return head


__all__ = [
    "AsyncSession",
    "Base",
    "create_all",
    "get_engine",
    "get_session_maker",
    "override_engine",
    "reset_engine",
    "run_migrations_at_boot",
    "session_scope",
]
