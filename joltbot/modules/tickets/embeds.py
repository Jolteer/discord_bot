"""Every embed and DM the ticket module sends, in one place."""

from __future__ import annotations

from datetime import datetime

import discord

from ...core.limits import EMBED_DESCRIPTION_MAX, EMBED_FIELD_NAME_MAX, EMBED_FIELD_VALUE_MAX, EMBED_TITLE_MAX
from ...core.models import Ticket
from ...core.templating import render
from .constants import (
    OUTCOME_ACCEPTED,
    OUTCOME_CHANNEL_DELETED,
    OUTCOME_CLOSED,
    OUTCOME_DENIED,
    OUTCOME_LEFT,
)
from .settings import MAX_CHOICE_QUESTIONS, MAX_FORM_QUESTIONS, TicketSettings

OUTCOME_STYLE: dict[str, tuple[str, discord.Colour]] = {
    OUTCOME_ACCEPTED: ("✅ Accepted", discord.Colour.green()),
    OUTCOME_DENIED: ("❌ Denied", discord.Colour.red()),
    OUTCOME_CLOSED: ("🔒 Closed without decision", discord.Colour.light_grey()),
    OUTCOME_LEFT: ("🚪 Applicant left the server", discord.Colour.dark_grey()),
    OUTCOME_CHANNEL_DELETED: ("🗑️ Ticket channel was deleted manually", discord.Colour.dark_grey()),
}


# Shown for an outcome this code doesn't know (it shouldn't happen).
UNKNOWN_OUTCOME_STYLE = ("Closed", discord.Colour.light_grey())

MAX_ANSWERS = MAX_FORM_QUESTIONS + MAX_CHOICE_QUESTIONS
ANSWERS_BUDGET = 5200  # characters all answers may use together in one embed
MIN_ANSWER_CHARS = 16  # an answer is never cut shorter than this, even when the budget is spent
LOG_ANSWER_LIMIT = 300  # per answer in the log embed, which carries more fields than the ticket's own
CLOSING_REASON_LIMIT = 900  # the deny reason, inside the closing notice's description
LOG_REASON_LIMIT = 1000  # the deny reason, in its own field of the log embed


def ts(dt: datetime | None, style: str = "f") -> str:
    """A Discord timestamp tag, which every reader sees in their own time zone. ``-`` for no time."""
    return discord.utils.format_dt(dt, style) if dt else "-"


def clip(text: str | None, limit: int = EMBED_FIELD_VALUE_MAX) -> str:
    """Trim text to fit a Discord field. Empty text becomes ``-``: Discord rejects an empty field."""
    text = (text or "").strip() or "-"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def panel_embed(settings: TicketSettings, guild: discord.Guild, colour: discord.Colour) -> discord.Embed:
    """The public panel: the server's own title and text, above the Open Application button.

    Args:
        settings: The server's ticket settings (panel title and text).
        guild: The server, for its name and icon.
        colour: The server's embed colour.

    Returns:
        The embed for the panel message.
    """
    m = settings.messages
    embed = discord.Embed(
        title=render(m.panel_title, server=guild.name)[:EMBED_TITLE_MAX],
        description=render(m.panel_description, server=guild.name)[:EMBED_DESCRIPTION_MAX],
        colour=colour,
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text="One application per person at a time.")
    return embed


def _answer_fields(embed: discord.Embed, ticket: Ticket, limit: int = EMBED_FIELD_VALUE_MAX) -> None:
    """Add one field per answer to the embed, within a shared character budget."""
    # Discord refuses an embed over 6000 characters in total. Five long answers come close,
    # so the answers share a budget that leaves room for the embed's other fields.
    budget = ANSWERS_BUDGET
    for answer in (ticket.answers or [])[:MAX_ANSWERS]:
        name = clip(answer.get("label"), EMBED_FIELD_NAME_MAX)
        value = clip(answer.get("value"), max(min(limit, budget - len(name)), MIN_ANSWER_CHARS))
        budget -= len(name) + len(value)
        embed.add_field(name=name, value=value, inline=False)


def _outcome_style(ticket: Ticket) -> tuple[str, discord.Colour]:
    """The title and colour for a ticket's outcome. No outcome recorded counts as closed without a decision."""
    return OUTCOME_STYLE.get(ticket.outcome or OUTCOME_CLOSED, UNKNOWN_OUTCOME_STYLE)


def application_embed(member: discord.Member, ticket: Ticket, colour: discord.Colour) -> discord.Embed:
    """The first message in a new ticket: the applicant's answers and the age of their account.

    Args:
        member: The applicant.
        ticket: Their ticket, holding the answers.
        colour: The server's embed colour.

    Returns:
        The embed posted with the Accept / Deny / Close buttons.
    """
    embed = discord.Embed(title=f"Application - {member.display_name}", colour=colour, timestamp=ticket.created_at)
    embed.set_author(name=f"{member} • {member.id}", icon_url=member.display_avatar.url)
    _answer_fields(embed, ticket)
    embed.add_field(
        name="Discord account", value=f"{member.mention}\nCreated {ts(member.created_at, 'R')}", inline=False
    )
    embed.set_footer(text=f"Ticket #{ticket.id}")
    return embed


def closing_embed(ticket: Ticket, delay: int) -> discord.Embed:
    """The notice posted in a ticket channel when it starts closing.

    Args:
        ticket: The ticket, with its decision already saved.
        delay: Seconds until the channel is deleted, shown to whoever is reading.

    Returns:
        The embed, titled and coloured by the outcome.
    """
    title, colour = _outcome_style(ticket)
    lines = []
    if ticket.handled_by:
        lines.append(f"By <@{ticket.handled_by}>")
    if ticket.extra_role_id:
        lines.append(f"Extra role: <@&{ticket.extra_role_id}>")
    if ticket.reason:
        lines.append(f"Reason: {clip(ticket.reason, CLOSING_REASON_LIMIT)}")
    lines.append(f"\nSaving transcript… this channel will be deleted in **{delay}s**.")
    return discord.Embed(title=title, description="\n".join(lines), colour=colour)


def log_embed(ticket: Ticket, *, resumed: bool, transcript_ok: bool) -> discord.Embed:
    """The permanent record posted in the log channel.

    Shows who applied, who decided, when, the answers, the decision and any notes.

    Args:
        ticket: The closed (or closing) ticket.
        resumed: True if this was finished after a restart or a retry; noted in the embed.
        transcript_ok: False if no transcript could be made; noted in the embed.

    Returns:
        The embed, titled and coloured by the outcome.
    """
    title, colour = _outcome_style(ticket)
    embed = discord.Embed(title=f"Ticket #{ticket.id} - {title}", colour=colour, timestamp=ticket.closed_at)
    embed.add_field(name="Applicant", value=f"<@{ticket.user_id}>\n`{ticket.username}` • `{ticket.user_id}`")
    embed.add_field(name="Handled by", value=f"<@{ticket.handled_by}>" if ticket.handled_by else "- (automatic)")
    embed.add_field(name="Opened", value=ts(ticket.created_at))
    embed.add_field(name="Closed", value=ts(ticket.closed_at))
    # Shorter answers here, so the embed stays under Discord's 6000-character cap.
    _answer_fields(embed, ticket, limit=LOG_ANSWER_LIMIT)
    if ticket.outcome == OUTCOME_ACCEPTED and ticket.extra_role_id:
        embed.add_field(name="Extra role given", value=f"<@&{ticket.extra_role_id}>")
    if ticket.outcome == OUTCOME_DENIED:
        embed.add_field(name="Action", value="Kicked" if ticket.kicked else "Not kicked")
        embed.add_field(name="Reason", value=clip(ticket.reason, LOG_REASON_LIMIT), inline=False)
    notes = list(ticket.note_lines)
    if resumed:
        notes.append("Finished after a bot restart or retry.")
    if not transcript_ok:
        notes.append("No transcript: the ticket channel was already gone or unreadable.")
    if notes:
        embed.add_field(name="Notes", value=clip("\n".join(f"• {n}" for n in notes)), inline=False)
    return embed


def welcome_dm(
    settings: TicketSettings, guild: discord.Guild, member: discord.Member, extra_role: discord.Role | None
) -> discord.Embed:
    """The DM an accepted member gets, built from the server's own welcome text.

    Args:
        settings: The server's ticket settings (the welcome text).
        guild: The server, for its name.
        member: The accepted member.
        extra_role: The extra role staff picked on Accept, if any. Fills ``{role}`` and ``{role_line}``.

    Returns:
        The embed to send.
    """
    role_line = f"Role: **{extra_role.name}**\n" if extra_role else ""
    text = render(
        settings.messages.welcome_dm,
        user=member.mention,
        user_name=member.display_name,
        server=guild.name,
        role=extra_role.name if extra_role else "",
        role_line=role_line,
    )
    return discord.Embed(description=text[:EMBED_DESCRIPTION_MAX], colour=discord.Colour.green())


def deny_dm(
    settings: TicketSettings, guild: discord.Guild, member: discord.Member, reason: str, kicked: bool
) -> discord.Embed:
    """The DM a denied applicant gets: the server's deny text, then the "kicked" or "kept" ending.

    Args:
        settings: The server's ticket settings (the deny texts).
        guild: The server, for its name.
        member: The denied applicant.
        reason: What staff typed; fills ``{reason}``.
        kicked: True if they are about to be kicked, which picks the ending.

    Returns:
        The embed to send.
    """
    m = settings.messages
    text = render(m.deny_dm, user=member.mention, user_name=member.display_name, server=guild.name, reason=reason)
    text += render(m.deny_dm_kicked_suffix if kicked else m.deny_dm_kept_suffix, server=guild.name)
    return discord.Embed(description=text[:EMBED_DESCRIPTION_MAX], colour=discord.Colour.red())
