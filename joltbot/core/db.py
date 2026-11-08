"""Database engine and sessions, shared by the bot and the web app.

Both processes open the same SQLite file. SQLite's WAL mode lets one process
write while the other reads. To switch to Postgres later, change DATABASE_URL
(and `pip install asyncpg`); nothing else here needs to change.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .models import Base


def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
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
    def __init__(self, url: str) -> None:
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
        """Use as ``async with db.session() as session:``. Each session has its own
        connection and transaction, so concurrent bot events never share one."""
        return self._sessions()

    async def create_all(self) -> None:
        """Create tables directly from the models. Used by tests; real deployments
        use Alembic (`alembic upgrade head`) so schema changes are tracked."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()
