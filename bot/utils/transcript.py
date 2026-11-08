"""Turns a ticket channel's history into a plain-text transcript."""
from __future__ import annotations

import io
import re
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from ..database import Ticket


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n} B"


async def build_transcript(
    channel: discord.TextChannel, ticket: "Ticket"
) -> tuple[str, list[discord.Attachment]]:
    """Returns (transcript text, every attachment posted in the ticket)."""
    lines = [
        f"Ticket #{ticket.id} — #{channel.name}",
        f"Applicant: {ticket.username} ({ticket.user_id})",
        f"Governor: {ticket.governor_name} | ID {ticket.governor_id} | Power {ticket.power} | Alliance {ticket.alliance}",
        f"Opened (UTC): {ticket.created_at}",
        f"Outcome: {ticket.outcome or 'n/a'}" + (f" by user {ticket.handled_by}" if ticket.handled_by else ""),
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
            for f in embed.fields:
                value = (f.value or "").replace("\n", " / ")
                lines.append(f"      {f.name}: {value}")
        for att in msg.attachments:
            attachments.append(att)
            kind = att.content_type or "file"
            lines.append(f"    [attachment] {att.filename} ({kind}, {_human_size(att.size)}): {att.url}")
        if msg.stickers:
            lines.append("    [sticker] " + ", ".join(s.name for s in msg.stickers))
        lines.append("")
    lines.append("=" * 72)
    lines.append(
        "Note: Discord deletes attachments when the ticket channel is deleted, so the links above stop "
        "working. The bot re-uploads them to the log channel next to this transcript."
    )
    return "\n".join(lines), attachments


def transcript_file(text: str, ticket: "Ticket") -> discord.File:
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", ticket.username).strip("-") or "user"
    return discord.File(io.BytesIO(text.encode("utf-8")), filename=f"ticket-{ticket.id}-{safe}.txt")
