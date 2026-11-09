"""Ticket lifecycle: create, accept, deny, close, finalize (transcript -> log -> delete), recover.

Views and listeners stay thin and call into TicketManager, so all the rules
(ordering, rollback on failure, race protection) live in this one file.

Fixes compared to the old bot:
- Any error after a ticket is claimed (not only Discord "HTTP" errors, but also
  dropped connections) releases the claim, so the ticket never gets stuck.
- A failed channel creation always removes the reservation, so the applicant
  never sees "being created" forever.
- recover() runs every few minutes, not just at startup, and finishes any
  ticket that has been stuck in 'closing' for too long.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

import discord

from ...core.db import Database
from ...core.models import Ticket
from ...core.templating import render
from . import embeds, store
from .checks import hierarchy_problems, kick_problem, roles_by_id
from .context import TicketContext, load_context
from .transcript import build_transcript, transcript_file

if TYPE_CHECKING:
    from ...bot.client import JoltBot

log = logging.getLogger(__name__)

MAX_REUPLOADS = 25  # cap on attachments re-uploaded to the log channel per ticket
STUCK_AFTER_SECONDS = 120  # a claimed ticket with nothing working on it for this long is resumed
DISCORD_TROUBLE = "Discord didn't respond properly. Nothing was changed; please try again in a moment."


def ticket_channel_name(member: discord.Member) -> str:
    """Build a Discord-safe channel name from the member's username.

    Args:
        member: Applicant whose name becomes the channel slug.

    Returns:
        Name like ``ticket-alice`` truncated to 100 characters.
    """
    slug = re.sub(r"[^a-z0-9-]+", "-", member.name.lower()).strip("-") or str(member.id)
    return f"ticket-{slug}"[:100]


class TicketManager:
    """Discord-side ticket lifecycle used by views and the tickets cog.

    Args:
        bot: Parent ``JoltBot`` (provides ``db`` and issue reporting).
    """

    def __init__(self, bot: JoltBot) -> None:
        self.bot = bot
        # Ticket IDs this process is currently working on. recover() leaves these alone.
        self._busy: set[int] = set()
        self._tasks: set[asyncio.Task] = set()

    @property
    def db(self) -> Database:
        return self.bot.db

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        # Keep a reference to background tasks, otherwise Python may garbage-collect them mid-run.
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(_log_task_errors)

    async def _get_member(self, guild: discord.Guild, user_id: int) -> discord.Member | None:
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user_id)
        except discord.NotFound:
            return None

    async def _dm(self, member: discord.Member, embed: discord.Embed) -> bool:
        try:
            await member.send(embed=embed)
            return True
        except (discord.HTTPException, OSError):
            return False

    def _overwrites(self, guild: discord.Guild, member: discord.Member, ctx: TicketContext) -> dict:
        staff_perms = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            attach_files=True,
            embed_links=True,
            read_message_history=True,
            manage_messages=True,
        )
        overwrites: dict = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                embed_links=True,
                attach_files=True,
                read_message_history=True,
                manage_channels=True,
                manage_messages=True,
            ),
            member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                attach_files=True,
                embed_links=True,
                read_message_history=True,
            ),
        }
        for role in roles_by_id(guild, ctx.settings.staff_role_ids):
            overwrites[role] = staff_perms
        # A separate ping role must be able to see the ticket or the ping won't notify them.
        # It gets read/write access but NOT button powers (those are checked by role in the view).
        for role in roles_by_id(guild, ctx.settings.effective_ping_role_ids):
            overwrites.setdefault(
                role, discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
            )
        return overwrites

    # ------------------------------------------------------------------ open

    async def active_ticket_for(self, member: discord.Member) -> Ticket | None:
        """Return the member's active ticket, or close one whose channel vanished.

        Args:
            member: Applicant in a guild.

        Returns:
            Open/closing ticket, or ``None`` if none / orphan channel was claimed.
        """
        ticket = await store.get_active_by_user(self.db, member.guild.id, member.id)
        if (
            ticket
            and ticket.status == "open"
            and ticket.channel_id
            and member.guild.get_channel(ticket.channel_id) is None
        ):
            # The channel vanished (deleted while the bot was offline). Log it and treat as no ticket.
            if await store.claim(self.db, ticket.id, outcome="channel_deleted", handled_by=None):
                self._spawn(self.finalize(ticket.id))
            return None
        return ticket

    async def create_ticket(
        self, member: discord.Member, ctx: TicketContext, answers: list[dict[str, str]]
    ) -> tuple[discord.TextChannel | None, str | None]:
        """Reserve a row, create the private channel, and post the application.

        Rolls back the reservation if channel creation fails so the applicant
        is never stuck with "being created".

        Args:
            member: Applicant.
            ctx: Fresh guild ticket context (must be ``ready``).
            answers: Submitted form answers.

        Returns:
            ``(channel, None)`` on success, or ``(None, error_message)`` for the applicant.
        """
        guild = member.guild
        category = guild.get_channel(ctx.settings.ticket_category_id or 0)
        if not isinstance(category, discord.CategoryChannel):
            await self.bot.report_issue(
                guild.id, "tickets.category", "The ticket category no longer exists. Pick a new one on the dashboard."
            )
            return None, "The ticket system isn't set up correctly yet. Please let a staff member know."

        ticket_id = await store.reserve(
            self.db, guild_id=guild.id, user_id=member.id, username=member.name, answers=answers
        )
        if ticket_id is None:
            existing = await store.get_active_by_user(self.db, guild.id, member.id)
            if existing and existing.channel_id and existing.status == "open":
                return None, f"You already have an open application: <#{existing.channel_id}>"
            return None, "Your previous ticket is still being closed. Try again in a few seconds."

        try:
            channel = await guild.create_text_channel(
                name=ticket_channel_name(member),
                category=category,
                overwrites=self._overwrites(guild, member, ctx),
                topic=f"Ticket #{ticket_id} for {member} ({member.id})",
                reason=f"Ticket for {member} ({member.id})",
            )
        except discord.Forbidden:
            await store.delete_reservation(self.db, ticket_id)
            await self.bot.report_issue(
                guild.id,
                "tickets.create_channel",
                f"I couldn't create a ticket channel in **{category.name}**. JoltBot needs Manage Channels and "
                "Manage Roles there, plus every permission it gives out (View, Send, Attach Files, Embed Links, "
                "Read History, Manage Messages). Run /tickets diagnostics in Discord for details.",
            )
            return None, "I couldn't create your ticket (missing permissions). Please let a staff member know."
        except discord.HTTPException as e:
            await store.delete_reservation(self.db, ticket_id)
            log.warning("Guild %s: failed to create ticket channel: %s", guild.id, e)
            if len(category.channels) >= 50:
                await self.bot.report_issue(
                    guild.id,
                    "tickets.category_full",
                    f"The **{category.name}** category is full (Discord allows 50 channels).",
                )
                return None, "The ticket category is full. Please let a staff member know."
            return None, "Discord refused to create the ticket right now. Please try again shortly."
        except Exception:
            # e.g. the connection dropped. Always undo the reservation so they can retry.
            await store.delete_reservation(self.db, ticket_id)
            log.exception("Guild %s: unexpected error creating ticket channel", guild.id)
            return None, DISCORD_TROUBLE

        await self.bot.resolve_issue(guild.id, "tickets.create_channel")
        await self.bot.resolve_issue(guild.id, "tickets.category_full")
        await store.set_channel(self.db, ticket_id, channel.id)
        ticket = await store.get(self.db, ticket_id)
        assert ticket is not None

        from .views import TicketControlView  # local import avoids a circular import

        pings = " ".join(f"<@&{rid}>" for rid in ctx.settings.effective_ping_role_ids)
        intro = render(
            ctx.settings.messages.ticket_intro, user=member.mention, user_name=member.display_name, server=guild.name
        )
        try:
            await channel.send(
                content=f"{member.mention} {pings}".strip(),
                embed=embeds.application_embed(member, ticket, ctx.colour),
                view=TicketControlView(self.bot),
                allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False),
            )
            await channel.send(intro[:2000], allowed_mentions=discord.AllowedMentions.none())
        except (discord.HTTPException, OSError) as e:
            log.warning("Guild %s: created %s but couldn't post the application in it: %s", guild.id, channel, e)
        log.info("Guild %s: opened ticket #%s (%s) for %s", guild.id, ticket_id, channel.name, member.id)
        return channel, None

    # ------------------------------------------------------------------ decisions

    async def accept(
        self, guild: discord.Guild, staff: discord.Member, ticket: Ticket, extra_role: discord.Role | None
    ) -> str | None:
        """Claim the ticket, grant accept roles, DM welcome, then finalize.

        Roles are added before pending is removed so a partial failure leaves
        the member with access rather than locked out.

        Args:
            guild: Guild owning the ticket.
            staff: Staff member who confirmed Accept.
            ticket: Open ticket row.
            extra_role: Optional extra role chosen on Accept.

        Returns:
            ``None`` on success, or an error message for the staff member.
        """
        ctx = await load_context(self.bot, guild.id)
        member = await self._get_member(guild, ticket.user_id)
        if member is None:
            return "The applicant is no longer in the server. Use 🔒 Close instead."
        accept_roles = roles_by_id(guild, ctx.settings.accept_role_ids)
        if not accept_roles:
            return "None of the 'role given on accept' roles exist any more. Fix this on the dashboard."
        pending = guild.get_role(ctx.settings.pending_role_id or 0)
        if problems := hierarchy_problems(guild, [r for r in (*accept_roles, pending, extra_role) if r]):
            return "\n".join(problems)

        self._busy.add(ticket.id)
        try:
            if not await store.claim(
                self.db,
                ticket.id,
                outcome="accepted",
                handled_by=staff.id,
                extra_role_id=extra_role.id if extra_role else None,
            ):
                return "Someone else already handled this ticket."
            reason = f"Application accepted by {staff} (ticket #{ticket.id})"
            try:
                # Add first, then remove: if removal fails they keep access instead of being locked out.
                await member.add_roles(*accept_roles, *([extra_role] if extra_role else []), reason=reason)
                if pending and pending in member.roles:
                    await member.remove_roles(pending, reason=reason)
            except discord.Forbidden:
                await store.release(self.db, ticket.id)
                return (
                    "Discord blocked the role change. JoltBot's role must sit ABOVE every role it gives "
                    "or removes (Server Settings → Roles). Run /tickets diagnostics."
                )
            except Exception:
                await store.release(self.db, ticket.id)
                log.exception("Guild %s: accept of ticket #%s failed", guild.id, ticket.id)
                return DISCORD_TROUBLE

            if not await self._dm(member, embeds.welcome_dm(ctx.settings, guild, member, extra_role)):
                await store.add_note(self.db, ticket.id, "Welcome DM failed (applicant's DMs are closed).")
            log.info("Guild %s: ticket #%s accepted by %s", guild.id, ticket.id, staff.id)
        finally:
            self._busy.discard(ticket.id)
        self._spawn(self.finalize(ticket.id))
        return None

    async def deny(
        self, guild: discord.Guild, staff: discord.Member, ticket: Ticket, reason: str, kick: bool
    ) -> str | None:
        """Claim the ticket, optionally kick, DM the deny reason, then finalize.

        The deny DM is sent before a kick so Discord still shares a guild with
        the applicant (DMs are more reliable that way).

        Args:
            guild: Guild owning the ticket.
            staff: Staff member who confirmed Deny.
            ticket: Open ticket row.
            reason: Deny reason shown in the DM / log.
            kick: If True, kick after the DM when hierarchy allows.

        Returns:
            ``None`` on success, or an error message for the staff member.
        """
        ctx = await load_context(self.bot, guild.id)
        member = await self._get_member(guild, ticket.user_id)
        if kick and member is not None and (problem := kick_problem(guild, member)):
            return problem

        self._busy.add(ticket.id)
        try:
            if not await store.claim(self.db, ticket.id, outcome="denied", handled_by=staff.id, reason=reason):
                return "Someone else already handled this ticket."
            try:
                if member is None:
                    await store.add_note(self.db, ticket.id, "Applicant had already left; no DM sent.")
                else:
                    # DM *before* kicking: once they're gone we share no server and the DM usually fails.
                    if not await self._dm(member, embeds.deny_dm(ctx.settings, guild, member, reason, kick)):
                        await store.add_note(self.db, ticket.id, "Denial DM failed (applicant's DMs are closed).")
                    if kick:
                        try:
                            await member.kick(reason=f"Application denied by {staff}: {reason}"[:512])
                            await store.set_kicked(self.db, ticket.id)
                        except discord.HTTPException as e:
                            await store.add_note(self.db, ticket.id, f"Kick failed ({e.status}); member was kept.")
                    else:
                        # Not kicked: make sure they still have the pending role (if the server uses one).
                        pending = guild.get_role(ctx.settings.pending_role_id or 0)
                        if pending and pending not in member.roles:
                            try:
                                await member.add_roles(pending, reason="Application denied; kept pending")
                            except discord.HTTPException:
                                pass
            except Exception:
                # The decision is already recorded and the DM may be sent, so finish the ticket
                # rather than releasing it; the log will show what happened.
                log.exception("Guild %s: deny of ticket #%s hit an error", guild.id, ticket.id)
                await store.add_note(self.db, ticket.id, "An error happened while denying; check the member manually.")
            log.info("Guild %s: ticket #%s denied by %s (kick=%s)", guild.id, ticket.id, staff.id, kick)
        finally:
            self._busy.discard(ticket.id)
        self._spawn(self.finalize(ticket.id))
        return None

    async def close(self, staff: discord.Member, ticket: Ticket) -> str | None:
        """Claim the ticket with outcome ``closed`` (no role changes) and finalize.

        Args:
            staff: Staff member who confirmed Close.
            ticket: Open ticket row.

        Returns:
            ``None`` on success, or an error if already claimed.
        """
        if not await store.claim(self.db, ticket.id, outcome="closed", handled_by=staff.id):
            return "Someone else already handled this ticket."
        log.info("Guild %s: ticket #%s closed by %s", ticket.guild_id, ticket.id, staff.id)
        self._spawn(self.finalize(ticket.id))
        return None

    # ------------------------------------------------------------------ automatic closes

    async def handle_member_left(self, guild_id: int, user_id: int) -> None:
        """Auto-close an active ticket when the applicant leaves the guild.

        Args:
            guild_id: Discord guild snowflake.
            user_id: Member who left.
        """
        ticket = await store.get_active_by_user(self.db, guild_id, user_id)
        if ticket and await store.claim(self.db, ticket.id, outcome="left", handled_by=None):
            log.info("Guild %s: applicant %s left; auto-closing ticket #%s", guild_id, user_id, ticket.id)
            self._spawn(self.finalize(ticket.id))

    async def handle_channel_deleted(self, channel_id: int) -> None:
        """React when a ticket channel is deleted outside the normal finalize path.

        Args:
            channel_id: Deleted Discord channel snowflake.
        """
        ticket = await store.get_by_channel(self.db, channel_id)
        if ticket is None:
            return
        if ticket.status == "open" and await store.claim(
            self.db, ticket.id, outcome="channel_deleted", handled_by=None
        ):
            self._spawn(self.finalize(ticket.id))
        elif ticket.status == "closed":
            await store.mark_channel_gone(self.db, ticket.id)

    # ------------------------------------------------------------------ finalize

    async def finalize(self, ticket_id: int, *, resumed: bool = False) -> None:
        """Transcript → log channel → mark closed → delete the channel after a delay.

        Does not delete the channel if the log post failed (preserve evidence).
        Safe to call more than once. If anything unexpected goes wrong, the
        ticket stays ``closing`` and ``recover()`` retries it a few minutes later.

        Args:
            ticket_id: Ticket primary key already in ``closing`` (or to resume).
            resumed: True when recovery is finishing a previously stuck ticket.
        """
        if ticket_id in self._busy:
            return
        self._busy.add(ticket_id)
        try:
            await self._finalize(ticket_id, resumed)
        except Exception:
            log.exception("Finalizing ticket #%s failed; it will be retried automatically", ticket_id)
        finally:
            self._busy.discard(ticket_id)

    async def _finalize(self, ticket_id: int, resumed: bool) -> None:
        ticket = await store.get(self.db, ticket_id)
        if ticket is None or ticket.status == "closed":
            return
        guild = self.bot.get_guild(ticket.guild_id)
        if guild is None or guild.unavailable:
            return  # retried later by recover()
        ctx = await load_context(self.bot, guild.id)
        channel = guild.get_channel(ticket.channel_id) if ticket.channel_id else None
        if not isinstance(channel, discord.TextChannel):
            channel = None

        text: str | None = None
        attachments: list[discord.Attachment] = []
        if channel is not None:
            delay = ctx.settings.delete_delay_seconds
            try:
                await channel.send(embed=embeds.closing_embed(ticket, delay))
            except discord.HTTPException:
                pass
            # Stop the applicant from posting while we archive.
            applicant = guild.get_member(ticket.user_id)
            if applicant is not None:
                try:
                    overwrite = channel.overwrites_for(applicant)
                    overwrite.send_messages = False
                    await channel.set_permissions(applicant, overwrite=overwrite, reason="Ticket closing")
                except discord.HTTPException:
                    pass
            try:
                text, attachments = await build_transcript(channel, ticket)
            except discord.HTTPException as e:
                log.warning("Guild %s: couldn't read %s for the transcript: %s", guild.id, channel, e)

        # Status stays 'closing' until the log is posted, so a crash here is retried.
        await store.stamp_closed_at(self.db, ticket_id)
        ticket = await store.get(self.db, ticket_id)
        assert ticket is not None
        logged = await self._post_log(guild, ctx, ticket, text, attachments, resumed)
        await store.mark_closed(self.db, ticket_id, logged=logged)

        if channel is None:
            await store.mark_channel_gone(self.db, ticket_id)
            return
        if not logged:
            try:
                await channel.send(
                    "⚠️ I couldn't post the transcript to the log channel, so I did **not** delete this channel "
                    "(nothing is lost). Fix JoltBot's permissions in the log channel, then delete this channel "
                    "manually. The dashboard shows what's wrong."
                )
            except discord.HTTPException:
                pass
            return
        await asyncio.sleep(ctx.settings.delete_delay_seconds)
        await self._delete_channel(channel, ticket)

    async def _delete_channel(self, channel: discord.abc.GuildChannel, ticket: Ticket) -> None:
        try:
            await channel.delete(reason=f"Ticket #{ticket.id} closed ({ticket.outcome})")
        except discord.NotFound:
            pass
        except discord.Forbidden:
            await self.bot.report_issue(
                ticket.guild_id,
                "tickets.delete_channel",
                "I couldn't delete a closed ticket channel. Give JoltBot Manage Channels in the ticket category.",
            )
            return
        except discord.HTTPException as e:
            log.warning("Guild %s: couldn't delete %s: %s", ticket.guild_id, channel, e)
            return
        await store.mark_channel_gone(self.db, ticket.id)

    async def _post_log(
        self,
        guild: discord.Guild,
        ctx: TicketContext,
        ticket: Ticket,
        text: str | None,
        attachments: list[discord.Attachment],
        resumed: bool,
    ) -> bool:
        log_channel = guild.get_channel(ctx.settings.log_channel_id or 0)
        if not isinstance(log_channel, discord.TextChannel):
            await self.bot.report_issue(
                guild.id,
                "tickets.log_channel",
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
            await self.bot.report_issue(
                guild.id,
                "tickets.log_channel",
                f"I can't post in #{log_channel.name}. JoltBot needs View Channel, Send Messages, Embed Links and "
                "Attach Files there. Closed tickets are kept (not deleted) until this is fixed.",
            )
            return False
        except discord.HTTPException as e:
            log.warning("Guild %s: failed to post ticket log: %s", guild.id, e)
            return False
        await self.bot.resolve_issue(guild.id, "tickets.log_channel")
        if ctx.settings.reupload_attachments and attachments:
            await self._reupload(log_channel, ticket, attachments)
        return True

    async def _reupload(
        self, log_channel: discord.TextChannel, ticket: Ticket, attachments: list[discord.Attachment]
    ) -> None:
        """Discord deletes attachments with their channel, so copy them into the log channel."""
        limit = log_channel.guild.filesize_limit
        batch: list[discord.File] = []
        batch_size = 0
        skipped: list[str] = []

        async def flush() -> None:
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
            if len(batch) == 10 or batch_size + att.size > limit:
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
                await log_channel.send(message[:2000])
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------------ recovery

    async def recover(self, guild: discord.Guild) -> None:
        """Bring the database and Discord back in sync for one guild.

        Runs at startup and every few minutes, so nothing stays stuck until the
        next restart (the old bot only did this at startup). Skips tickets in
        ``_busy`` that this process is already finalizing.

        Args:
            guild: Connected, available Discord guild.
        """
        if guild.unavailable:
            return
        for t in await store.list_for_guild(self.db, guild.id, "open"):
            if t.id in self._busy:
                continue
            if t.channel_id is None:
                # Reserved but the channel was never created (crash or error mid-way).
                if store.older_than(t.created_at, STUCK_AFTER_SECONDS):
                    await store.delete_reservation(self.db, t.id)
                continue
            if guild.get_channel(t.channel_id) is None:
                if await store.claim(self.db, t.id, outcome="channel_deleted", handled_by=None):
                    self._spawn(self.finalize(t.id, resumed=True))
            elif guild.get_member(t.user_id) is None:
                if await store.claim(self.db, t.id, outcome="left", handled_by=None):
                    self._spawn(self.finalize(t.id, resumed=True))
        for t in await store.list_for_guild(self.db, guild.id, "closing"):
            # claimed_at is None for tickets copied from the old bot; treat those as stuck too.
            if t.id not in self._busy and (t.claimed_at is None or store.older_than(t.claimed_at, STUCK_AFTER_SECONDS)):
                self._spawn(self.finalize(t.id, resumed=True))
        for t in await store.list_closed_needing_delete(self.db, guild.id):
            if t.id in self._busy:
                continue
            channel = guild.get_channel(t.channel_id) if t.channel_id else None
            if channel is None:
                await store.mark_channel_gone(self.db, t.id)
            else:
                self._spawn(self._delete_channel(channel, t))


def _log_task_errors(task: asyncio.Task) -> None:
    if not task.cancelled() and (exc := task.exception()):
        log.error("Background ticket task failed", exc_info=exc)
