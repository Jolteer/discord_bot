"""Staff buttons inside each ticket, plus the follow-up prompts (alliance pick, deny reason, confirmations).

TicketControlView is persistent (static custom_ids, timeout=None) and looks the
ticket up by channel ID, so one registered instance serves every ticket — even
ones opened before the last restart.

The follow-up prompts are short-lived ephemeral views; only the staff member
who clicked sees them.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from ..database import Ticket
from ..utils.checks import is_staff
from .verify_panel import _safe_reply

if TYPE_CHECKING:
    from ..client import TicketBot

log = logging.getLogger(__name__)


async def _applicant_uploaded_file(channel: discord.abc.Messageable, user_id: int) -> bool:
    try:
        async for msg in channel.history(limit=200):
            if msg.author.id == user_id and msg.attachments:
                return True
    except discord.HTTPException:
        return True  # don't block on a history error; just skip the warning
    return False


class TicketControlView(discord.ui.View):
    def __init__(self, bot: "TicketBot") -> None:
        super().__init__(timeout=None)
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_staff(interaction.user, self.bot.cfg):
            await interaction.response.send_message(self.bot.cfg.messages["not_staff"], ephemeral=True)
            return False
        return True

    async def _open_ticket(self, interaction: discord.Interaction) -> Ticket | None:
        t = await self.bot.db.get_by_channel(interaction.channel_id or 0)
        if t is None or t.status != "open":
            await interaction.response.send_message("This ticket is already closed or being closed.", ephemeral=True)
            return None
        return t

    @discord.ui.button(label="Accept", emoji="✅", style=discord.ButtonStyle.success, custom_id="rokticket:accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        ticket = await self._open_ticket(interaction)
        if ticket is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        lines = [f"Accept <@{ticket.user_id}>?"]
        if self.bot.cfg.alliance_roles:
            lines.append("Optionally pick an alliance role, then press **Confirm accept**.")
        if not await _applicant_uploaded_file(interaction.channel, ticket.user_id):  # type: ignore[arg-type]
            lines.append("⚠️ The applicant hasn't uploaded a screenshot yet.")
        view = AcceptView(self.bot, ticket)
        view.message = await interaction.followup.send("\n".join(lines), view=view, ephemeral=True, wait=True)

    @discord.ui.button(label="Deny", emoji="❌", style=discord.ButtonStyle.danger, custom_id="rokticket:deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        ticket = await self._open_ticket(interaction)
        if ticket is None:
            return
        await interaction.response.send_modal(DenyReasonModal(self.bot, ticket))

    @discord.ui.button(label="Close", emoji="🔒", style=discord.ButtonStyle.secondary, custom_id="rokticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        ticket = await self._open_ticket(interaction)
        if ticket is None:
            return
        await interaction.response.send_message(
            "Close this ticket **without** accepting or denying? The applicant stays Unverified.",
            view=CloseConfirmView(self.bot, ticket),
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.exception("Ticket control button failed", exc_info=error)
        await _safe_reply(interaction, "Something went wrong. Check the bot's console log.")


class _StaffPrompt(discord.ui.View):
    """Base for the short-lived ephemeral prompts."""

    def __init__(self, bot: "TicketBot", ticket: Ticket, timeout: float = 300) -> None:
        super().__init__(timeout=timeout)
        self.bot = bot
        self.ticket = ticket
        self.message: discord.Message | discord.WebhookMessage | None = None

    async def _finish(self, interaction: discord.Interaction, error: str | None, success: str) -> None:
        self.stop()
        content = f"❌ {error}" if error else success
        try:
            await interaction.edit_original_response(content=content, view=None)
        except discord.HTTPException:
            pass

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(content="Timed out — click the ticket button again if needed.", view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", view=None)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.exception("Staff prompt failed", exc_info=error)
        await _safe_reply(interaction, "Something went wrong. Check the bot's console log.")


class AcceptView(_StaffPrompt):
    def __init__(self, bot: "TicketBot", ticket: Ticket) -> None:
        super().__init__(bot, ticket)
        self.selected_role_id: int | None = None
        if bot.cfg.alliance_roles:
            options = [discord.SelectOption(label="No alliance role", value="0", emoji="➖", default=True)]
            options += [
                discord.SelectOption(label=a.label, value=str(a.role_id), emoji=a.emoji)
                for a in bot.cfg.alliance_roles[:24]
            ]
            self.select = discord.ui.Select(placeholder="Alliance role (optional)", options=options, row=0)
            self.select.callback = self._on_select
            self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        self.selected_role_id = int(self.select.values[0]) or None
        await interaction.response.defer()

    @discord.ui.button(label="Confirm accept", emoji="✅", style=discord.ButtonStyle.success, row=1)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        guild = interaction.guild
        assert guild is not None and isinstance(interaction.user, discord.Member)
        role = guild.get_role(self.selected_role_id) if self.selected_role_id else None
        if self.selected_role_id and role is None:
            await self._finish(interaction, "That alliance role no longer exists. Fix config.json.", "")
            return
        error = await self.bot.tickets.accept(guild, interaction.user, self.ticket, role)
        extra = f" with {role.mention}" if role else ""
        await self._finish(interaction, error, f"✅ Accepted <@{self.ticket.user_id}>{extra}. Closing the ticket…")


class DenyReasonModal(discord.ui.Modal, title="Deny application"):
    reason = discord.ui.TextInput(
        label="Reason (sent to the applicant)",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        placeholder="e.g. Power below the kingdom's minimum for migration.",
    )

    def __init__(self, bot: "TicketBot", ticket: Ticket) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.ticket = ticket

    async def on_submit(self, interaction: discord.Interaction) -> None:
        reason = self.reason.value.strip()
        view = DenyActionView(self.bot, self.ticket, reason)
        await interaction.response.send_message(
            f"**Reason:** {reason}\n\nWhat should happen to <@{self.ticket.user_id}>?",
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        view.message = await interaction.original_response()


class DenyActionView(_StaffPrompt):
    def __init__(self, bot: "TicketBot", ticket: Ticket, reason: str) -> None:
        super().__init__(bot, ticket)
        self.reason = reason

    async def _deny(self, interaction: discord.Interaction, kick: bool) -> None:
        await interaction.response.defer()
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        error = await self.bot.tickets.deny(interaction.guild, interaction.user, self.ticket, self.reason, kick)
        done = "❌ Denied and kicked." if kick else "❌ Denied — they stay Unverified."
        await self._finish(interaction, error, f"{done} Closing the ticket…")

    @discord.ui.button(label="Keep Unverified", emoji="⏸️", style=discord.ButtonStyle.primary, row=0)
    async def keep(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._deny(interaction, kick=False)

    @discord.ui.button(label="Kick from server", emoji="👢", style=discord.ButtonStyle.danger, row=0)
    async def kick(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._deny(interaction, kick=True)


class CloseConfirmView(_StaffPrompt):
    @discord.ui.button(label="Yes, close it", emoji="🔒", style=discord.ButtonStyle.danger, row=0)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        assert isinstance(interaction.user, discord.Member)
        error = await self.bot.tickets.close(interaction.user, self.ticket)
        await self._finish(interaction, error, "🔒 Closing the ticket…")
