"""What an applicant sees: the panel button, the dropdown step and the application form."""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING

import discord

from ....bot.interactions import safe_reply
from ....core.limits import (
    BUTTON_LABEL_MAX,
    MESSAGE_MAX,
    MODAL_TITLE_MAX,
    SELECT_MAX_OPTIONS,
    SELECT_OPTION_LABEL_MAX,
    SELECT_PLACEHOLDER_MAX,
    TEXT_INPUT_LABEL_MAX,
    TEXT_INPUT_PLACEHOLDER_MAX,
)
from ....core.templating import render
from ..constants import ISSUE_CHOICE_EMOJI
from ..context import load_context
from ..settings import MAX_CHOICE_QUESTIONS, MAX_FORM_QUESTIONS, TicketSettings, custom_emoji_id, custom_emoji_name
from .common import PANEL_ID, custom_id

if TYPE_CHECKING:
    from ....bot.client import JoltBot

log = logging.getLogger(__name__)

NOT_SET_UP = "Applications aren't set up on this server yet. Please let a staff member know."
FORM_TIMEOUT_SECONDS = 900  # how long an applicant has to fill in the form
CHOICE_STEP_TIMEOUT_SECONDS = 300  # how long the dropdown step stays usable
CONTINUE_ROW = 4  # the last of a message's five rows; the dropdowns take the rows above it


class ApplicationModal(discord.ui.Modal):
    """The application form. Built from the server's own questions (max 5)."""

    def __init__(self, bot: JoltBot, settings: TicketSettings, choices: list[dict[str, str]] | None = None) -> None:
        """Build the form's text boxes from the server's questions.

        Args:
            bot: Running bot.
            settings: The server's ticket settings (form title and questions).
            choices: Answers already given in the dropdown step, if the server has one.
        """
        super().__init__(title=settings.messages.form_title[:MODAL_TITLE_MAX], timeout=FORM_TIMEOUT_SECONDS)
        self.bot = bot
        # Answers already given in the dropdown step (ChoiceStepView); stored after the typed ones.
        self.choices: list[dict[str, str]] = list(choices or [])
        self.inputs: list[tuple[str, discord.ui.TextInput]] = []
        for question in settings.form_questions[:MAX_FORM_QUESTIONS]:
            text_input = discord.ui.TextInput(
                label=question.label[:TEXT_INPUT_LABEL_MAX],
                placeholder=question.placeholder[:TEXT_INPUT_PLACEHOLDER_MAX] or None,
                required=question.required,
                style=discord.TextStyle.paragraph if question.long else discord.TextStyle.short,
                max_length=question.max_length,
            )
            self.add_item(text_input)
            self.inputs.append((question.label, text_input))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Open the ticket when the applicant submits the form.

        Anyone who could open the form may submit it. Settings are loaded again here,
        because an admin may have changed them while the applicant was typing.

        Args:
            interaction: The form submission.
        """
        member = interaction.user
        assert isinstance(member, discord.Member)
        # Discord wants an answer within 3 seconds; creating the ticket channel can take longer.
        await interaction.response.defer(ephemeral=True, thinking=True)
        ctx = await load_context(self.bot, member.guild.id)
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
        await interaction.followup.send(text[:MESSAGE_MAX], ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        """Log an unexpected failure and ask the applicant to try again."""
        log.exception("Application form failed", exc_info=error)
        await safe_reply(interaction, "Something went wrong submitting your application. Please try again.")


CHOICE_STEP_TEXT = "**Step 1 of 2.** Pick an option from each list, then press **Continue** to fill in the form."


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
        super().__init__(timeout=CHOICE_STEP_TIMEOUT_SECONDS)
        self.bot = bot
        self.settings = settings
        self.questions = settings.choice_questions[:MAX_CHOICE_QUESTIONS]
        self.picks: dict[int, int] = {}  # question number -> option number
        self.selects: list[discord.ui.Select] = []
        self.message: discord.InteractionMessage | None = None
        self.has_icons = False
        for number, question in enumerate(self.questions):
            options = []
            for i, option in enumerate(question.options[:SELECT_MAX_OPTIONS]):
                emoji = option_emoji(guild, option.emoji) if icons else None
                self.has_icons = self.has_icons or emoji is not None
                options.append(
                    discord.SelectOption(label=option.label[:SELECT_OPTION_LABEL_MAX], value=str(i), emoji=emoji)
                )
            select = discord.ui.Select(
                placeholder=(question.label if question.required else f"{question.label} (optional)")[
                    :SELECT_PLACEHOLDER_MAX
                ],
                min_values=1 if question.required else 0,
                max_values=1,
                options=options,
                row=number,
            )
            select.callback = functools.partial(self._picked, number)
            self.add_item(select)
            self.selects.append(select)
        self.continue_button = discord.ui.Button(label="Continue", style=discord.ButtonStyle.success, row=CONTINUE_ROW)
        self.continue_button.callback = self._continue
        self.add_item(self.continue_button)

    async def _picked(self, number: int, interaction: discord.Interaction) -> None:
        """Remember, or clear, the applicant's pick for one dropdown.

        Args:
            number: Which question the dropdown belongs to.
            interaction: The dropdown interaction.
        """
        values = self.selects[number].values
        if values:
            self.picks[number] = int(values[0])
        else:  # an optional dropdown was cleared again
            self.picks.pop(number, None)
        # There is nothing to show, but Discord reports "interaction failed" unless every click is answered.
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
        """Open the application form, once every required dropdown has a pick."""
        missing = [q.label for number, q in enumerate(self.questions) if q.required and number not in self.picks]
        if missing:
            await interaction.response.send_message(
                "Please pick an option for: " + ", ".join(f"**{label}**" for label in missing), ephemeral=True
            )
            return
        # Not stopped here: if they close the form by accident, Continue still works.
        await interaction.response.send_modal(ApplicationModal(self.bot, self.settings, self.answers()))

    async def on_timeout(self) -> None:
        """Replace the dropdowns with a note when the step expires."""
        if self.message is not None:
            try:
                await self.message.edit(content="This timed out. Click the panel button again to apply.", view=None)
            except discord.HTTPException as e:
                log.debug("Couldn't mark the dropdown step as timed out: %s", e)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        """Log an unexpected failure and ask the applicant to start again."""
        log.exception("Application dropdowns failed", exc_info=error)
        await safe_reply(interaction, "Something went wrong. Please click the panel button again.")


class PanelView(discord.ui.View):
    """The "Open Application" button on the panel message."""

    def __init__(self, bot: JoltBot, *, label: str = "Open Application", legacy: bool = False) -> None:
        """Create the panel's one button.

        Args:
            bot: Running bot.
            label: The button text, from the server's settings. It matters only when a
                panel is posted: a click is matched by ``custom_id``, whatever the label.
            legacy: True registers the old bot's button ID instead of JoltBot's.
        """
        # timeout=None and a fixed custom_id are what make a view persistent: once registered
        # with bot.add_view, its button works on every old panel message, also after a restart.
        super().__init__(timeout=None)
        self.bot = bot
        button = discord.ui.Button(
            label=label[:BUTTON_LABEL_MAX],
            emoji="📝",
            style=discord.ButtonStyle.success,
            custom_id=custom_id(PANEL_ID, legacy),
        )
        button.callback = self.open_application
        self.add_item(button)

    async def open_application(self, interaction: discord.Interaction) -> None:
        """Start an application when someone clicks the panel button.

        Any member of the server may click. The click is turned away with a private
        reply if applications are off or not set up, if the member already holds an
        accept role, or if they already have a ticket. Otherwise they get the dropdown
        step (if the server has one) or the form.

        Args:
            interaction: The button click.
        """
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
            await interaction.response.send_message(text[:MESSAGE_MAX], ephemeral=True)
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
                    ISSUE_CHOICE_EMOJI,
                    "Discord refused an icon on one of the dropdown questions, so applicants see the options "
                    "without icons. Check the icons under Tickets → Dropdown questions on the dashboard.",
                )
            else:
                if view.has_icons:
                    await self.bot.resolve_issue(member.guild.id, ISSUE_CHOICE_EMOJI)
            try:
                view.message = await interaction.original_response()  # only needed to tidy up on timeout
            except (discord.HTTPException, OSError) as e:
                log.debug("Couldn't fetch the dropdown message; it won't be tidied up on timeout: %s", e)
            return
        await interaction.response.send_modal(ApplicationModal(self.bot, ctx.settings))

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        """Log an unexpected failure and ask the member to try again."""
        log.exception("Panel button failed", exc_info=error)
        await safe_reply(interaction, "Something went wrong. Please try again in a moment.")
