"""Writes the bot's heartbeat once a minute while it is connected to Discord.

See ``core/health.py`` for who reads it. Nothing is written while the gateway
connection is down, so a bot that is running but can't reach Discord shows as
offline after a few minutes, the same as one that has crashed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from discord.ext import commands, tasks

from ..core.health import HEARTBEAT_INTERVAL_SECONDS, write_heartbeat

if TYPE_CHECKING:
    from .client import JoltBot

log = logging.getLogger(__name__)


class Heartbeat(commands.Cog):
    """Tracks the gateway connection and records a heartbeat while it is up."""

    def __init__(self, bot: JoltBot) -> None:
        self.bot = bot
        self._connected = False

    async def cog_load(self) -> None:
        """Start the heartbeat loop when the extension loads."""
        self.beat.start()

    async def cog_unload(self) -> None:
        """Stop the heartbeat loop when the extension unloads."""
        self.beat.cancel()

    # discord.py fires "connect" for a fresh session, "resumed" for a resumed one,
    # and "disconnect" whenever the connection drops or an attempt fails.

    @commands.Cog.listener()
    async def on_connect(self) -> None:
        self._connected = True

    @commands.Cog.listener()
    async def on_resumed(self) -> None:
        self._connected = True

    @commands.Cog.listener()
    async def on_disconnect(self) -> None:
        self._connected = False

    @tasks.loop(seconds=HEARTBEAT_INTERVAL_SECONDS)
    async def beat(self) -> None:
        if not self._connected or not self.bot.is_ready() or self.bot.is_closed():
            return
        try:
            async with self.bot.db.session() as session, session.begin():
                await write_heartbeat(session)
        except Exception:
            # A missed beat is harmless on its own (three in a row mark the bot offline),
            # so this is a warning, not an error that would page the operator.
            log.warning("Couldn't write the bot heartbeat", exc_info=True)

    @beat.before_loop
    async def _before_beat(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: JoltBot) -> None:
    """discord.py extension entry point for the heartbeat.

    Args:
        bot: The running ``JoltBot`` instance.
    """
    await bot.add_cog(Heartbeat(bot))
