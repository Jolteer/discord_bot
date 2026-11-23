"""Deletes mod log entries older than each server's retention period.

Entries can hold the text of deleted messages, so they must not be kept longer
than the server chose (``ModLogSettings.retention_days``). This applies whether
logging is on or off: switching logging off does not keep old entries forever.
The bot runs it at startup and then once a day (``cog.py``).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ...core.db import Database
from ...core.guild_settings import load_module
from ...core.types import utcnow
from . import store
from .constants import MODULE

log = logging.getLogger(__name__)


async def purge_expired(
    db: Database, *, now: datetime | None = None, batch: int = store.DELETE_BATCH
) -> dict[int, int]:
    """Delete every server's entries that are older than that server's retention period.

    A server with no saved settings, or settings that no longer validate, gets
    the default period (``load_module`` returns defaults then). A failure for
    one server is logged and the other servers are still cleaned.

    Args:
        db: Shared database.
        now: Clock override for tests.
        batch: Rows per transaction (see ``store.delete_older_than``).

    Returns:
        Entries deleted per server, only for servers where something was deleted.
    """
    now = now or utcnow()
    deleted: dict[int, int] = {}
    for guild_id in await store.guilds_with_entries(db):
        try:
            async with db.session() as session:
                state = await load_module(session, guild_id, MODULE)
            cutoff = now - timedelta(days=state.config.retention_days)
            if count := await store.delete_older_than(db, guild_id, cutoff, batch=batch):
                deleted[guild_id] = count
        except Exception:
            log.exception("Guild %s: couldn't delete old mod log entries", guild_id)
    return deleted
