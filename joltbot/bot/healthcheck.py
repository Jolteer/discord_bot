"""Docker health check for the bot: python -m joltbot.bot.healthcheck

Exits 0 if the bot wrote a heartbeat in the last few minutes, 1 otherwise.
The bot has no port to probe, so this reads the heartbeat from the database
(see ``core/health.py``). Docker shows the result in ``docker compose ps``.
"""

from __future__ import annotations

import asyncio
import sys

from ..core.config import get_config
from ..core.db import Database
from ..core.health import BotStatus, bot_status


async def read_status(database_url: str) -> BotStatus:
    """Open the database just long enough to read the bot's status.

    Args:
        database_url: SQLAlchemy database URL.

    Returns:
        The bot's ``BotStatus``.
    """
    db = Database(database_url)
    try:
        async with db.session() as session:
            return await bot_status(session)
    finally:
        await db.dispose()


def main() -> None:
    """CLI entry. Prints one line (shown by ``docker inspect``) and sets the exit code."""
    try:
        status = asyncio.run(read_status(get_config().database_url))
    except Exception as e:
        print(f"unhealthy: couldn't read the database ({e.__class__.__name__})")
        sys.exit(1)
    if status.online:
        print("healthy")
        sys.exit(0)
    seen = f"last heartbeat {status.last_seen:%Y-%m-%d %H:%M:%S} UTC" if status.last_seen else "no heartbeat yet"
    print(f"unhealthy: not connected to Discord ({seen})")
    sys.exit(1)


if __name__ == "__main__":
    main()
