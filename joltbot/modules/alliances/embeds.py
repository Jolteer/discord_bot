"""Every embed and DM the alliance module sends, in one place.

Each is built from the stored request alone, so a request's message can be
redrawn at any time, also after a restart or after the member left.
"""

from __future__ import annotations

from datetime import datetime

import discord

from ...core.limits import EMBED_DESCRIPTION_MAX, EMBED_TITLE_MAX
from ...core.models import AllianceRequest
from ...core.templating import render

# clip() and ts() are the ticket module's small text helpers; they are not ticket-specific.
from ..tickets.embeds import clip, ts
from .constants import STATUS_ACCEPTED, STATUS_CANCELLED, STATUS_CLOSED, STATUS_REJECTED
from .settings import AllianceSettings

# How each final status is shown: (title for the log, colour).
STATUS_STYLE: dict[str, tuple[str, discord.Colour]] = {
    STATUS_ACCEPTED: ("✅ Accepted", discord.Colour.green()),
    STATUS_REJECTED: ("❌ Rejected", discord.Colour.red()),
    STATUS_CLOSED: ("🚪 Member left the server", discord.Colour.dark_grey()),
    STATUS_CANCELLED: ("🚫 Cancelled", discord.Colour.light_grey()),
}
NO_REASON = "No reason was given."


def _date(moment: datetime | None) -> str:
    """A date with "how long ago" next to it, e.g. the age of an account. ``-`` for no date."""
    return f"{ts(moment, 'D')} ({ts(moment, 'R')})" if moment else "-"


def panel_embed(settings: AllianceSettings, guild: discord.Guild, colour: discord.Colour) -> discord.Embed:
    """The public panel: the server's own title and text, above the alliance menu.

    Args:
        settings: The server's alliance settings (panel title and text).
        guild: The server, for its name and icon.
        colour: The server's embed colour.

    Returns:
        The embed for the panel message.
    """
    embed = discord.Embed(
        title=render(settings.panel_title, server=guild.name)[:EMBED_TITLE_MAX],
        description=render(settings.panel_description, server=guild.name)[:EMBED_DESCRIPTION_MAX],
        colour=colour,
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text="One request at a time. Only that alliance's leadership can accept it.")
    return embed


def _decision_line(request: AllianceRequest) -> str | None:
    """What happened to a request, for its own message. ``None`` while it is still waiting."""
    when = ts(request.decided_at)
    if request.status == STATUS_ACCEPTED:
        return f"✅ Accepted by <@{request.decided_by}> • {when}"
    if request.status == STATUS_REJECTED:
        return f"❌ Rejected by <@{request.decided_by}> • {when}"
    if request.status == STATUS_CANCELLED:
        return f"🚫 Cancelled by <@{request.decided_by}> • {when}"
    if request.status == STATUS_CLOSED:
        return f"🚪 Closed: the member left the server before a decision • {when}"
    return None


def request_embed(request: AllianceRequest, colour: discord.Colour) -> discord.Embed:
    """The message leadership sees in the alliance's request channel.

    Shows who is asking, the details to check against the in-game roster, how
    old the Discord account is and when they joined the server. Once decided,
    it also says who decided and when.

    Args:
        request: The stored request.
        colour: The server's embed colour, used while the request is waiting.

    Returns:
        The embed posted with the Accept / Reject buttons.
    """
    _, decided_colour = STATUS_STYLE.get(request.status, ("", colour))
    embed = discord.Embed(
        title=clip(f"Join request: {request.alliance_name}", EMBED_TITLE_MAX),
        colour=decided_colour,
        timestamp=request.created_at,
    )
    embed.add_field(
        name="Member", value=f"<@{request.user_id}>\n`{request.username}` • `{request.user_id}`", inline=False
    )
    embed.add_field(name="In-game name", value=clip(request.ingame_name))
    embed.add_field(name="Governor ID", value=clip(request.governor_id))
    # A Discord ID contains the moment the account was made, so this needs no lookup.
    embed.add_field(name="Account created", value=_date(discord.utils.snowflake_time(request.user_id)), inline=False)
    embed.add_field(name="Joined server", value=_date(request.member_joined_at), inline=False)
    if decision := _decision_line(request):
        embed.add_field(name="Decision", value=decision, inline=False)
    if request.status == STATUS_REJECTED and request.reason:
        embed.add_field(name="Reason", value=clip(request.reason), inline=False)
    embed.set_footer(text=f"Request #{request.id}")
    return embed


def log_embed(request: AllianceRequest, notes: list[str]) -> discord.Embed:
    """The permanent record of a decision, posted in the log channel.

    Args:
        request: The decided (or closed) request.
        notes: Anything that didn't go to plan, e.g. a DM that couldn't be delivered.

    Returns:
        The embed, titled and coloured by the outcome.
    """
    title, colour = STATUS_STYLE.get(request.status, ("Closed", discord.Colour.light_grey()))
    embed = discord.Embed(
        title=f"Alliance request #{request.id} - {title}", colour=colour, timestamp=request.decided_at
    )
    embed.add_field(name="Member", value=f"<@{request.user_id}>\n`{request.username}` • `{request.user_id}`")
    embed.add_field(name="Alliance", value=f"<@&{request.alliance_role_id}>\n{clip(request.alliance_name, 200)}")
    embed.add_field(name="Decided by", value=f"<@{request.decided_by}>" if request.decided_by else "- (automatic)")
    embed.add_field(name="In-game name", value=clip(request.ingame_name))
    embed.add_field(name="Governor ID", value=clip(request.governor_id))
    embed.add_field(name="Requested", value=ts(request.created_at))
    if request.status == STATUS_REJECTED:
        embed.add_field(name="Reason", value=clip(request.reason or NO_REASON), inline=False)
    if notes:
        embed.add_field(name="Notes", value=clip("\n".join(f"• {note}" for note in notes)), inline=False)
    return embed


def accepted_dm(request: AllianceRequest, guild: discord.Guild) -> discord.Embed:
    """The DM a member gets when their request is accepted."""
    text = (
        f"✅ Your request to join **{request.alliance_name}** in **{guild.name}** was accepted. "
        "You now have access to its channels."
    )
    return discord.Embed(description=text[:EMBED_DESCRIPTION_MAX], colour=discord.Colour.green())


def rejected_dm(request: AllianceRequest, guild: discord.Guild, retry_at: datetime | None) -> discord.Embed:
    """The DM a member gets when their request is rejected.

    Args:
        request: The rejected request, holding the reason if one was given.
        guild: The server, for its name.
        retry_at: When they may ask this alliance again, or ``None`` if there is no wait.
    """
    text = (
        f"❌ Your request to join **{request.alliance_name}** in **{guild.name}** was not accepted.\n\n"
        f"**Reason:** {request.reason or NO_REASON}"
    )
    if retry_at is not None:
        text += f"\n\nYou can ask this alliance again {ts(retry_at, 'R')}."
    return discord.Embed(description=text[:EMBED_DESCRIPTION_MAX], colour=discord.Colour.red())


def cancelled_dm(request: AllianceRequest, guild: discord.Guild) -> discord.Embed:
    """The DM a member gets when an admin cancels their request."""
    text = (
        f"Your request to join **{request.alliance_name}** in **{guild.name}** was cancelled by the server's staff. "
        "You can send a new one from the alliance menu."
    )
    return discord.Embed(description=text[:EMBED_DESCRIPTION_MAX], colour=discord.Colour.light_grey())
