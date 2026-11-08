"""Shared test fixtures. Every test gets its own fresh SQLite file."""

from __future__ import annotations

import pytest

from joltbot.core.db import Database
from joltbot.core.guilds import GuildInfo, ensure_guild

GUILD_A = 111111111111111111
GUILD_B = 222222222222222222


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}")
    await database.create_all()
    yield database
    await database.dispose()


@pytest.fixture
async def two_guilds(db):
    """Two servers JoltBot is in, with default (switched-off) settings."""
    async with db.session() as session, session.begin():
        await ensure_guild(session, GuildInfo(GUILD_A, "Server A"))
        await ensure_guild(session, GuildInfo(GUILD_B, "Server B"))
    return GUILD_A, GUILD_B
