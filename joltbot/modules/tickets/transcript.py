"""Turns a ticket channel's history into a plain-text transcript."""

from __future__ import annotations

import io
import re

import discord

from ...core.models import Ticket


def _human_size(n: float) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


async def build_transcript(channel: discord.TextChannel, ticket: Ticket) -> tuple[str, list[discord.Attachment]]:
    """Returns (transcript text, every attachment posted in the ticket)."""
    lines = [
        f"Ticket #{ticket.id} - #{channel.name}",
        f"Applicant: {ticket.username} ({ticket.user_id})",
        f"Opened (UTC): {ticket.created_at:%Y-%m-%d %H:%M:%S}",
        f"Outcome: {ticket.outcome or 'n/a'}" + (f" by user {ticket.handled_by}" if ticket.handled_by else ""),
        "Answers:",
        *[f"  {a.get('label', '?')}: {a.get('value', '')}" for a in ticket.answers or []],
        "=" * 72,
        "",
    ]
    attachments: list[discord.Attachment] = []
    async for msg in channel.history(limit=None, oldest_first=True):
        stamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        author = f"{msg.author} ({msg.author.id})" + (" [BOT]" if msg.author.bot else "")
        lines.append(f"[{stamp}] {author}:")
        if msg.content:
            lines.extend(f"    {line}" for line in msg.content.splitlines())
        for embed in msg.embeds:
            header = " | ".join(p for p in (embed.title, embed.author.name if embed.author else None) if p)
            lines.append(f"    [embed] {header}")
            if embed.description:
                lines.extend(f"      {line}" for line in embed.description.splitlines())
            for field in embed.fields:
                lines.append(f"      {field.name}: {(field.value or '').replace(chr(10), ' / ')}")
        for att in msg.attachments:
            attachments.append(att)
            lines.append(
                f"    [attachment] {att.filename} ({att.content_type or 'file'}, {_human_size(att.size)}): {att.url}"
            )
        if msg.stickers:
            lines.append("    [sticker] " + ", ".join(s.name for s in msg.stickers))
        lines.append("")
    lines.append("=" * 72)
    lines.append(
        "Note: Discord deletes attachments when the ticket channel is deleted, so the links above stop "
        "working. JoltBot re-uploads them to the log channel next to this transcript."
    )
    return "\n".join(lines), attachments


def transcript_file(text: str, ticket: Ticket) -> discord.File:
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", ticket.username).strip("-") or "user"
    return discord.File(io.BytesIO(text.encode("utf-8")), filename=f"ticket-{ticket.id}-{safe}.txt")
