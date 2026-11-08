"""All embeds in one place so the look is consistent and easy to restyle."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from ..config import Config
    from ..database import Ticket

BRAND = discord.Colour.from_rgb(201, 162, 39)  # gold
OUTCOME_STYLE: dict[str, tuple[str, discord.Colour]] = {
    "accepted": ("✅ Accepted", discord.Colour.green()),
    "denied": ("❌ Denied", discord.Colour.red()),
    "closed": ("🔒 Closed without decision", discord.Colour.light_grey()),
    "left": ("🚪 Applicant left the server", discord.Colour.dark_grey()),
    "channel_deleted": ("🗑️ Ticket channel was deleted manually", discord.Colour.dark_grey()),
}


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def fmt(template: str, **values: object) -> str:
    """str.format that leaves unknown {placeholders} alone instead of crashing."""
    return template.format_map(_SafeDict({k: str(v) for k, v in values.items()}))


def ts(dt: datetime | None, style: str = "f") -> str:
    return discord.utils.format_dt(dt, style) if dt else "—"


def clip(text: str | None, limit: int = 1024) -> str:
    text = (text or "").strip() or "—"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def panel_embed(cfg: "Config", guild: discord.Guild) -> discord.Embed:
    e = discord.Embed(
        title=cfg.messages["panel_title"],
        description=fmt(cfg.messages["panel_description"], guild=guild.name),
        colour=BRAND,
    )
    if guild.icon:
        e.set_thumbnail(url=guild.icon.url)
    e.set_footer(text="One application per person at a time.")
    return e


def application_embed(member: discord.Member, ticket: "Ticket") -> discord.Embed:
    e = discord.Embed(title=f"Application — {member.display_name}", colour=BRAND, timestamp=ticket.created_dt)
    e.set_author(name=f"{member} • {member.id}", icon_url=member.display_avatar.url)
    e.add_field(name="Governor name", value=clip(ticket.governor_name), inline=True)
    e.add_field(name="Governor ID", value=clip(ticket.governor_id), inline=True)
    e.add_field(name="Current power", value=clip(ticket.power), inline=True)
    e.add_field(name="Alliance (current / applying to)", value=clip(ticket.alliance), inline=False)
    if ticket.extra:
        e.add_field(name="Anything else", value=clip(ticket.extra), inline=False)
    e.add_field(
        name="Discord account",
        value=f"{member.mention}\nCreated {ts(member.created_at, 'R')}",
        inline=False,
    )
    e.set_footer(text=f"Ticket #{ticket.id}")
    return e


def closing_embed(ticket: "Ticket", delay: int, will_delete: bool) -> discord.Embed:
    title, colour = OUTCOME_STYLE.get(ticket.outcome or "closed", ("Closed", discord.Colour.light_grey()))
    lines = []
    if ticket.handled_by:
        lines.append(f"By <@{ticket.handled_by}>")
    if ticket.alliance_role_id:
        lines.append(f"Alliance role: <@&{ticket.alliance_role_id}>")
    if ticket.reason:
        lines.append(f"Reason: {clip(ticket.reason, 900)}")
    if will_delete:
        lines.append(f"\nSaving transcript… this channel will be deleted in **{delay}s**.")
    return discord.Embed(title=title, description="\n".join(lines) or None, colour=colour)


def log_embed(ticket: "Ticket", *, resumed: bool, transcript_ok: bool) -> discord.Embed:
    title, colour = OUTCOME_STYLE.get(ticket.outcome or "closed", ("Closed", discord.Colour.light_grey()))
    e = discord.Embed(title=f"Ticket #{ticket.id} — {title}", colour=colour, timestamp=ticket.closed_dt)
    e.add_field(name="Applicant", value=f"<@{ticket.user_id}>\n`{ticket.username}` • `{ticket.user_id}`", inline=True)
    e.add_field(
        name="Handled by",
        value=f"<@{ticket.handled_by}>" if ticket.handled_by else "— (automatic)",
        inline=True,
    )
    e.add_field(name="Opened", value=ts(ticket.created_dt), inline=True)
    e.add_field(name="Closed", value=ts(ticket.closed_dt), inline=True)
    e.add_field(name="Governor", value=f"{clip(ticket.governor_name, 200)}\nID `{clip(ticket.governor_id, 50)}`", inline=True)
    e.add_field(name="Power", value=clip(ticket.power, 100), inline=True)
    e.add_field(name="Alliance stated", value=clip(ticket.alliance, 200), inline=True)
    if ticket.outcome == "accepted":
        e.add_field(
            name="Alliance role given",
            value=f"<@&{ticket.alliance_role_id}>" if ticket.alliance_role_id else "None",
            inline=True,
        )
    if ticket.outcome == "denied":
        e.add_field(name="Action", value="Kicked" if ticket.kicked else "Kept Unverified", inline=True)
        e.add_field(name="Reason", value=clip(ticket.reason), inline=False)
    notes = list(ticket.note_lines)
    if resumed:
        notes.append("Finished after a bot restart.")
    if not transcript_ok:
        notes.append("No transcript: the ticket channel was already gone or unreadable.")
    if notes:
        e.add_field(name="Notes", value=clip("\n".join(f"• {n}" for n in notes)), inline=False)
    return e


def welcome_dm(cfg: "Config", guild: discord.Guild, alliance: discord.Role | None) -> discord.Embed:
    alliance_line = f"Alliance role: **{alliance.name}**\n" if alliance else ""
    return discord.Embed(
        description=fmt(cfg.messages["welcome_dm"], guild=guild.name, alliance=alliance.name if alliance else "",
                        alliance_line=alliance_line),
        colour=discord.Colour.green(),
    )


def deny_dm(cfg: "Config", guild: discord.Guild, reason: str, kicked: bool) -> discord.Embed:
    text = fmt(cfg.messages["deny_dm"], guild=guild.name, reason=reason)
    text += cfg.messages["deny_dm_kicked_suffix"] if kicked else cfg.messages["deny_dm_kept_suffix"]
    return discord.Embed(description=text[:4000], colour=discord.Colour.red())
