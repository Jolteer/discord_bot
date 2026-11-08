"""Every embed and DM the ticket module sends, in one place."""

from __future__ import annotations

from datetime import datetime

import discord

from ...core.models import Ticket
from ...core.templating import render
from .settings import TicketSettings

OUTCOME_STYLE: dict[str, tuple[str, discord.Colour]] = {
    "accepted": ("✅ Accepted", discord.Colour.green()),
    "denied": ("❌ Denied", discord.Colour.red()),
    "closed": ("🔒 Closed without decision", discord.Colour.light_grey()),
    "left": ("🚪 Applicant left the server", discord.Colour.dark_grey()),
    "channel_deleted": ("🗑️ Ticket channel was deleted manually", discord.Colour.dark_grey()),
}


def ts(dt: datetime | None, style: str = "f") -> str:
    return discord.utils.format_dt(dt, style) if dt else "-"


def clip(text: str | None, limit: int = 1024) -> str:
    text = (text or "").strip() or "-"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def panel_embed(settings: TicketSettings, guild: discord.Guild, colour: discord.Colour) -> discord.Embed:
    m = settings.messages
    embed = discord.Embed(
        title=render(m.panel_title, server=guild.name)[:256],
        description=render(m.panel_description, server=guild.name)[:4096],
        colour=colour,
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text="One application per person at a time.")
    return embed


def _answer_fields(embed: discord.Embed, ticket: Ticket, limit: int = 1024) -> None:
    for answer in (ticket.answers or [])[:5]:
        embed.add_field(name=clip(answer.get("label"), 256), value=clip(answer.get("value"), limit), inline=False)


def application_embed(member: discord.Member, ticket: Ticket, colour: discord.Colour) -> discord.Embed:
    embed = discord.Embed(title=f"Application - {member.display_name}", colour=colour, timestamp=ticket.created_at)
    embed.set_author(name=f"{member} • {member.id}", icon_url=member.display_avatar.url)
    _answer_fields(embed, ticket)
    embed.add_field(
        name="Discord account", value=f"{member.mention}\nCreated {ts(member.created_at, 'R')}", inline=False
    )
    embed.set_footer(text=f"Ticket #{ticket.id}")
    return embed


def closing_embed(ticket: Ticket, delay: int) -> discord.Embed:
    title, colour = OUTCOME_STYLE.get(ticket.outcome or "closed", ("Closed", discord.Colour.light_grey()))
    lines = []
    if ticket.handled_by:
        lines.append(f"By <@{ticket.handled_by}>")
    if ticket.extra_role_id:
        lines.append(f"Extra role: <@&{ticket.extra_role_id}>")
    if ticket.reason:
        lines.append(f"Reason: {clip(ticket.reason, 900)}")
    lines.append(f"\nSaving transcript… this channel will be deleted in **{delay}s**.")
    return discord.Embed(title=title, description="\n".join(lines), colour=colour)


def log_embed(ticket: Ticket, *, resumed: bool, transcript_ok: bool) -> discord.Embed:
    title, colour = OUTCOME_STYLE.get(ticket.outcome or "closed", ("Closed", discord.Colour.light_grey()))
    embed = discord.Embed(title=f"Ticket #{ticket.id} - {title}", colour=colour, timestamp=ticket.closed_at)
    embed.add_field(name="Applicant", value=f"<@{ticket.user_id}>\n`{ticket.username}` • `{ticket.user_id}`")
    embed.add_field(name="Handled by", value=f"<@{ticket.handled_by}>" if ticket.handled_by else "- (automatic)")
    embed.add_field(name="Opened", value=ts(ticket.created_at))
    embed.add_field(name="Closed", value=ts(ticket.closed_at))
    _answer_fields(embed, ticket, limit=300)  # shorter here so the embed stays under Discord's 6000-char cap
    if ticket.outcome == "accepted" and ticket.extra_role_id:
        embed.add_field(name="Extra role given", value=f"<@&{ticket.extra_role_id}>")
    if ticket.outcome == "denied":
        embed.add_field(name="Action", value="Kicked" if ticket.kicked else "Not kicked")
        embed.add_field(name="Reason", value=clip(ticket.reason, 1000), inline=False)
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
    role_line = f"Role: **{extra_role.name}**\n" if extra_role else ""
    text = render(
        settings.messages.welcome_dm,
        user=member.mention,
        user_name=member.display_name,
        server=guild.name,
        role=extra_role.name if extra_role else "",
        role_line=role_line,
    )
    return discord.Embed(description=text[:4096], colour=discord.Colour.green())


def deny_dm(
    settings: TicketSettings, guild: discord.Guild, member: discord.Member, reason: str, kicked: bool
) -> discord.Embed:
    m = settings.messages
    text = render(m.deny_dm, user=member.mention, user_name=member.display_name, server=guild.name, reason=reason)
    text += render(m.deny_dm_kicked_suffix if kicked else m.deny_dm_kept_suffix, server=guild.name)
    return discord.Embed(description=text[:4096], colour=discord.Colour.red())
