"""The 'Open Application' button in #verify and the application form (modal)."""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import discord

from ..tickets import ApplicationAnswers

if TYPE_CHECKING:
    from ..client import TicketBot

log = logging.getLogger(__name__)


class ApplicationModal(discord.ui.Modal, title="Kingdom Application"):
    governor_name = discord.ui.TextInput(label="Governor name", max_length=64)
    governor_id = discord.ui.TextInput(
        label="Governor ID", placeholder="Numbers only — shown on your governor profile", min_length=4, max_length=24
    )
    power = discord.ui.TextInput(label="Current power", placeholder="e.g. 45M or 45,000,000", max_length=32)
    alliance = discord.ui.TextInput(label="Alliance you're in or applying to", max_length=64)
    extra = discord.ui.TextInput(
        label="Anything else? (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
        placeholder="Migrating from another kingdom? Main/farm account? Anything leadership should know.",
    )

    def __init__(self, bot: "TicketBot") -> None:
        super().__init__(timeout=900)
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        assert isinstance(member, discord.Member)
        # Be forgiving with formatting ("123 456 789", "#123456789") rather than throwing away their answers.
        gov_id = re.sub(r"\D", "", self.governor_id.value)
        await interaction.response.defer(ephemeral=True, thinking=True)
        answers = ApplicationAnswers(
            governor_name=self.governor_name.value.strip(),
            governor_id=gov_id or self.governor_id.value.strip(),
            power=self.power.value.strip(),
            alliance=self.alliance.value.strip(),
            extra=(self.extra.value or "").strip() or None,
        )
        channel, error = await self.bot.tickets.create_ticket(member, answers)
        if error:
            await interaction.followup.send(f"❌ {error}", ephemeral=True)
        else:
            assert channel is not None
            await interaction.followup.send(
                f"✅ Your application ticket is open: {channel.mention}\n"
                "Head there and upload a screenshot of your governor profile.",
                ephemeral=True,
            )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Application modal failed", exc_info=error)
        await _safe_reply(interaction, "Something went wrong submitting your application. Please try again or ping staff.")


class VerifyPanelView(discord.ui.View):
    """Persistent: registered once at startup, works on every panel message forever."""

    def __init__(self, bot: "TicketBot") -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Open Application", emoji="📝", style=discord.ButtonStyle.success, custom_id="rokverify:open_application"
    )
    async def open_application(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cfg = self.bot.cfg
        member = interaction.user
        if not isinstance(member, discord.Member) or interaction.guild_id != cfg.guild_id:
            await interaction.response.send_message("This only works in the kingdom server.", ephemeral=True)
            return
        if any(r.id == cfg.member_role_id for r in member.roles):
            await interaction.response.send_message(cfg.messages["already_verified"], ephemeral=True)
            return
        existing = await self.bot.tickets.active_ticket_for(member)
        if existing is not None:
            where = f"<#{existing.channel_id}>" if existing.channel_id else "being created"
            await interaction.response.send_message(f"You already have an open application: {where}", ephemeral=True)
            return
        await interaction.response.send_modal(ApplicationModal(self.bot))

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.exception("Verify panel button failed", exc_info=error)
        await _safe_reply(interaction, "Something went wrong. Please try again in a moment.")


async def _safe_reply(interaction: discord.Interaction, text: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass
