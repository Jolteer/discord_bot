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


def _info(guild: discord.Guild) -> GuildInfo:
    return GuildInfo(id=guild.id, name=guild.name, icon=guild.icon.key if guild.icon else None)


class GuildSync(commands.Cog):
    """Mirror Discord guild membership into the ``guilds`` table and purge leftovers."""

    def __init__(self, bot: JoltBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        """Start the daily purge loop when the extension loads."""
        self.purge.start()

    async def cog_unload(self) -> None:
        """Stop the purge loop when the extension unloads."""
        self.purge.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Runs after every (re)connect; syncing is idempotent, so that's fine.
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
        # Every module starts switched off until an admin sets it up on the dashboard.
        async with self.bot.db.session() as session, session.begin():
            await ensure_guild(session, _info(guild))
        log.info("Joined guild %s (%s)", guild.id, guild.name)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        # Kicked/banned or the server was deleted. (Outages fire on_guild_unavailable instead.)
        async with self.bot.db.session() as session, session.begin():
            await mark_guild_left(session, guild.id)
        log.info("Removed from guild %s; its data will be deleted in 30 days", guild.id)

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        if before.name != after.name or before.icon != after.icon:
            async with self.bot.db.session() as session, session.begin():
                await ensure_guild(session, _info(after))

    @tasks.loop(hours=24)
    async def purge(self) -> None:
        try:
            async with self.bot.db.session() as session, session.begin():
                purged = await purge_left_guilds(session)
            if purged:
                log.info("Deleted data for %d server(s) JoltBot left over 30 days ago", len(purged))
        except Exception:
            log.exception("Daily purge failed")

    @purge.before_loop
    async def _before_purge(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: JoltBot) -> None:
    """discord.py extension entry point for guild sync.

    Args:
        bot: The running ``JoltBot`` instance.
    """
    await bot.add_cog(GuildSync(bot))
