"""Keeps the guilds table in step with the servers JoltBot is actually in."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, tasks

from ..core.guilds import GuildInfo, ensure_guild, mark_guild_left, purge_left_guilds, sync_guilds

if TYPE_CHECKING:
    from .client import JoltBot

log = logging.getLogger(__name__)

PURGE_EVERY_HOURS = 24  # how often data of servers JoltBot left over 30 days ago is deleted


def _info(guild: discord.Guild) -> GuildInfo:
    """The parts of a Discord server the database keeps: its ID, name and icon hash."""
    return GuildInfo(id=guild.id, name=guild.name, icon=guild.icon.key if guild.icon else None)


class GuildSync(commands.Cog):
    """Mirror Discord guild membership into the ``guilds`` table and purge leftovers."""

    def __init__(self, bot: JoltBot) -> None:
        """Keep the bot; the purge loop is started in ``cog_load``."""
        self.bot = bot

    async def cog_load(self) -> None:
        """Start the daily purge loop when the extension loads."""
        self.purge.start()

    async def cog_unload(self) -> None:
        """Stop the purge loop when the extension unloads."""
        self.purge.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Bring the guilds table in line with the servers the bot is in right now.

        discord.py fires this after every connect and reconnect. The sync is
        idempotent, so repeats are harmless. Servers Discord reports as unavailable
        (an outage on its side) are left alone, not marked as left.
        """
        present = [_info(g) for g in self.bot.guilds if not g.unavailable]
        unavailable = {g.id for g in self.bot.guilds if g.unavailable}
        async with self.bot.db.session() as session, session.begin():
            joined, left = await sync_guilds(session, present, unavailable)
        log.info(
            "Ready as %s in %d server(s); %d new/rejoined, %d marked as left",
            self.bot.user,
            len(present),
            len(joined),
            len(left),
        )

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Record a server JoltBot was just added to.

        Every module starts switched off there until an admin sets it up on the dashboard.

        Args:
            guild: The server that was joined.
        """
        async with self.bot.db.session() as session, session.begin():
            await ensure_guild(session, _info(guild))
        log.info("Joined guild %s (%s)", guild.id, guild.name)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        """Mark a server as left: JoltBot was kicked or banned, or the server was deleted.

        Its data is kept for 30 days in case JoltBot is invited back. A Discord outage
        fires ``on_guild_unavailable`` instead, so it never ends up here.

        Args:
            guild: The server JoltBot is no longer in.
        """
        async with self.bot.db.session() as session, session.begin():
            await mark_guild_left(session, guild.id)
        log.info("Removed from guild %s; its data will be deleted in 30 days", guild.id)

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        """Keep the stored name and icon current when a server changes them.

        Args:
            before: The server as it was.
            after: The server as it is now.
        """
        if before.name != after.name or before.icon != after.icon:
            async with self.bot.db.session() as session, session.begin():
                await ensure_guild(session, _info(after))

    @tasks.loop(hours=PURGE_EVERY_HOURS)
    async def purge(self) -> None:
        """Once a day, delete the data of servers JoltBot left more than 30 days ago.

        A failure is logged and tried again the next day; it must not stop the loop.
        """
        try:
            async with self.bot.db.session() as session, session.begin():
                purged = await purge_left_guilds(session)
            if purged:
                log.info("Deleted data for %d server(s) JoltBot left over 30 days ago", len(purged))
        except Exception:
            log.exception("Daily purge failed")

    @purge.before_loop
    async def _before_purge(self) -> None:
        """Hold the first run until the bot is connected."""
        await self.bot.wait_until_ready()


async def setup(bot: JoltBot) -> None:
    """discord.py extension entry point for guild sync.

    Args:
        bot: The running ``JoltBot`` instance.
    """
    await bot.add_cog(GuildSync(bot))
