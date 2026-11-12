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
import signal
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
    """Verify the DB is at Alembic head before connecting to Discord.

    Args:
        url: SQLAlchemy database URL.

    Returns:
        ``None`` if the schema is current; otherwise a human-readable problem
        string suitable for a critical log line.
    """
    db = Database(url)
    try:
        return await schema_problem(db)
    finally:
        await db.dispose()


def close_on_sigterm(bot: JoltBot) -> None:
    """Disconnect cleanly when the process is asked to stop.

    ``docker stop`` and ``systemctl stop`` send SIGTERM. Without a handler the
    bot is killed mid-work (and under Docker only after a 10-second wait).
    Closing the bot makes ``bot.start`` return, so the process exits normally.
    Must be called from inside the running event loop.

    Args:
        bot: The bot to close when SIGTERM arrives.
    """
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, lambda: loop.create_task(bot.close()))
    except (NotImplementedError, RuntimeError, ValueError) as e:
        # Windows event loops have no signal handlers (Ctrl+C still works there). Whatever
        # the reason, a missing stop handler must never keep the bot from starting.
        log.debug("No SIGTERM handler installed: %r", e)


async def run_once() -> None:
    """Run the bot until it disconnects cleanly or raises.

    Raises:
        discord.LoginFailure: Invalid token.
        discord.PrivilegedIntentsRequired: Portal intents not enabled.
        Exception: Other startup/runtime failures (caller may retry).
    """
    bot = JoltBot(get_config())
    async with bot:
        close_on_sigterm(bot)
        await bot.start(get_config().discord_token)


def main() -> None:
    """CLI entry: validate setup, then connect with forever-retry on transient errors.

    Exit codes:
        2 — Discord rejected the token (``EXIT_BAD_TOKEN``).
        3 — Missing env / schema / privileged intents (``EXIT_BAD_SETUP``).
    """
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
