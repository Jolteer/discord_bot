"""Is the bot process alive and connected to Discord?

The bot has no web server to ask, so it writes a timestamp (a "heartbeat")
to the database once a minute while it is connected. Anything that can read
the database can then tell whether the bot is up: the dashboard's
``/health/bot`` endpoint and overview banner, and Docker's health check
(``python -m joltbot.bot.healthcheck``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from .models import BotMeta
from .types import utcnow

HEARTBEAT_KEY = "bot_heartbeat"
HEARTBEAT_INTERVAL_SECONDS = 60
# Three missed beats. Long enough to ride out a restart or a brief gateway reconnect.
STALE_AFTER = timedelta(seconds=180)


@dataclass(frozen=True)
class BotStatus:
    """When the bot last reported in, and whether that is recent enough to call it online."""

    last_seen: datetime | None
    online: bool


async def write_heartbeat(session: AsyncSession, now: datetime | None = None) -> None:
    """Record that the bot is connected right now.

    Args:
        session: Open async session (caller commits).
        now: Optional clock override for tests.
    """
    await session.merge(BotMeta(key=HEARTBEAT_KEY, value=(now or utcnow()).isoformat()))


def _parse(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    return parsed if parsed is None or parsed.tzinfo is not None else None


async def bot_status(session: AsyncSession, now: datetime | None = None) -> BotStatus:
    """Read the last heartbeat and decide whether the bot counts as online.

    Args:
        session: Open async session.
        now: Optional clock override for tests.

    Returns:
        ``BotStatus``; offline with ``last_seen=None`` if the bot never reported in.
    """
    row = await session.get(BotMeta, HEARTBEAT_KEY)
    last_seen = _parse(row.value if row is not None else None)
    online = last_seen is not None and (now or utcnow()) - last_seen < STALE_AFTER
    return BotStatus(last_seen=last_seen, online=online)
