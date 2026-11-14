"""Alembic migration runner.

Usage (from the project folder, with the venv active):
    alembic upgrade head                              # apply all migrations
    alembic revision --autogenerate -m "add x table"  # after changing models.py

The database URL comes from DATABASE_URL in .env, unless something (like the
tests) sets sqlalchemy.url explicitly.
"""

import asyncio
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy.engine import Connection

from joltbot.core.config import get_config
from joltbot.core.db import Database
from joltbot.core.models import Base, UTCDateTime

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def render_item(type_: str, obj: Any, autogen_context: Any) -> str | bool:
    """Write our UTCDateTime column type as a plain timestamp in migration files,
    so old migrations never depend on code in models.py."""
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False  # default rendering for everything else


def database_url() -> str:
    """The database to migrate: an explicit ``sqlalchemy.url`` (tests), otherwise ``DATABASE_URL``."""
    return config.get_main_option("sqlalchemy.url") or get_config().database_url


def run_migrations_offline() -> None:
    """`alembic upgrade head --sql`: print the SQL instead of running it."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run the migrations on an open connection."""
    # render_as_batch: SQLite can't ALTER most things in place, so Alembic
    # rebuilds the table instead. Harmless on Postgres.
    context.configure(
        connection=connection, target_metadata=target_metadata, render_as_batch=True, render_item=render_item
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Open the database and run the migrations on it."""
    # Reuse our Database class so SQLite gets the same settings (WAL, foreign keys)
    # and the data/ folder is created if needed.
    db = Database(database_url())
    async with db.engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await db.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
