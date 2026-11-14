"""Posts a closed ticket's decision, transcript and attachments to the server's log channel."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from ...core.limits import MESSAGE_MAX, MESSAGE_MAX_FILES
from ...core.models import Ticket
from . import embeds
from .constants import ISSUE_LOG_CHANNEL
from .context import TicketContext
from .transcript import transcript_file

if TYPE_CHECKING:
    from ...bot.client import JoltBot

log = logging.getLogger(__name__)

MAX_REUPLOADS = 25  # cap on attachments re-uploaded to the log channel per ticket
# What JoltBot needs in the log channel to post a decision with its transcript.
LOG_CHANNEL_PERMISSIONS = ("view_channel", "send_messages", "embed_links", "attach_files")


async def post_log(
    bot: JoltBot,
    guild: discord.Guild,
    ctx: TicketContext,
    ticket: Ticket,
    text: str | None,
    attachments: list[discord.Attachment],
    resumed: bool,
) -> bool:
    """Post the ticket's decision and transcript in the log channel.

    A missing log channel, or one JoltBot can't post in, is reported to the
    server's admins as a dashboard issue. The issue clears itself on the next
    post that works.

    Args:
        bot: Running bot (for reporting and clearing the issue).
        guild: Server the ticket belongs to.
        ctx: Fresh ticket settings for the server.
        ticket: The ticket, with its decision already saved.
        text: The transcript, or ``None`` if the channel was gone or unreadable.
        attachments: Files from the ticket channel, copied over if the server wants that.
        resumed: True when this is a later try (after a restart or a failure).

    Returns:
        True if the decision was posted. False means the caller must keep the
        ticket channel, because it is then the only record.
    """
    log_channel = guild.get_channel(ctx.settings.log_channel_id or 0)
    if not isinstance(log_channel, discord.TextChannel):
        await bot.report_issue(
            guild.id,
            ISSUE_LOG_CHANNEL,
            "The ticket log channel is missing. Pick one on the dashboard. Closed "
            "tickets are kept (not deleted) until this is fixed.",
        )
        return False
    files = [transcript_file(text, ticket)] if text else []
    try:
        await log_channel.send(
            embed=embeds.log_embed(ticket, resumed=resumed, transcript_ok=text is not None),
            files=files,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.Forbidden:
        await bot.report_issue(
            guild.id,
            ISSUE_LOG_CHANNEL,
            f"I can't post in #{log_channel.name}. JoltBot needs View Channel, Send Messages, Embed Links and "
            "Attach Files there. Closed tickets are kept (not deleted) until this is fixed.",
        )
        return False
    except discord.HTTPException as e:
        log.warning("Guild %s: failed to post ticket log: %s", guild.id, e)
        return False
    await bot.resolve_issue(guild.id, ISSUE_LOG_CHANNEL)
    if ctx.settings.reupload_attachments and attachments:
        await reupload_attachments(log_channel, ticket, attachments)
    return True


async def reupload_attachments(
    log_channel: discord.TextChannel, ticket: Ticket, attachments: list[discord.Attachment]
) -> None:
    """Copy a ticket's attachments into the log channel.

    Discord deletes attachments together with their channel, so the links in
    the transcript stop working once the ticket channel is gone.

    Args:
        log_channel: Where the copies go.
        ticket: The ticket the files belong to.
        attachments: The files, oldest first. At most ``MAX_REUPLOADS`` are
            copied, and files over the server's upload limit are skipped; both
            are said in a follow-up message.
    """
    limit = log_channel.guild.filesize_limit
    batch: list[discord.File] = []
    batch_size = 0
    skipped: list[str] = []

    async def flush() -> None:
        """Send the files gathered so far as one message, then start a new batch."""
        nonlocal batch, batch_size
        if batch:
            try:
                await log_channel.send(
                    f"📎 Attachments from ticket #{ticket.id} (<@{ticket.user_id}>)",
                    files=batch,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as e:
                log.warning("Couldn't re-upload attachments for ticket #%s: %s", ticket.id, e)
        batch, batch_size = [], 0

    for att in attachments[:MAX_REUPLOADS]:
        if att.size > limit:
            skipped.append(att.filename)
            continue
        # One message holds a limited number of files, and their sizes add up against the same limit.
        if len(batch) == MESSAGE_MAX_FILES or batch_size + att.size > limit:
            await flush()
        try:
            batch.append(await att.to_file())
            batch_size += att.size
        except discord.HTTPException:
            skipped.append(att.filename)
    await flush()
    if skipped or len(attachments) > MAX_REUPLOADS:
        message = f"⚠️ Ticket #{ticket.id}: couldn't re-upload {', '.join(skipped)}"
        if (extra := len(attachments) - MAX_REUPLOADS) > 0:
            message += f" (+{extra} more over the {MAX_REUPLOADS}-file cap)"
        try:
            await log_channel.send(message[:MESSAGE_MAX])
        except discord.HTTPException as e:
            log.debug("Couldn't post the skipped-attachments note for ticket #%s: %s", ticket.id, e)
