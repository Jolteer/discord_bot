"""Start the bot: python -m joltbot.bot

The old bot exited on any startup error (e.g. Discord briefly unreachable),
and systemd gave up after 5 quick failures, leaving it offline for good. Now
temporary errors are retried here with growing delays, forever. Only problems
a retry can't fix (bad token, missing intents, bad setup) stop the process,
with distinct exit codes that the systemd unit is told not to restart on.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time

import discord

from ..core.config import get_config
from ..core.db import Database
from ..core.logsetup import setup_logging
from ..core.schema import schema_problem
from .client import JoltBot

EXIT_BAD_TOKEN = 2
EXIT_BAD_SETUP = 3

log = logging.getLogger("joltbot.bot")


async def check_database(url: str) -> str | None:
    """None if the schema is current, otherwise what to do about it."""
    db = Database(url)
    try:
        return await schema_problem(db)
    finally:
        await db.dispose()


async def run_once() -> None:
    """Run the bot until it disconnects for good or raises."""
    bot = JoltBot(get_config())
    async with bot:
        await bot.start(get_config().discord_token)


def main() -> None:
    config = get_config()
    setup_logging("bot", config.log_level, config.log_dir)
    if missing := config.missing("discord_token"):
        log.critical("Missing in .env: %s (see .env.example).", ", ".join(missing))
        sys.exit(EXIT_BAD_SETUP)
    if problem := asyncio.run(check_database(config.database_url)):
        log.critical(problem)
        sys.exit(EXIT_BAD_SETUP)

    delay = 10
    while True:
        started = time.monotonic()
        try:
            asyncio.run(run_once())
            return  # clean shutdown
        except KeyboardInterrupt:
            return
        except discord.LoginFailure:
            log.critical(
                "Discord rejected the token. Reset it in the Developer Portal (Bot → Reset Token) "
                "and update DISCORD_TOKEN in .env."
            )
            sys.exit(EXIT_BAD_TOKEN)
        except discord.PrivilegedIntentsRequired:
            log.critical(
                "Privileged intents are off. Developer Portal → your app → Bot → turn ON 'Server Members "
                "Intent' and 'Message Content Intent', save, then restart."
            )
            sys.exit(EXIT_BAD_SETUP)
        except Exception:
            log.exception("JoltBot stopped unexpectedly; starting again in %ss", delay)
        if time.monotonic() - started > 600:
            delay = 10  # it ran fine for a while, so start the back-off over
        time.sleep(delay)
        delay = min(delay * 2, 300)


if __name__ == "__main__":
    main()
