"""New members get [Unverified]; staff post the verify panel."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..utils import embeds
from ..utils.checks import hierarchy_problems, staff_only
from ..views.verify_panel import VerifyPanelView

if TYPE_CHECKING:
    from ..client import TicketBot

log = logging.getLogger(__name__)


class Onboarding(commands.Cog):
    def __init__(self, bot: "TicketBot") -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        cfg = self.bot.cfg
        if member.guild.id != cfg.guild_id or member.bot:
            return
        role = member.guild.get_role(cfg.unverified_role_id)
        if role is None:
            log.error("Unverified role %s not found — check roles.unverified in config.json", cfg.unverified_role_id)
            return
        if problems := hierarchy_problems(member.guild, [role]):
            log.error("Can't give %s the Unverified role: %s", member, " ".join(problems))
            return
        try:
            await member.add_roles(role, reason="New member — awaiting verification")
            log.info("Gave Unverified to new member %s (%s)", member, member.id)
        except discord.HTTPException as e:
            log.error("Failed to give Unverified to %s: %s", member, e)

    @app_commands.command(name="setup-verify", description="Post (or re-post) the verification panel in the verify channel.")
    @app_commands.guild_only()
    @staff_only()
    async def setup_verify(self, interaction: discord.Interaction) -> None:
        cfg = self.bot.cfg
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)

        channel = guild.get_channel(cfg.verify_channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send("❌ channels.verify in config.json isn't a text channel in this server.", ephemeral=True)
            return
        perms = channel.permissions_for(guild.me)
        missing = [n for n in ("view_channel", "send_messages", "embed_links") if not getattr(perms, n)]
        if missing:
            await interaction.followup.send(
                f"❌ I'm missing {', '.join(m.replace('_', ' ').title() for m in missing)} in {channel.mention}.",
                ephemeral=True,
            )
            return

        # Remove the previous panel so there's only ever one.
        if old := await self.bot.db.get_setting("verify_panel"):
            try:
                old_ch_id, old_msg_id = map(int, old.split(":"))
                old_ch = guild.get_channel(old_ch_id)
                if isinstance(old_ch, discord.TextChannel):
                    await old_ch.get_partial_message(old_msg_id).delete()
            except (ValueError, discord.HTTPException):
                pass

        msg = await channel.send(embed=embeds.panel_embed(cfg, guild), view=VerifyPanelView(self.bot))
        await self.bot.db.set_setting("verify_panel", f"{channel.id}:{msg.id}")

        lines = [f"✅ Posted the verification panel in {channel.mention}."]
        try:
            await msg.pin(reason="Verification panel")
            lines.append("📌 Pinned it.")
        except discord.Forbidden:
            lines.append("⚠️ Couldn't pin it (I need **Pin Messages** there). Pin it manually if you like.")
        except discord.HTTPException:
            lines.append("⚠️ Couldn't pin it (pin limit reached?).")

        roles = [guild.get_role(cfg.member_role_id), guild.get_role(cfg.unverified_role_id)]
        roles += [guild.get_role(a.role_id) for a in cfg.alliance_roles]
        if problems := hierarchy_problems(guild, [r for r in roles if r]):
            lines.append("\n**Fix before approving anyone:**")
            lines += [f"• {p}" for p in problems]
        lines.append("\nRun `/verify-diagnostics` for a full check.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)


async def setup(bot: "TicketBot") -> None:
    await bot.add_cog(Onboarding(bot))
