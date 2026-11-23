"""The alliance menu, the join form, and the Accept / Reject buttons.

AlliancePanelView and RequestDecisionView are *persistent*: they are registered
once at startup with fixed custom_ids and timeout=None, so they keep working on
every message ever posted, also after a restart. Neither stores anything in
memory: each click looks up the server's settings, and the request by the ID of
the message that was clicked.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

import discord

from ...bot.interactions import safe_reply
from ...core.limits import MODAL_TITLE_MAX, SELECT_MAX_OPTIONS, SELECT_OPTION_LABEL_MAX
from ...core.models import AllianceRequest
from . import store
from .constants import STATUS_PENDING
from .context import load_context
from .settings import Alliance

if TYPE_CHECKING:
    from ...bot.client import JoltBot

log = logging.getLogger(__name__)

# Discord sends these back on every click, which is how a click on an old message finds its
# handler after a restart. They are part of every message already posted: never change them.
CHOOSE_ID = "joltbot:alliances:choose"
ACCEPT_ID, REJECT_ID = "joltbot:alliances:accept", "joltbot:alliances:reject"

NOT_SET_UP = "Alliance requests aren't set up on this server yet. Please let a staff member know."
GONE = "This request no longer exists."
FORM_TIMEOUT_SECONDS = 900  # how long a member has to fill in the join form
REASON_FORM_TIMEOUT_SECONDS = 600  # how long a reviewer has to type a reject reason
INGAME_NAME_MAX = 50
GOVERNOR_ID_MAX = 20
REJECT_REASON_MAX = 500  # the reason must also fit in the DM and the embeds


def already_handled(request: AllianceRequest) -> str:
    """What a reviewer is told when someone else got to the request first."""
    if request.decided_by:
        return f"Already handled by <@{request.decided_by}>."
    return "This request is already closed."


class JoinRequestModal(discord.ui.Modal):
    """The form a member fills in after picking an alliance.

    Leadership needs both answers to find the member on the in-game roster.

    Args:
        bot: Running bot.
        alliance_role_id: The picked alliance's member role (its identity).
        alliance_name: Its name, for the form's title.
    """

    ingame_name: discord.ui.TextInput[JoinRequestModal] = discord.ui.TextInput(
        label="In-game name", min_length=1, max_length=INGAME_NAME_MAX
    )
    governor_id: discord.ui.TextInput[JoinRequestModal] = discord.ui.TextInput(
        label="Governor ID",
        placeholder="The number on your in-game profile",
        min_length=1,
        max_length=GOVERNOR_ID_MAX,
    )

    def __init__(self, bot: JoltBot, alliance_role_id: int, alliance_name: str) -> None:
        """Title the form after the alliance that was picked."""
        super().__init__(title=f"Join {alliance_name}"[:MODAL_TITLE_MAX], timeout=FORM_TIMEOUT_SECONDS)
        self.bot = bot
        self.alliance_role_id = alliance_role_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Send the request to the alliance's leadership and confirm privately.

        Anyone who could open the form may submit it. Whether they may still
        ask (no other request waiting, no cooldown) is checked again here.

        Args:
            interaction: The form submission.
        """
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("This only works inside a server.", ephemeral=True)
            return
        name = self.ingame_name.value.strip()
        governor = "".join(self.governor_id.value.split())  # "1234 5678" is fine
        if not name or not (governor.isascii() and governor.isdigit()):
            await interaction.response.send_message(
                "❌ The Governor ID is the number on your in-game profile, digits only. "
                "Pick your alliance from the menu again to retry.",
                ephemeral=True,
            )
            return
        # Discord wants an answer within 3 seconds; posting the request can take longer.
        await interaction.response.defer(ephemeral=True, thinking=True)
        text = await self.bot.alliances.create_request(member, self.alliance_role_id, name, governor)
        await interaction.followup.send(text, ephemeral=True)

    # The ignore: a form's on_error takes no ``item`` (discord.py's own Modal is declared the
    # same way), while the View class that Modal is built on does.
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[override]
        """Log an unexpected failure and ask the member to try again."""
        log.exception("Alliance join form failed", exc_info=error)
        await safe_reply(interaction, "Something went wrong sending your request. Please try again.")


class AlliancePanelView(discord.ui.View):
    """The "choose your alliance" menu on the panel message.

    Args:
        bot: Running bot.
        alliances: The server's alliances, for the menu's options. They matter
            only when a panel is posted: a pick is matched by ``custom_id`` and
            checked against the settings as they are at that moment.
    """

    def __init__(self, bot: JoltBot, alliances: Sequence[Alliance] = ()) -> None:
        """Create the menu with one option per alliance."""
        # timeout=None and a fixed custom_id are what make a view persistent: once registered
        # with bot.add_view, the menu works on every old panel message, also after a restart.
        super().__init__(timeout=None)
        self.bot = bot
        options = [
            # The value is the alliance's member role: its identity, which survives a rename.
            discord.SelectOption(label=alliance.name[:SELECT_OPTION_LABEL_MAX], value=str(alliance.member_role_id))
            for alliance in alliances[:SELECT_MAX_OPTIONS]
        ]
        # Discord refuses a menu without options. The copy registered at startup is never
        # posted, so it only needs a stand-in.
        options = options or [discord.SelectOption(label="-", value="0")]
        select: discord.ui.Select[AlliancePanelView] = discord.ui.Select(
            custom_id=CHOOSE_ID, placeholder="Choose your alliance", min_values=1, max_values=1, options=options
        )
        # Assigning ``callback`` is discord.py's way to give a component built in code its
        # handler. A type checker sees a method being replaced, hence the ignores in this file.
        select.callback = self.choose  # type: ignore[method-assign]
        self.add_item(select)

    async def choose(self, interaction: discord.Interaction) -> None:
        """Open the join form when a member picks an alliance.

        Any member of the server may pick. The pick is turned away with a
        private reply if requests are off or not set up, if they already belong
        to that alliance, if they have another request waiting, or if that
        alliance rejected them recently.

        Args:
            interaction: The menu interaction.
        """
        member = interaction.user
        if not isinstance(member, discord.Member) or interaction.guild is None:
            await interaction.response.send_message("This only works inside a server.", ephemeral=True)
            return
        # Discord gives us only 3 seconds to answer, and a form can't be opened after a
        # "working on it" reply, so keep this part quick (a few small DB reads).
        ctx = await load_context(self.bot, member.guild.id)
        if not ctx.ready:
            await interaction.response.send_message(NOT_SET_UP, ephemeral=True)
            return
        # Read from the click itself, not from the Select object: this one view serves every server.
        data = cast("dict[str, Any]", interaction.data or {})
        values = data.get("values") or []
        picked = str(values[0]) if values else ""
        alliance = ctx.settings.alliance_for(int(picked)) if picked.isascii() and picked.isdigit() else None
        if alliance is None:
            await interaction.response.send_message(
                "That alliance is no longer available here. Ask an admin to post a fresh menu.", ephemeral=True
            )
        elif problem := await self.bot.alliances.apply_problem(member, alliance, ctx.settings):
            await interaction.response.send_message(problem, ephemeral=True)
        else:
            await interaction.response.send_modal(JoinRequestModal(self.bot, alliance.member_role_id, alliance.name))
        await self._reset_menu(interaction, ctx.settings.alliances)

    async def _reset_menu(self, interaction: discord.Interaction, alliances: Sequence[Alliance]) -> None:
        """Put the menu back to "Choose your alliance" after a pick.

        Discord keeps showing the picked option to that member, and picking the
        shown option again does nothing. Without this, someone who closed the
        form by accident could not reopen it for the same alliance.
        """
        if interaction.message is None:
            return
        try:
            await interaction.message.edit(view=AlliancePanelView(self.bot, alliances))
        except (discord.HTTPException, OSError) as e:
            log.debug("Couldn't reset the alliance menu: %s", e)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        """Log an unexpected failure and ask the member to try again."""
        log.exception("Alliance menu failed", exc_info=error)
        await safe_reply(interaction, "Something went wrong. Please try again in a moment.")


class RejectReasonModal(discord.ui.Modal, title="Reject request"):
    """The form where a reviewer may type why a request is rejected.

    Args:
        bot: Running bot.
        request_id: The request being rejected.
    """

    reason: discord.ui.TextInput[RejectReasonModal] = discord.ui.TextInput(
        label="Reason (optional, sent to the member)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=REJECT_REASON_MAX,
    )

    def __init__(self, bot: JoltBot, request_id: int) -> None:
        """Remember which request the reason is for."""
        super().__init__(timeout=REASON_FORM_TIMEOUT_SECONDS)
        self.bot = bot
        self.request_id = request_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Reject the request. The reviewer is checked again: the form can stay open for minutes."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        request = await store.get(self.bot.db, self.request_id)
        if request is None or interaction.guild is None:
            await interaction.followup.send(GONE, ephemeral=True)
            return
        # The cast: inside a server, Discord always sends whoever clicked as a Member.
        reviewer = cast(discord.Member, interaction.user)
        text = await self.bot.alliances.reject(interaction.guild, reviewer, request, self.reason.value)
        await interaction.followup.send(text, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[override]
        """Log an unexpected failure and ask the reviewer to try again."""
        log.exception("Reject reason form failed", exc_info=error)
        await safe_reply(interaction, "Something went wrong. Please try again in a moment.")


class RequestDecisionView(discord.ui.View):
    """Accept / Reject buttons under every join request.

    Args:
        bot: Running bot.
        disabled: True draws the buttons greyed out, for a request that has been decided.
    """

    def __init__(self, bot: JoltBot, *, disabled: bool = False) -> None:
        """Create the two buttons."""
        super().__init__(timeout=None)
        self.bot = bot
        for button_id, label, emoji, style, callback in (
            (ACCEPT_ID, "Accept", "✅", discord.ButtonStyle.success, self.accept),
            (REJECT_ID, "Reject", "❌", discord.ButtonStyle.danger, self.reject),
        ):
            button: discord.ui.Button[RequestDecisionView] = discord.ui.Button(
                label=label, emoji=emoji, style=style, custom_id=button_id, disabled=disabled
            )
            button.callback = callback  # type: ignore[method-assign]
            self.add_item(button)

    async def _pending_request(self, interaction: discord.Interaction) -> AllianceRequest | None:
        """The waiting request this message belongs to.

        Returns:
            The request, or ``None`` after telling the clicker it is gone or who already handled it.
        """
        message = interaction.message
        request = await store.get_by_message(self.bot.db, message.id) if message is not None else None
        # The guild check stops a request of one server ever being acted on from another.
        if request is None or request.guild_id != interaction.guild_id:
            await interaction.response.send_message(GONE, ephemeral=True)
            return None
        if request.status != STATUS_PENDING:
            await interaction.response.send_message(already_handled(request), ephemeral=True)
            return None
        return request

    async def accept(self, interaction: discord.Interaction) -> None:
        """Accept was clicked: give the member the alliance's role, if the clicker may decide.

        Who can use it:
            A member holding one of that alliance's reviewer roles, or an override
            role. Checked here on every click (``AllianceManager.accept``); being
            able to see the channel is not enough. Nobody can accept their own request.
        """
        request = await self._pending_request(interaction)
        if request is None or interaction.guild is None:
            return
        # Answer the click first: the role change, DM and log can take longer than Discord's 3 seconds.
        await interaction.response.defer(ephemeral=True, thinking=True)
        # The cast: inside a server, Discord always sends whoever clicked as a Member.
        reviewer = cast(discord.Member, interaction.user)
        text = await self.bot.alliances.accept(interaction.guild, reviewer, request)
        await interaction.followup.send(text, ephemeral=True)

    async def reject(self, interaction: discord.Interaction) -> None:
        """Reject was clicked: ask the reviewer for an optional reason.

        Who can use it:
            The same people as Accept, checked here before the form opens and
            again when it is submitted.
        """
        request = await self._pending_request(interaction)
        if request is None or interaction.guild is None:
            return
        if problem := await self.bot.alliances.review_problem(interaction.guild, interaction.user, request):
            await interaction.response.send_message(problem, ephemeral=True)
            return
        # Not deferred: Discord only lets a form be opened as the very first answer to a click.
        await interaction.response.send_modal(RejectReasonModal(self.bot, request.id))

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        """Log an unexpected failure and ask the reviewer to try again."""
        log.exception("Alliance request button failed", exc_info=error)
        await safe_reply(interaction, "Something went wrong. Please try again in a moment.")
