"""Mod log database operations.

Each function opens its own short session/transaction, so concurrent events
never share a connection. All queries go through SQLAlchemy, which sends
values as bound parameters, never as part of the SQL text.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from sqlalchemy import delete, select

from ...core.db import Database, rows_changed
from ...core.models import ModLogEntry
from .events import LogEvent

# Rows deleted per transaction by the cleanup. SQLite has one writer at a time, so a
# long delete would make the bot's own inserts and dashboard saves wait their turn.
DELETE_BATCH = 500


async def insert(db: Database, event: LogEvent) -> int:
    """Save one event to the log history.

    Args:
        db: Shared database.
        event: The event to save.

    Returns:
        The new entry's id.
    """
    async with db.session() as session, session.begin():
        entry = ModLogEntry(
            guild_id=event.guild_id,
            event_type=event.event,
            category=event.category,
            actor_id=event.actor_id,
            target_id=event.target_id,
            channel_id=event.channel_id,
            details=event.details(),
            reason=event.reason,
            created_at=event.created_at,
        )
        session.add(entry)
        await session.flush()
        return entry.id


async def guilds_with_entries(db: Database) -> list[int]:
    """Every server that has at least one saved entry, whether its logging is on or off.

    Args:
        db: Shared database.

    Returns:
        Guild IDs, in ascending order.
    """
    async with db.session() as session:
        result = await session.execute(select(ModLogEntry.guild_id).distinct().order_by(ModLogEntry.guild_id))
        return list(result.scalars())


async def delete_older_than(db: Database, guild_id: int, cutoff: datetime, *, batch: int = DELETE_BATCH) -> int:
    """Delete one server's entries saved before ``cutoff``, a batch at a time.

    Each batch is its own short transaction, and other tasks get a turn in
    between, so a large first cleanup never holds the database for long.
    An entry saved exactly at ``cutoff`` is kept.

    Args:
        db: Shared database.
        guild_id: The server whose entries are deleted. No other server's are touched.
        cutoff: Entries with ``created_at`` before this are deleted.
        batch: Rows per transaction.

    Returns:
        How many entries were deleted.
    """
    expired = (
        select(ModLogEntry.id)
        .where(ModLogEntry.guild_id == guild_id, ModLogEntry.created_at < cutoff)
        .order_by(ModLogEntry.id)
        .limit(batch)
        .scalar_subquery()
    )
    total = 0
    while True:
        async with db.session() as session, session.begin():
            result = await session.execute(delete(ModLogEntry).where(ModLogEntry.id.in_(expired)))
        deleted = rows_changed(result)
        total += deleted
        if deleted < batch:
            return total
        await asyncio.sleep(0)
