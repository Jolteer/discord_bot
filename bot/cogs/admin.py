"""Staff tools: /verify-diagnostics and the one-time /migrate-existing-members."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..utils.checks import hierarchy_problems, staff_only
from ..utils.diagnostics import ERROR, WARN, run_diagnostics

if TYPE_CHECKING:
    from ..client import TicketBot

log = logging.getLogger(__name__)


class MigrateConfirmView(discord.ui.View):
    def __init__(self, cog: "Admin", targets: list[discord.Member], role: discord.Role) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.targets = targets
        self.role = role

    @discord.ui.button(label="Give them Member", style=discord.ButtonStyle.success)
    async def go(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content=f"Working… 0/{len(self.targets)}", view=None)
        await self.cog.run_migration(interaction, self.targets, self.role)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", view=None)


class Admin(commands.Cog):
    def __init__(self, bot: "TicketBot") -> None:
        self.bot = bot
        self._migration_lock = asyncio.Lock()

    @app_commands.command(name="verify-diagnostics", description="Check the bot's permissions, role order and channel setup.")
    @app_commands.guild_only()
    @staff_only()
    async def diagnostics(self, interaction: discord.Interaction) -> None:
        findings = run_diagnostics(self.bot)
        errors = sum(f.level == ERROR for f in findings)
        warns = sum(f.level == WARN for f in findings)
        colour = discord.Colour.red() if errors else discord.Colour.orange() if warns else discord.Colour.green()
        body = "\n".join(f.render() for f in findings)
        embed = discord.Embed(
            title="Verification bot diagnostics",
            description=body[:4000],
            colour=colour,
        )
        embed.set_footer(text=f"{errors} error(s), {warns} warning(s)")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="migrate-existing-members",
        description="One-time: give the Member role to everyone already here (before switching verification on).",
    )
    @app_commands.guild_only()
    @staff_only()
    async def migrate(self, interaction: discord.Interaction) -> None:
        cfg = self.bot.cfg
        guild = interaction.guild
        assert guild is not None
        member_role = guild.get_role(cfg.member_role_id)
        unverified = guild.get_role(cfg.unverified_role_id)
        if member_role is None:
            await interaction.response.send_message("❌ The Member role from config.json doesn't exist.", ephemeral=True)
            return
        if problems := hierarchy_problems(guild, [member_role]):
            await interaction.response.send_message("❌ " + "\n".join(problems), ephemeral=True)
            return
        if self._migration_lock.locked():
            await interaction.response.send_message("A migration is already running.", ephemeral=True)
            return
        targets = [
            m for m in guild.members
            if not m.bot and member_role not in m.roles and (unverified is None or unverified not in m.roles)
        ]
        if not targets:
            await interaction.response.send_message("Nobody to migrate — everyone already has Member or Unverified.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"This gives {member_role.mention} to **{len(targets)}** member(s) who have **neither** Member nor "
            "Unverified (i.e. people who were here before the bot). Anyone with Unverified is skipped.\n"
            "Large servers take a few minutes because of Discord rate limits. Continue?",
            view=MigrateConfirmView(self, targets, member_role),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def run_migration(self, interaction: discord.Interaction, targets: list[discord.Member], role: discord.Role) -> None:
        async with self._migration_lock:
            done = failed = 0
            for i, m in enumerate(targets, 1):
                try:
                    await m.add_roles(role, reason=f"Migration to verification system by {interaction.user}")
                    done += 1
                except discord.HTTPException as e:
                    failed += 1
                    log.warning("Migration: couldn't give Member to %s: %s", m, e)
                if i % 25 == 0:
                    try:
                        await interaction.edit_original_response(content=f"Working… {i}/{len(targets)}")
                    except discord.HTTPException:
                        pass  # interaction tokens expire after 15 min; keep going
            log.info("Migration finished: %d given, %d failed (by %s)", done, failed, interaction.user)
            try:
                await interaction.edit_original_response(
                    content=f"✅ Migration done: gave Member to **{done}** member(s)" + (f", **{failed}** failed (see log)." if failed else ".")
                )
            except discord.HTTPException:
                pass


async def setup(bot: "TicketBot") -> None:
    await bot.add_cog(Admin(bot))
