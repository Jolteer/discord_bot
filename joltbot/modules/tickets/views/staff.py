"""What staff see: the Accept / Deny / Close buttons in a ticket, and the prompts they open."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

import discord

from ....bot.interactions import safe_reply
from ....core.limits import MESSAGE_MAX, SELECT_OPTION_LABEL_MAX, TEXT_INPUT_PLACEHOLDER_MAX
from ....core.models import Ticket
from ....core.templating import render
from .. import store
from ..checks import is_staff
from ..constants import STATUS_OPEN
from ..context import load_context
from ..settings import MAX_EXTRA_ROLE_CHOICES, TicketSettings
from .common import ACCEPT_ID, CLOSE_ID, DENY_ID, custom_id

if TYPE_CHECKING:
    from ....bot.client import JoltBot

log = logging.getLogger(__name__)

PROMPT_TIMEOUT_SECONDS = 300  # how long a private staff prompt stays usable
DENY_FORM_TIMEOUT_SECONDS = 600  # how long staff have to type a deny reason
DENY_REASON_MAX = 1000  # characters; the reason must also fit in the DM and the log embed
UPLOAD_CHECK_MESSAGES = 200  # how far back to look for a file from the applicant
NO_EXTRA_ROLE = "0"  # the dropdown value of the "No extra role" entry


async def staff_check(bot: JoltBot, interaction: discord.Interaction) -> bool:
    """Allow the click only if the user is staff in this server right now.

    Settings and roles are read fresh, so removing someone's staff role (or
    changing the staff roles on the dashboard) takes effect on their next click.

    Args:
        bot: Running bot (provides the database).
        interaction: The button or select interaction to check.

    Returns:
        True for staff. Otherwise replies with the server's "not staff" message
        and returns False.
    """
    if interaction.guild_id is None:
        return False
    ctx = await load_context(bot, interaction.guild_id)
    if not is_staff(interaction.user, ctx.settings):
        text = render(ctx.settings.messages.not_staff, server=interaction.guild.name if interaction.guild else "")
        await interaction.response.send_message(text[:MESSAGE_MAX], ephemeral=True)
        return False
    return True


async def _applicant_uploaded_file(channel: discord.abc.Messageable, user_id: int) -> bool:
    """Whether the applicant posted a file in their ticket, among its most recent messages.

    Returns True when the history can't be read, so that a Discord error never
    produces a false "no file" warning.
    """
    try:
        async for msg in channel.history(limit=UPLOAD_CHECK_MESSAGES):
            if msg.author.id == user_id and msg.attachments:
                return True
    except discord.HTTPException:
        return True  # don't block on a history error; just skip the warning
    return False


class TicketControlView(discord.ui.View):
    """Accept / Deny / Close buttons inside every ticket."""

    def __init__(self, bot: JoltBot, *, legacy: bool = False) -> None:
        """Create the three buttons.

        Args:
            bot: Running bot.
            legacy: True registers the old bot's button IDs instead of JoltBot's.
        """
        # timeout=None and fixed custom_ids are what make a view persistent: once registered
        # with bot.add_view, its buttons work on every old message, also after a restart.
        super().__init__(timeout=None)
        self.bot = bot
        for new_id, label, emoji, style, callback in (
            (ACCEPT_ID, "Accept", "✅", discord.ButtonStyle.success, self.accept),
            (DENY_ID, "Deny", "❌", discord.ButtonStyle.danger, self.deny),
            (CLOSE_ID, "Close", "🔒", discord.ButtonStyle.secondary, self.close),
        ):
            button: discord.ui.Button[TicketControlView] = discord.ui.Button(
                label=label, emoji=emoji, style=style, custom_id=custom_id(new_id, legacy)
            )
            # Assigning ``callback`` is discord.py's way to give a button built in code its
            # handler. A type checker sees a method being replaced, hence the ignore.
            button.callback = callback  # type: ignore[method-assign]
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Staff only. Runs before every button of this view."""
        return await staff_check(self.bot, interaction)

    async def _open_ticket(self, interaction: discord.Interaction) -> Ticket | None:
        """The open ticket this channel belongs to.

        Args:
            interaction: A click on one of the ticket buttons.

        Returns:
            The ticket, or ``None`` after telling the clicker it is already closed or closing.
        """
        ticket = await store.get_by_channel(self.bot.db, interaction.channel_id or 0)
        # The guild check stops a ticket row from one server ever being acted on from another.
        if ticket is None or ticket.guild_id != interaction.guild_id or ticket.status != STATUS_OPEN:
            await interaction.response.send_message("This ticket is already closed or being closed.", ephemeral=True)
            return None
        return ticket

    async def accept(self, interaction: discord.Interaction) -> None:
        """Accept was clicked: ask the staff member to confirm, and offer the extra role choices.

        Staff only (``interaction_check``). Nothing changes until **Confirm accept** is pressed.
        """
        ticket = await self._open_ticket(interaction)
        if ticket is None:
            return
        # Discord wants an answer within 3 seconds; reading the channel's history below can take longer.
        await interaction.response.defer(ephemeral=True, thinking=True)
        ctx = await load_context(self.bot, ticket.guild_id)
        lines = [f"Accept <@{ticket.user_id}>?"]
        picked, _ = ctx.settings.picked_roles(ticket.answers)
        if picked:
            roles = ", ".join(f"<@&{role_id}>" for role_id in dict.fromkeys(role_id for _, role_id in picked))
            lines.append(f"Their answers also give them: {roles}.")
        if ctx.settings.extra_role_choices:
            lines.append("Optionally pick an extra role, then press **Confirm accept**.")
        # TODO: ``require_attachment`` only warns; Accept still proceeds. If admins
        # expect a hard block, enforce it here (or rename the setting to match warn-only).
        # The cast: these buttons only exist on a message in a ticket's text channel.
        channel = cast(discord.abc.Messageable, interaction.channel)
        if ctx.settings.require_attachment and not await _applicant_uploaded_file(channel, ticket.user_id):
            lines.append("⚠️ The applicant hasn't uploaded a file yet.")
        view = AcceptView(self.bot, ticket, ctx.settings)
        view.message = await interaction.followup.send("\n".join(lines), view=view, ephemeral=True, wait=True)

    async def deny(self, interaction: discord.Interaction) -> None:
        """Deny was clicked: ask the staff member for the reason. Staff only."""
        ticket = await self._open_ticket(interaction)
        if ticket is None:
            return
        ctx = await load_context(self.bot, ticket.guild_id)
        # Not deferred: Discord only lets a form be opened as the very first answer to a click.
        await interaction.response.send_modal(DenyReasonModal(self.bot, ticket, ctx.settings.messages.deny_reason_hint))

    async def close(self, interaction: discord.Interaction) -> None:
        """Close was clicked: ask the staff member to confirm closing without a decision. Staff only."""
        ticket = await self._open_ticket(interaction)
        if ticket is None:
            return
        await interaction.response.send_message(
            "Close this ticket **without** accepting or denying?",
            view=CloseConfirmView(self.bot, ticket),
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        """Log an unexpected failure and ask the staff member to try again."""
        log.exception("Ticket button failed", exc_info=error)
        await safe_reply(interaction, "Something went wrong. Please try again in a moment.")


class _StaffPrompt(discord.ui.View):
    """Base for the short-lived private prompts only the clicking staff member sees.

    Every click on a prompt re-checks staff, like the ticket buttons do. A prompt
    stays open for minutes, and someone can lose their staff role in that time.
    """

    def __init__(self, bot: JoltBot, ticket: Ticket, timeout: float = PROMPT_TIMEOUT_SECONDS) -> None:
        """Create a prompt for one ticket.

        Args:
            bot: Running bot.
            ticket: The ticket this prompt decides.
            timeout: Seconds the prompt stays usable.
        """
        super().__init__(timeout=timeout)
        self.bot = bot
        self.ticket = ticket
        self.message: discord.Message | discord.WebhookMessage | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Staff only, checked again on every click of the prompt."""
        return await staff_check(self.bot, interaction)

    async def _finish(self, interaction: discord.Interaction, error: str | None, success: str) -> None:
        """End the prompt, replacing it with the outcome.

        Args:
            interaction: The click that was just handled.
            error: Why it didn't work, or ``None`` if it did.
            success: The text shown when it worked.
        """
        self.stop()
        try:
            await interaction.edit_original_response(content=f"❌ {error}" if error else success, view=None)
        except discord.HTTPException as e:
            log.debug("Couldn't update the staff prompt for ticket #%s: %s", self.ticket.id, e)

    async def on_timeout(self) -> None:
        """Replace the prompt with a note when it expires."""
        if self.message is not None:
            try:
                await self.message.edit(content="Timed out. Click the ticket button again if needed.", view=None)
            except discord.HTTPException as e:
                log.debug("Couldn't mark the staff prompt for ticket #%s as timed out: %s", self.ticket.id, e)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Close the prompt without doing anything. Staff only."""
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", view=None)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        """Log an unexpected failure and ask the staff member to try again."""
        log.exception("Staff prompt failed", exc_info=error)
        await safe_reply(interaction, "Something went wrong. Please try again in a moment.")


class AcceptView(_StaffPrompt):
    """The private "Confirm accept" prompt, with the optional extra-role dropdown.

    Args:
        bot: Running bot.
        ticket: The ticket being accepted.
        settings: The server's ticket settings (its extra role choices).
    """

    def __init__(self, bot: JoltBot, ticket: Ticket, settings: TicketSettings) -> None:
        """Add the extra-role dropdown, if the server has extra role choices."""
        super().__init__(bot, ticket)
        self.selected_role_id: int | None = None
        if settings.extra_role_choices:
            options = [discord.SelectOption(label="No extra role", value=NO_EXTRA_ROLE, emoji="➖", default=True)]
            options += [
                discord.SelectOption(
                    label=c.label[:SELECT_OPTION_LABEL_MAX], value=str(c.role_id), emoji=c.emoji or None
                )
                for c in settings.extra_role_choices[:MAX_EXTRA_ROLE_CHOICES]
            ]
            self.select: discord.ui.Select[AcceptView] = discord.ui.Select(
                placeholder="Extra role (optional)", options=options, row=0
            )
            self.select.callback = self._on_select  # type: ignore[method-assign]  # see TicketControlView
            self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        """Remember which extra role was picked, or that none was."""
        self.selected_role_id = int(self.select.values[0]) or None
        # There is nothing to show, but Discord reports "interaction failed" unless every click is answered.
        await interaction.response.defer()

    @discord.ui.button(label="Confirm accept", emoji="✅", style=discord.ButtonStyle.success, row=1)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Accept the applicant: give the roles, DM them and close the ticket.

        Staff only. A problem (role order, a role that no longer exists, someone else
        handled the ticket first) is shown in the prompt and nothing is changed.
        """
        # Answer the click first: giving roles and sending the DM can take longer than Discord's 3 seconds.
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
    """The form where staff type why an application is denied.

    The reason is sent to the applicant. Submitting it opens ``DenyActionView``,
    which asks whether to kick them or keep them.

    Args:
        bot: Running bot.
        ticket: The ticket being denied.
        hint: Grey hint text for the reason box, from the server's settings.
    """

    reason: discord.ui.TextInput[DenyReasonModal] = discord.ui.TextInput(
        label="Reason (sent to the applicant)", style=discord.TextStyle.paragraph, max_length=DENY_REASON_MAX
    )

    def __init__(self, bot: JoltBot, ticket: Ticket, hint: str) -> None:
        """Set the reason box's hint text."""
        super().__init__(timeout=DENY_FORM_TIMEOUT_SECONDS)
        self.bot = bot
        self.ticket = ticket
        self.reason.placeholder = hint[:TEXT_INPUT_PLACEHOLDER_MAX] or None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Show the reason back and ask what should happen to the applicant."""
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
    """The private prompt after a deny reason: deny and kick, or deny and keep.

    Args:
        bot: Running bot.
        ticket: The ticket being denied.
        reason: The reason typed in ``DenyReasonModal``.
    """

    def __init__(self, bot: JoltBot, ticket: Ticket, reason: str) -> None:
        """Keep the typed reason for the denial."""
        super().__init__(bot, ticket)
        self.reason = reason

    async def _deny(self, interaction: discord.Interaction, kick: bool) -> None:
        """Carry out the denial and show the result in the prompt.

        Args:
            interaction: The button click.
            kick: True to kick the applicant after they are told.
        """
        await interaction.response.defer()
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        error = await self.bot.tickets.deny(interaction.guild, interaction.user, self.ticket, self.reason, kick)
        done = "❌ Denied and kicked." if kick else "❌ Denied. They stay in the server."
        await self._finish(interaction, error, f"{done} Closing the ticket…")

    @discord.ui.button(label="Deny, don't kick", emoji="⏸️", style=discord.ButtonStyle.primary, row=0)
    async def keep(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Deny, and leave the applicant in the server. Staff only."""
        await self._deny(interaction, kick=False)

    @discord.ui.button(label="Deny and kick", emoji="👢", style=discord.ButtonStyle.danger, row=0)
    async def kick(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Deny, and kick the applicant. Staff only.

        JoltBot needs Kick Members and a role above the applicant's highest one.
        """
        await self._deny(interaction, kick=True)


class CloseConfirmView(_StaffPrompt):
    """The private prompt that confirms closing a ticket without accepting or denying."""

    @discord.ui.button(label="Yes, close it", emoji="🔒", style=discord.ButtonStyle.danger, row=0)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Close the ticket with no decision and no role changes. Staff only."""
        await interaction.response.defer()
        assert isinstance(interaction.user, discord.Member)
        error = await self.bot.tickets.close(interaction.user, self.ticket)
        await self._finish(interaction, error, "🔒 Closing the ticket…")
