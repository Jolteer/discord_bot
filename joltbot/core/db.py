"""Database engine and sessions, shared by the bot and the web app.

Both processes open the same SQLite file. SQLite's WAL mode lets one process
write while the other reads. To switch to Postgres later, change DATABASE_URL
(and `pip install asyncpg`); nothing else here needs to change.

TODO: Postgres (``postgresql+asyncpg://…``) is supported by the models/dialect
variants but has not been integration-tested yet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .models import Base


def _sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
    """Runs on every new SQLite connection.

    - foreign_keys: SQLite ignores FOREIGN KEY / ON DELETE CASCADE unless this is on.
    - journal_mode=WAL: readers don't block the writer (bot + dashboard at once).
    - busy_timeout: if the other process is mid-write, wait up to 5s instead of
      failing immediately with "database is locked".
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


class Database:
    """Async SQLAlchemy engine wrapper shared by the bot and dashboard.

    Args:
        url: SQLAlchemy URL (default SQLite via ``DATABASE_URL``).
    """

    def __init__(self, url: str) -> None:
        """Create the engine and the session factory.

        For SQLite this also creates the database's folder if it is missing, and
        arranges for the pragmas above to run on every new connection.
        """
        parsed = make_url(url)
        if parsed.get_backend_name() == "sqlite" and parsed.database not in (None, "", ":memory:"):
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)

        self.engine: AsyncEngine = create_async_engine(url)
        if parsed.get_backend_name() == "sqlite":
            event.listen(self.engine.sync_engine, "connect", _sqlite_pragmas)

        # expire_on_commit=False: objects stay readable after commit, which is
        # what you want in async code (otherwise reading an attribute would try
        # to lazily hit the database again).
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    def session(self) -> AsyncSession:
        """Open a short-lived session for one unit of work.

        Use as ``async with db.session() as session:``. Each session has its own
        connection and transaction, so concurrent bot events never share one.

        Returns:
            A new ``AsyncSession``.
        """
        return self._sessions()

    async def create_all(self) -> None:
        """Create tables from the ORM models.

        Used by tests only. Real deployments use Alembic
        (``alembic upgrade head``) so schema changes are tracked.
        """
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        """Close the engine and release connection-pool resources."""
        await self.engine.dispose()
