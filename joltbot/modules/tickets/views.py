"""Buttons, forms and prompts for the ticket module.

PanelView and TicketControlView are *persistent*: they're registered once at
startup with fixed custom_ids and timeout=None, so their buttons keep working
on every message ever posted, even after a restart. Neither stores per-guild
data; each click looks up the guild's settings (and the ticket, by channel ID).

Messages posted by the old KOC Gatekeeper bot use the IDs "rokverify:..." and
"rokticket:...". We register those too (legacy=True), so old panels and open
tickets keep working. Once every server has re-posted its panel with
/tickets panel and old tickets are closed, the legacy registration can go.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from ...core.models import Ticket
from ...core.templating import render
from . import store
from .checks import is_staff
from .context import load_context
from .settings import TicketSettings

if TYPE_CHECKING:
    from ...bot.client import JoltBot

log = logging.getLogger(__name__)

PANEL_ID = "joltbot:tickets:open"
ACCEPT_ID, DENY_ID, CLOSE_ID = "joltbot:tickets:accept", "joltbot:tickets:deny", "joltbot:tickets:close"
LEGACY_IDS = {
    PANEL_ID: "rokverify:open_application",
    ACCEPT_ID: "rokticket:accept",
    DENY_ID: "rokticket:deny",
    CLOSE_ID: "rokticket:close",
}
NOT_SET_UP = "Applications aren't set up on this server yet. Please let a staff member know."


async def safe_reply(interaction: discord.Interaction, text: str) -> None:
    """Reply privately, whether or not the interaction was already answered.

    Args:
        interaction: Discord component/slash interaction.
        text: Ephemeral message body.
    """
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except (discord.HTTPException, OSError):
        pass


def _custom_id(new_id: str, legacy: bool) -> str:
    return LEGACY_IDS[new_id] if legacy else new_id


# ---------------------------------------------------------------- applicant side


class ApplicationModal(discord.ui.Modal):
    """The application form. Built from the server's own questions (max 5)."""

    def __init__(self, bot: JoltBot, settings: TicketSettings) -> None:
        super().__init__(title=settings.messages.form_title[:45], timeout=900)
        self.bot = bot
        self.inputs: list[tuple[str, discord.ui.TextInput]] = []
        for question in settings.form_questions[:5]:
            text_input = discord.ui.TextInput(
                label=question.label[:45],
                placeholder=question.placeholder[:100] or None,
                required=question.required,
                style=discord.TextStyle.paragraph if question.long else discord.TextStyle.short,
                max_length=question.max_length,
            )
            self.add_item(text_input)
            self.inputs.append((question.label, text_input))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        assert isinstance(member, discord.Member)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ctx = await load_context(self.bot, member.guild.id)  # settings may have changed while they typed
        if not ctx.ready:
            await interaction.followup.send(NOT_SET_UP, ephemeral=True)
            return
        answers = [{"label": label, "value": (field.value or "").strip()} for label, field in self.inputs]
        channel, error = await self.bot.tickets.create_ticket(member, ctx, answers)
        if error:
            await interaction.followup.send(f"❌ {error}", ephemeral=True)
            return
        assert channel is not None
        text = render(
            ctx.settings.messages.ticket_opened,
            user=member.mention,
            user_name=member.display_name,
            server=member.guild.name,
            channel=channel.mention,
        )
        await interaction.followup.send(text[:2000], ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Application form failed", exc_info=error)
        await safe_reply(interaction, "Something went wrong submitting your application. Please try again.")


class PanelView(discord.ui.View):
    """The "Open Application" button on the panel message."""

    def __init__(self, bot: JoltBot, *, label: str = "Open Application", legacy: bool = False) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        button = discord.ui.Button(
            label=label[:80], emoji="📝", style=discord.ButtonStyle.success, custom_id=_custom_id(PANEL_ID, legacy)
        )
        button.callback = self.open_application
        self.add_item(button)

    async def open_application(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or interaction.guild is None:
            await interaction.response.send_message("This only works inside a server.", ephemeral=True)
            return
        # Discord gives us only 3 seconds to answer, so keep this part quick (two small DB reads).
        ctx = await load_context(self.bot, member.guild.id)
        if not ctx.ready:
            await interaction.response.send_message(NOT_SET_UP, ephemeral=True)
            return
        accept_ids = set(ctx.settings.accept_role_ids)
        if any(role.id in accept_ids for role in member.roles):
            text = render(
                ctx.settings.messages.already_verified,
                user=member.mention,
                user_name=member.display_name,
                server=member.guild.name,
            )
            await interaction.response.send_message(text[:2000], ephemeral=True)
            return
        existing = await self.bot.tickets.active_ticket_for(member)
        if existing is not None:
            where = f"<#{existing.channel_id}>" if existing.channel_id else "being created"
            await interaction.response.send_message(f"You already have an open application: {where}", ephemeral=True)
            return
        await interaction.response.send_modal(ApplicationModal(self.bot, ctx.settings))

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.exception("Panel button failed", exc_info=error)
        await safe_reply(interaction, "Something went wrong. Please try again in a moment.")


# ---------------------------------------------------------------- staff side


async def _applicant_uploaded_file(channel: discord.abc.Messageable, user_id: int) -> bool:
    try:
        async for msg in channel.history(limit=200):
            if msg.author.id == user_id and msg.attachments:
                return True
    except discord.HTTPException:
        return True  # don't block on a history error; just skip the warning
    return False


class TicketControlView(discord.ui.View):
    """Accept / Deny / Close buttons inside every ticket."""

    def __init__(self, bot: JoltBot, *, legacy: bool = False) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        for new_id, label, emoji, style, callback in (
            (ACCEPT_ID, "Accept", "✅", discord.ButtonStyle.success, self.accept),
            (DENY_ID, "Deny", "❌", discord.ButtonStyle.danger, self.deny),
            (CLOSE_ID, "Close", "🔒", discord.ButtonStyle.secondary, self.close),
        ):
            button = discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=_custom_id(new_id, legacy))
            button.callback = callback
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id is None:
            return False
        ctx = await load_context(self.bot, interaction.guild_id)
        if not is_staff(interaction.user, ctx.settings):
            text = render(ctx.settings.messages.not_staff, server=interaction.guild.name if interaction.guild else "")
            await interaction.response.send_message(text[:2000], ephemeral=True)
            return False
        return True

    async def _open_ticket(self, interaction: discord.Interaction) -> Ticket | None:
        ticket = await store.get_by_channel(self.bot.db, interaction.channel_id or 0)
        # The guild check stops a ticket row from one server ever being acted on from another.
        if ticket is None or ticket.guild_id != interaction.guild_id or ticket.status != "open":
            await interaction.response.send_message("This ticket is already closed or being closed.", ephemeral=True)
            return None
        return ticket

    async def accept(self, interaction: discord.Interaction) -> None:
        ticket = await self._open_ticket(interaction)
        if ticket is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ctx = await load_context(self.bot, ticket.guild_id)
        lines = [f"Accept <@{ticket.user_id}>?"]
        if ctx.settings.extra_role_choices:
            lines.append("Optionally pick an extra role, then press **Confirm accept**.")
        # TODO: ``require_attachment`` only warns; Accept still proceeds. If admins
        # expect a hard block, enforce it here (or rename the setting to match warn-only).
        if ctx.settings.require_attachment and not await _applicant_uploaded_file(interaction.channel, ticket.user_id):
            lines.append("⚠️ The applicant hasn't uploaded a file yet.")
        view = AcceptView(self.bot, ticket, ctx.settings)
        view.message = await interaction.followup.send("\n".join(lines), view=view, ephemeral=True, wait=True)

    async def deny(self, interaction: discord.Interaction) -> None:
        ticket = await self._open_ticket(interaction)
        if ticket is None:
            return
        ctx = await load_context(self.bot, ticket.guild_id)
        await interaction.response.send_modal(DenyReasonModal(self.bot, ticket, ctx.settings.messages.deny_reason_hint))

    async def close(self, interaction: discord.Interaction) -> None:
        ticket = await self._open_ticket(interaction)
        if ticket is None:
            return
        await interaction.response.send_message(
            "Close this ticket **without** accepting or denying?",
            view=CloseConfirmView(self.bot, ticket),
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.exception("Ticket button failed", exc_info=error)
        await safe_reply(interaction, "Something went wrong. Please try again in a moment.")


class _StaffPrompt(discord.ui.View):
    """Base for the short-lived private prompts only the clicking staff member sees.

    TODO: Unlike TicketControlView, this base does not re-run ``is_staff`` in
    ``interaction_check``. Risk is mitigated because prompts are ephemeral (only
    the clicker sees them), but a shared client or forwarded ephemeral could still
    confirm Accept/Deny/Close without a second staff check.
    """

    def __init__(self, bot: JoltBot, ticket: Ticket, timeout: float = 300) -> None:
        super().__init__(timeout=timeout)
        self.bot = bot
        self.ticket = ticket
        self.message: discord.Message | discord.WebhookMessage | None = None

    async def _finish(self, interaction: discord.Interaction, error: str | None, success: str) -> None:
        self.stop()
        try:
            await interaction.edit_original_response(content=f"❌ {error}" if error else success, view=None)
        except discord.HTTPException:
            pass

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(content="Timed out. Click the ticket button again if needed.", view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", view=None)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.exception("Staff prompt failed", exc_info=error)
        await safe_reply(interaction, "Something went wrong. Please try again in a moment.")


class AcceptView(_StaffPrompt):
    def __init__(self, bot: JoltBot, ticket: Ticket, settings: TicketSettings) -> None:
        super().__init__(bot, ticket)
        self.selected_role_id: int | None = None
        if settings.extra_role_choices:
            options = [discord.SelectOption(label="No extra role", value="0", emoji="➖", default=True)]
            options += [
                discord.SelectOption(label=c.label[:100], value=str(c.role_id), emoji=c.emoji or None)
                for c in settings.extra_role_choices[:24]
            ]
            self.select = discord.ui.Select(placeholder="Extra role (optional)", options=options, row=0)
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
            await self._finish(interaction, "That role no longer exists. Update the extra roles on the dashboard.", "")
            return
        error = await self.bot.tickets.accept(guild, interaction.user, self.ticket, role)
        extra = f" with {role.mention}" if role else ""
        await self._finish(interaction, error, f"✅ Accepted <@{self.ticket.user_id}>{extra}. Closing the ticket…")


class DenyReasonModal(discord.ui.Modal, title="Deny application"):
    reason = discord.ui.TextInput(
        label="Reason (sent to the applicant)", style=discord.TextStyle.paragraph, max_length=1000
    )

    def __init__(self, bot: JoltBot, ticket: Ticket, hint: str) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.ticket = ticket
        self.reason.placeholder = hint[:100] or None

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
    def __init__(self, bot: JoltBot, ticket: Ticket, reason: str) -> None:
        super().__init__(bot, ticket)
        self.reason = reason

    async def _deny(self, interaction: discord.Interaction, kick: bool) -> None:
        await interaction.response.defer()
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        error = await self.bot.tickets.deny(interaction.guild, interaction.user, self.ticket, self.reason, kick)
        done = "❌ Denied and kicked." if kick else "❌ Denied. They stay in the server."
        await self._finish(interaction, error, f"{done} Closing the ticket…")

    @discord.ui.button(label="Deny, don't kick", emoji="⏸️", style=discord.ButtonStyle.primary, row=0)
    async def keep(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._deny(interaction, kick=False)

    @discord.ui.button(label="Deny and kick", emoji="👢", style=discord.ButtonStyle.danger, row=0)
    async def kick(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._deny(interaction, kick=True)


class CloseConfirmView(_StaffPrompt):
    @discord.ui.button(label="Yes, close it", emoji="🔒", style=discord.ButtonStyle.danger, row=0)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        assert isinstance(interaction.user, discord.Member)
        error = await self.bot.tickets.close(interaction.user, self.ticket)
        await self._finish(interaction, error, "🔒 Closing the ticket…")
