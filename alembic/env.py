"""Alembic env for fg-voice.

Runs migrations against the async SQLAlchemy engine from
`fg_voice.persistence.db`. The database URL is read from `Settings`
(env-driven) so the same alembic invocation works in dev, staging,
and production. Tests override the URL by pointing `SQLALCHEMY_URL`
at an in-memory SQLite before invoking Alembic programmatically."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Register every model on `Base.metadata` before Alembic snapshots it.
from fg_voice.persistence import models  # noqa: F401 — side-effect import
from fg_voice.persistence.db import Base

config = context.config

# Populate `sqlalchemy.url` from Settings unless the caller overrode it
# (e.g. tests inject via `context.configure(url=...)`).
if not config.get_main_option("sqlalchemy.url"):
    # Deferred import: `Settings` reads env, which tests may still be
    # setting up at this point in a normal migration invocation.
    from fg_voice.config import get_settings

    config.set_main_option("sqlalchemy.url", get_settings().database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout — no engine, no live connection. Useful for
    `alembic upgrade head --sql > migration.sql`."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section) or {}
    connectable = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
