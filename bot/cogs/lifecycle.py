"""Startup recovery + automatic closes (applicant left, channel deleted by hand)."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from ..utils.diagnostics import ERROR, WARN, run_diagnostics

if TYPE_CHECKING:
    from ..client import TicketBot

log = logging.getLogger(__name__)


class Lifecycle(commands.Cog):
    def __init__(self, bot: "TicketBot") -> None:
        self.bot = bot
        self._started = False

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # on_ready fires again after reconnects; only do startup work once.
        if self._started:
            return
        self._started = True
        log.info("Logged in as %s (%s)", self.bot.user, getattr(self.bot.user, "id", "?"))

        for finding in run_diagnostics(self.bot):
            level = {ERROR: logging.ERROR, WARN: logging.WARNING}.get(finding.level, logging.INFO)
            log.log(level, "[diagnostics] %s", finding.text.replace("**", ""))

        try:
            await self.bot.tickets.reconcile()
        except Exception:
            log.exception("Startup reconcile failed")

        if self.bot.cfg.assign_unverified_on_startup:
            await self._sweep_unverified()

    async def _sweep_unverified(self) -> None:
        """Give Unverified to anyone who joined while the bot was offline."""
        cfg = self.bot.cfg
        guild = self.bot.get_guild(cfg.guild_id)
        if guild is None:
            return
        unverified = guild.get_role(cfg.unverified_role_id)
        member_role = guild.get_role(cfg.member_role_id)
        if unverified is None or member_role is None:
            return
        humans = [m for m in guild.members if not m.bot]
        targets = [m for m in humans if member_role not in m.roles and unverified not in m.roles]
        if not targets:
            return
        # Safety net: if lots of people have neither role, /migrate-existing-members probably wasn't run yet.
        if len(targets) > max(25, len(humans) // 5):
            log.warning(
                "Startup sweep skipped: %d of %d members have neither Member nor Unverified. Run "
                "/migrate-existing-members first (README step 5), then restart.", len(targets), len(humans),
            )
            return
        for m in targets:
            try:
                await m.add_roles(unverified, reason="Joined while the verification bot was offline")
            except discord.HTTPException as e:
                log.warning("Sweep: couldn't give Unverified to %s: %s", m, e)
        log.info("Startup sweep: gave Unverified to %d member(s)", len(targets))

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent) -> None:
        if payload.guild_id == self.bot.cfg.guild_id:
            await self.bot.tickets.handle_member_left(payload.user.id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if channel.guild.id == self.bot.cfg.guild_id:
            await self.bot.tickets.handle_channel_deleted(channel.id)


async def setup(bot: "TicketBot") -> None:
    await bot.add_cog(Lifecycle(bot))
