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

import functools
import logging
from typing import TYPE_CHECKING

import discord

from ...core.models import Ticket
from ...core.templating import render
from . import store
from .checks import is_staff
from .context import load_context
from .settings import TicketSettings, custom_emoji_id, custom_emoji_name

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
        await interaction.response.send_message(text[:2000], ephemeral=True)
        return False
    return True


# ---------------------------------------------------------------- applicant side


class ApplicationModal(discord.ui.Modal):
    """The application form. Built from the server's own questions (max 5)."""

    def __init__(self, bot: JoltBot, settings: TicketSettings, choices: list[dict[str, str]] | None = None) -> None:
        super().__init__(title=settings.messages.form_title[:45], timeout=900)
        self.bot = bot
        # Answers already given in the dropdown step (ChoiceStepView); stored after the typed ones.
        self.choices: list[dict[str, str]] = list(choices or [])
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
        answers += self.choices
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


CHOICE_STEP_TEXT = "**Step 1 of 2.** Pick an option from each list, then press **Continue** to fill in the form."


CHOICE_EMOJI_ISSUE = "tickets.choice_emoji"


def option_emoji(guild: discord.Guild | None, setting: str | None) -> discord.PartialEmoji | str | None:
    """Turn a dropdown option's icon setting into something Discord can show.

    Args:
        guild: The server whose own emojis a name refers to.
        setting: ``ChoiceOption.emoji``: a normal emoji, the name of one of the
            server's emojis (``infantry`` or ``:infantry:``), or Discord's full
            ``<:name:id>`` form.

    Returns:
        The emoji, or ``None`` for no icon. A named emoji the server no longer
        has gives ``None``: the option then simply shows without an icon.
    """
    if not setting:
        return None
    if name := custom_emoji_name(setting):
        found = discord.utils.get(guild.emojis, name=name) if guild is not None else None
        if found is None:
            return None
        return discord.PartialEmoji(name=found.name, id=found.id, animated=bool(found.animated))
    if custom_emoji_id(setting):
        return discord.PartialEmoji.from_str(setting.strip())
    return setting.strip()  # a normal emoji


class ChoiceStepView(discord.ui.View):
    """Step 1 of applying on a server with dropdown questions: one dropdown per question.

    Shown privately to the applicant after they click the panel button.
    **Continue** opens the form (``ApplicationModal``) and hands it the picks.
    Discord can also put dropdowns inside a form, but a form holds 5 items in
    total, and servers may already use all 5 for typed questions.

    Not persistent: it lives for 5 minutes in memory. After a restart or a
    timeout the applicant simply clicks the panel button again.
    """

    def __init__(
        self, bot: JoltBot, settings: TicketSettings, guild: discord.Guild | None = None, *, icons: bool = True
    ) -> None:
        """
        Args:
            bot: Running bot.
            settings: The server's ticket settings (its dropdown questions).
            guild: The server, for looking up its own emojis by name.
            icons: False leaves every icon out (used when Discord refused one).
        """
        super().__init__(timeout=300)
        self.bot = bot
        self.settings = settings
        self.questions = settings.choice_questions[:4]
        self.picks: dict[int, int] = {}  # question number -> option number
        self.selects: list[discord.ui.Select] = []
        self.message: discord.InteractionMessage | None = None
        self.has_icons = False
        for number, question in enumerate(self.questions):
            options = []
            for i, option in enumerate(question.options[:25]):
                emoji = option_emoji(guild, option.emoji) if icons else None
                self.has_icons = self.has_icons or emoji is not None
                options.append(discord.SelectOption(label=option.label[:100], value=str(i), emoji=emoji))
            select = discord.ui.Select(
                placeholder=(question.label if question.required else f"{question.label} (optional)")[:150],
                min_values=1 if question.required else 0,
                max_values=1,
                options=options,
                row=number,
            )
            select.callback = functools.partial(self._picked, number)
            self.add_item(select)
            self.selects.append(select)
        self.continue_button = discord.ui.Button(label="Continue", style=discord.ButtonStyle.success, row=4)
        self.continue_button.callback = self._continue
        self.add_item(self.continue_button)

    async def _picked(self, number: int, interaction: discord.Interaction) -> None:
        values = self.selects[number].values
        if values:
            self.picks[number] = int(values[0])
        else:  # an optional dropdown was cleared again
            self.picks.pop(number, None)
        await interaction.response.defer()

    def answers(self) -> list[dict[str, str]]:
        """The picks in the shape ticket answers are stored in.

        Returns:
            One ``{"label", "value"}`` per answered question, plus ``"role_id"``
            when the picked option gives a role.
        """
        answers = []
        for number, question in enumerate(self.questions):
            if number not in self.picks:
                continue
            option = question.options[self.picks[number]]
            answer = {"label": question.label, "value": option.label}
            if option.role_id is not None:
                answer["role_id"] = str(option.role_id)
            answers.append(answer)
        return answers

    async def _continue(self, interaction: discord.Interaction) -> None:
        missing = [q.label for number, q in enumerate(self.questions) if q.required and number not in self.picks]
        if missing:
            await interaction.response.send_message(
                "Please pick an option for: " + ", ".join(f"**{label}**" for label in missing), ephemeral=True
            )
            return
        # Not stopped here: if they close the form by accident, Continue still works.
        await interaction.response.send_modal(ApplicationModal(self.bot, self.settings, self.answers()))

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(content="This timed out. Click the panel button again to apply.", view=None)
            except discord.HTTPException:
                pass

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.exception("Application dropdowns failed", exc_info=error)
        await safe_reply(interaction, "Something went wrong. Please click the panel button again.")


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
        if ctx.settings.choice_questions:
            # Dropdown questions first; its Continue button opens the form.
            view = ChoiceStepView(self.bot, ctx.settings, member.guild)
            try:
                await interaction.response.send_message(CHOICE_STEP_TEXT, view=view, ephemeral=True)
            except discord.HTTPException as e:
                if e.status != 400 or not view.has_icons:
                    raise
                # Discord rejects the whole message over one emoji it doesn't accept. Applying
                # matters more than icons, so show the dropdowns without them and tell the admins.
                view.stop()
                view = ChoiceStepView(self.bot, ctx.settings, member.guild, icons=False)
                await interaction.response.send_message(CHOICE_STEP_TEXT, view=view, ephemeral=True)
                await self.bot.report_issue(
                    member.guild.id,
                    CHOICE_EMOJI_ISSUE,
                    "Discord refused an icon on one of the dropdown questions, so applicants see the options "
                    "without icons. Check the icons under Tickets → Dropdown questions on the dashboard.",
                )
            else:
                if view.has_icons:
                    await self.bot.resolve_issue(member.guild.id, CHOICE_EMOJI_ISSUE)
            try:
                view.message = await interaction.original_response()  # only needed to tidy up on timeout
            except (discord.HTTPException, OSError):
                pass
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
        return await staff_check(self.bot, interaction)

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
        picked, _ = ctx.settings.picked_roles(ticket.answers)
        if picked:
            roles = ", ".join(f"<@&{role_id}>" for role_id in dict.fromkeys(role_id for _, role_id in picked))
            lines.append(f"Their answers also give them: {roles}.")
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

    Every click on a prompt re-checks staff, like the ticket buttons do. A prompt
    stays open for minutes, and someone can lose their staff role in that time.
    """

    def __init__(self, bot: JoltBot, ticket: Ticket, timeout: float = 300) -> None:
        super().__init__(timeout=timeout)
        self.bot = bot
        self.ticket = ticket
        self.message: discord.Message | discord.WebhookMessage | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await staff_check(self.bot, interaction)

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
