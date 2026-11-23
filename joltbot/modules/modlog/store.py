"""Mod log database operations.

Each function opens its own short session/transaction, so concurrent events
never share a connection. All queries go through SQLAlchemy, which sends
values as bound parameters, never as part of the SQL text.
"""

from __future__ import annotations

from ...core.db import Database
from ...core.models import ModLogEntry
from .events import LogEvent


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
