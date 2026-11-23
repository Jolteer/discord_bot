"""Ticket lifecycle: create, accept, deny, close, finalize (transcript -> log -> delete), recover.

Views and listeners stay thin and call into TicketManager, so all the rules
(ordering, rollback on failure, race protection) live in this one file.
Two stateless parts sit next to it: channels.py (the ticket channel's name and
permissions) and log_post.py (posting to the log channel).

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
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

import discord

from ...core.db import Database
from ...core.limits import AUDIT_REASON_MAX, CATEGORY_MAX_CHANNELS, MESSAGE_MAX
from ...core.models import Ticket
from ...core.templating import render
from . import embeds, store
from .channels import ticket_channel_name, ticket_overwrites
from .checks import hierarchy_problems, kick_problem, roles_by_id
from .constants import (
    ISSUE_CATEGORY,
    ISSUE_CATEGORY_FULL,
    ISSUE_CREATE_CHANNEL,
    ISSUE_DELETE_CHANNEL,
    OUTCOME_ACCEPTED,
    OUTCOME_CHANNEL_DELETED,
    OUTCOME_CLOSED,
    OUTCOME_DENIED,
    OUTCOME_LEFT,
    STATUS_CLOSED,
    STATUS_CLOSING,
    STATUS_OPEN,
)
from .context import TicketContext, load_context
from .log_post import LOG_CHANNEL_PERMISSIONS, post_log
from .transcript import build_transcript
from .views import TicketControlView

if TYPE_CHECKING:
    from ...bot.client import JoltBot

log = logging.getLogger(__name__)

STUCK_AFTER_SECONDS = 120  # a claimed ticket with nothing working on it for this long is resumed
DISCORD_TROUBLE = "Discord didn't respond properly. Nothing was changed; please try again in a moment."
ALREADY_HANDLED = "Someone else already handled this ticket."
WELCOME_DM_FAILED_NOTE = "Welcome DM failed (applicant's DMs are closed)."


class TicketManager:
    """Discord-side ticket lifecycle used by views and the tickets cog.

    Args:
        bot: Parent ``JoltBot`` (provides ``db`` and issue reporting).
    """

    def __init__(self, bot: JoltBot) -> None:
        """Create the manager with no ticket work in progress."""
        self.bot = bot
        # Ticket IDs this process is currently working on. recover() leaves these alone.
        self._busy: set[int] = set()
        # Ticket IDs whose channel is being created right now. recover() leaves these alone too.
        self._creating: set[int] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def db(self) -> Database:
        """The bot's database, for the ``store`` functions."""
        return self.bot.db

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        """Run a piece of ticket work in the background and keep track of it."""
        # Keep a reference to background tasks, otherwise Python may garbage-collect them mid-run.
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(_log_task_errors)

    async def _get_member(self, guild: discord.Guild, user_id: int) -> discord.Member | None:
        """Find a member, asking Discord when they are not in the bot's cache.

        Args:
            guild: Server to look in.
            user_id: The member's user ID.

        Returns:
            The member, or ``None`` once Discord confirms they are not in the server.

        Raises:
            discord.HTTPException: Discord couldn't answer. Not the same as "not a member".
        """
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user_id)
        except discord.NotFound:
            return None

    async def _has_left(self, guild: discord.Guild, user_id: int) -> bool:
        """Ask Discord whether a member missing from the cache has really left.

        The member cache can be incomplete (loading a server's member list can
        time out at startup), so a cache miss alone must never close a ticket.

        Args:
            guild: Guild the ticket belongs to.
            user_id: Applicant to look up.

        Returns:
            True only when Discord confirms the member is gone. False if they
            are still there, or if Discord couldn't answer (checked again on
            the next recovery round).
        """
        try:
            return await self._get_member(guild, user_id) is None
        except (discord.HTTPException, OSError) as e:
            log.warning("Guild %s: couldn't check whether %s left; leaving their ticket open: %s", guild.id, user_id, e)
            return False

    async def _dm(self, member: discord.Member, embed: discord.Embed) -> bool:
        """Send a DM. Returns False, without raising, if it can't be delivered (usually closed DMs)."""
        try:
            await member.send(embed=embed)
            return True
        except (discord.HTTPException, OSError):
            return False

    async def _send_welcome_dm(
        self,
        ctx: TicketContext,
        guild: discord.Guild,
        member: discord.Member,
        ticket: Ticket,
        extra_role: discord.Role | None,
    ) -> None:
        """DM the accepted member the server's welcome text.

        A DM that can't be delivered (closed DMs) becomes a note on the ticket.

        Args:
            ctx: Fresh ticket settings for the guild.
            guild: Guild owning the ticket.
            member: The accepted member.
            ticket: Their ticket, for the note.
            extra_role: The extra role staff picked on Accept, named in the DM.
        """
        if not await self._dm(member, embeds.welcome_dm(ctx.settings, guild, member, extra_role)):
            await store.add_note(self.db, ticket.id, WELCOME_DM_FAILED_NOTE)

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
            and ticket.status == STATUS_OPEN
            and ticket.channel_id
            and member.guild.get_channel(ticket.channel_id) is None
        ):
            # The channel vanished (deleted while the bot was offline). Log it and treat as no ticket.
            if await store.claim(self.db, ticket.id, outcome=OUTCOME_CHANNEL_DELETED, handled_by=None):
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
                guild.id, ISSUE_CATEGORY, "The ticket category no longer exists. Pick a new one on the dashboard."
            )
            return None, "The ticket system isn't set up correctly yet. Please let a staff member know."

        ticket_id = await store.reserve(
            self.db, guild_id=guild.id, user_id=member.id, username=member.name, answers=answers
        )
        if ticket_id is None:
            existing = await store.get_active_by_user(self.db, guild.id, member.id)
            if existing and existing.channel_id and existing.status == STATUS_OPEN:
                return None, f"You already have an open application: <#{existing.channel_id}>"
            return None, "Your previous ticket is still being closed. Try again in a few seconds."

        # recover() must not take this reservation for an abandoned one while Discord is
        # still creating its channel (that can take minutes when the bot is rate limited).
        self._creating.add(ticket_id)
        try:
            channel, error = await self._create_channel(member, ctx, category, ticket_id)
            if channel is None:
                # Always undo the reservation, so the applicant can try again.
                await store.delete_reservation(self.db, ticket_id)
                return None, error
            attached = await store.set_channel(self.db, ticket_id, channel.id)
        finally:
            self._creating.discard(ticket_id)

        ticket = await store.get(self.db, ticket_id) if attached else None
        if ticket is None:
            # The ticket was closed while its channel was being created (the applicant left
            # the server). Nothing points at the new channel, so remove it again.
            try:
                await channel.delete(reason=f"Ticket #{ticket_id} was closed before it finished opening")
            except (discord.HTTPException, OSError) as e:
                log.warning("Guild %s: couldn't remove unused ticket channel %s: %s", guild.id, channel, e)
            return None, "Your application was closed before it finished opening. Please try again."
        await self.bot.resolve_issue(guild.id, ISSUE_CREATE_CHANNEL)
        await self.bot.resolve_issue(guild.id, ISSUE_CATEGORY_FULL)

        await self._post_application(channel, member, ctx, ticket)
        log.info("Guild %s: opened ticket #%s (%s) for %s", guild.id, ticket_id, channel.name, member.id)
        return channel, None

    async def _post_application(
        self, channel: discord.TextChannel, member: discord.Member, ctx: TicketContext, ticket: Ticket
    ) -> None:
        """Post the application, the staff buttons and the server's welcome text in a new ticket channel.

        A failure here is only logged: the ticket exists and staff can still
        see the channel, so the applicant is not sent back to try again.

        Args:
            channel: The ticket channel that was just created.
            member: The applicant.
            ctx: Fresh ticket settings for the guild.
            ticket: The ticket row, for the application embed.
        """
        guild = member.guild
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
            await channel.send(intro[:MESSAGE_MAX], allowed_mentions=discord.AllowedMentions.none())
        except (discord.HTTPException, OSError) as e:
            log.warning("Guild %s: created %s but couldn't post the application in it: %s", guild.id, channel, e)

    async def _create_channel(
        self, member: discord.Member, ctx: TicketContext, category: discord.CategoryChannel, ticket_id: int
    ) -> tuple[discord.TextChannel | None, str | None]:
        """Create the private ticket channel.

        Args:
            member: Applicant.
            ctx: Fresh guild ticket context.
            category: The server's ticket category.
            ticket_id: The reserved ticket, for the channel topic.

        Returns:
            ``(channel, None)`` on success, or ``(None, error_message)`` for the applicant.
        """
        guild = member.guild
        try:
            channel = await guild.create_text_channel(
                name=ticket_channel_name(member),
                category=category,
                overwrites=ticket_overwrites(guild, member, ctx.settings),
                topic=f"Ticket #{ticket_id} for {member} ({member.id})",
                reason=f"Ticket for {member} ({member.id})",
            )
        except discord.Forbidden:
            await self.bot.report_issue(
                guild.id,
                ISSUE_CREATE_CHANNEL,
                f"I couldn't create a ticket channel in **{category.name}**. JoltBot needs Manage Channels and "
                "Manage Roles there, plus every permission it gives out (View, Send, Attach Files, Embed Links, "
                "Read History, Manage Messages). Run /tickets diagnostics in Discord for details.",
            )
            return None, "I couldn't create your ticket (missing permissions). Please let a staff member know."
        except discord.HTTPException as e:
            log.warning("Guild %s: failed to create ticket channel: %s", guild.id, e)
            if len(category.channels) >= CATEGORY_MAX_CHANNELS:
                await self.bot.report_issue(
                    guild.id,
                    ISSUE_CATEGORY_FULL,
                    f"The **{category.name}** category is full (Discord allows {CATEGORY_MAX_CHANNELS} channels).",
                )
                return None, "The ticket category is full. Please let a staff member know."
            return None, "Discord refused to create the ticket right now. Please try again shortly."
        except Exception:
            # e.g. the connection dropped.
            log.exception("Guild %s: unexpected error creating ticket channel", guild.id)
            return None, DISCORD_TROUBLE
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
        if not roles_by_id(guild, ctx.settings.accept_role_ids):
            return "None of the 'role given on accept' roles exist any more. Fix this on the dashboard."
        roles, notes = self._roles_for_accept(guild, ctx, ticket, extra_role.id if extra_role else None)
        pending = guild.get_role(ctx.settings.pending_role_id or 0)
        if problems := hierarchy_problems(guild, [*roles, *([pending] if pending else [])]):
            return "\n".join(problems)

        self._busy.add(ticket.id)
        try:
            if not await store.claim(
                self.db,
                ticket.id,
                outcome=OUTCOME_ACCEPTED,
                handled_by=staff.id,
                extra_role_id=extra_role.id if extra_role else None,
            ):
                return ALREADY_HANDLED
            reason = f"Application accepted by {staff} (ticket #{ticket.id})"
            try:
                # Add first, then remove: if removal fails they keep access instead of being locked out.
                await member.add_roles(*roles, reason=reason)
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

            for note in notes:
                await store.add_note(self.db, ticket.id, note)
            await self._send_welcome_dm(ctx, guild, member, ticket, extra_role)
            log.info("Guild %s: ticket #%s accepted by %s", guild.id, ticket.id, staff.id)
        finally:
            self._busy.discard(ticket.id)
        self._spawn(self.finalize(ticket.id))
        return None

    def _roles_for_accept(
        self, guild: discord.Guild, ctx: TicketContext, ticket: Ticket, extra_role_id: int | None
    ) -> tuple[list[discord.Role], list[str]]:
        """Every role an accept gives.

        That is the server's accept roles, the extra role staff picked, and the
        roles of the dropdown options the applicant picked. Used by ``accept``
        and by ``_confirm_accept_roles``, so an accept that is finished after a
        restart gives exactly the same roles.

        Args:
            guild: Guild owning the ticket.
            ctx: Fresh ticket settings for the guild.
            ticket: The ticket being accepted (its answers hold the picks).
            extra_role_id: The extra role chosen on Accept, if any.

        Returns:
            ``(roles, notes)``: the roles that still exist, without repeats, and
            a note for the log for each picked role that can't be given.
        """
        role_ids = [*ctx.settings.accept_role_ids, *([extra_role_id] if extra_role_id else [])]
        picked, dropped = ctx.settings.picked_roles(ticket.answers)
        notes = [f"The role for '{name}' is no longer set up, so it was not given." for name in dropped]
        for name, role_id in picked:
            if guild.get_role(role_id) is None:
                notes.append(f"The role for '{name}' no longer exists, so it was not given.")
            else:
                role_ids.append(role_id)
        return roles_by_id(guild, dict.fromkeys(role_ids)), notes

    async def _confirm_accept_roles(self, guild: discord.Guild, ctx: TicketContext, ticket: Ticket) -> None:
        """Finish the member's side of an accept that was cut short.

        The decision is saved before any role is given. If the bot stopped in
        between (a restart or an update), the ticket would otherwise be logged
        as accepted while the member got nothing. Giving a role twice is
        harmless, so this just makes sure. Anything it can't do becomes a note
        on the ticket, which the log shows.

        Args:
            guild: Guild owning the ticket.
            ctx: Fresh ticket settings for the guild.
            ticket: A ticket in ``closing`` with outcome ``accepted``.
        """
        unconfirmed = "Roles could not be confirmed after a restart; check this member."
        try:
            member = await self._get_member(guild, ticket.user_id)
        except (discord.HTTPException, OSError):
            await store.add_note(self.db, ticket.id, unconfirmed)
            return
        if member is None:
            await store.add_note(
                self.db, ticket.id, "The applicant is no longer in the server; their roles were not checked."
            )
            return
        roles, notes = self._roles_for_accept(guild, ctx, ticket, ticket.extra_role_id)
        missing = [r for r in roles if r not in member.roles]
        pending = guild.get_role(ctx.settings.pending_role_id or 0)
        reason = f"Application accepted (ticket #{ticket.id}); finished after a restart"
        try:
            if missing:
                await member.add_roles(*missing, reason=reason)
            if pending and pending in member.roles:
                await member.remove_roles(pending, reason=reason)
        except (discord.HTTPException, OSError) as e:
            log.warning("Guild %s: couldn't confirm roles for ticket #%s: %s", guild.id, ticket.id, e)
            await store.add_note(self.db, ticket.id, unconfirmed)
            return
        for note in notes:
            await store.add_note(self.db, ticket.id, note)
        if missing:
            # The accept never got as far as the roles, so the welcome DM wasn't sent either.
            await store.add_note(self.db, ticket.id, "Roles were given after a restart.")
            extra_role = guild.get_role(ticket.extra_role_id or 0)
            await self._send_welcome_dm(ctx, guild, member, ticket, extra_role)

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
            if not await store.claim(self.db, ticket.id, outcome=OUTCOME_DENIED, handled_by=staff.id, reason=reason):
                return ALREADY_HANDLED
            try:
                await self._carry_out_denial(guild, staff, ticket, member, ctx, reason, kick)
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

    async def _carry_out_denial(
        self,
        guild: discord.Guild,
        staff: discord.Member,
        ticket: Ticket,
        member: discord.Member | None,
        ctx: TicketContext,
        reason: str,
        kick: bool,
    ) -> None:
        """Tell the applicant they were denied, then kick them or keep them pending.

        Runs after the denial is already saved. Whatever can't be done (closed
        DMs, a failed kick) becomes a note on the ticket, which the log shows.

        Args:
            guild: Guild owning the ticket.
            staff: Staff member who denied, named in the kick's audit-log reason.
            ticket: The ticket, already claimed with outcome ``denied``.
            member: The applicant, or ``None`` if they already left.
            ctx: Fresh ticket settings for the guild.
            reason: Deny reason, sent in the DM.
            kick: True to kick the applicant after the DM.
        """
        if member is None:
            await store.add_note(self.db, ticket.id, "Applicant had already left; no DM sent.")
            return
        # DM *before* kicking: once they're gone we share no server and the DM usually fails.
        if not await self._dm(member, embeds.deny_dm(ctx.settings, guild, member, reason, kick)):
            await store.add_note(self.db, ticket.id, "Denial DM failed (applicant's DMs are closed).")
        if kick:
            try:
                await member.kick(reason=f"Application denied by {staff}: {reason}"[:AUDIT_REASON_MAX])
                await store.set_kicked(self.db, ticket.id)
            except discord.HTTPException as e:
                await store.add_note(self.db, ticket.id, f"Kick failed ({e.status}); member was kept.")
            return
        # Not kicked: make sure they still have the pending role (if the server uses one).
        pending = guild.get_role(ctx.settings.pending_role_id or 0)
        if pending and pending not in member.roles:
            try:
                await member.add_roles(pending, reason="Application denied; kept pending")
            except discord.HTTPException as e:
                log.debug("Guild %s: couldn't give the pending role back for ticket #%s: %s", guild.id, ticket.id, e)

    async def close(self, staff: discord.Member, ticket: Ticket) -> str | None:
        """Claim the ticket with outcome ``closed`` (no role changes) and finalize.

        Args:
            staff: Staff member who confirmed Close.
            ticket: Open ticket row.

        Returns:
            ``None`` on success, or an error if already claimed.
        """
        if not await store.claim(self.db, ticket.id, outcome=OUTCOME_CLOSED, handled_by=staff.id):
            return ALREADY_HANDLED
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
        if ticket and await store.claim(self.db, ticket.id, outcome=OUTCOME_LEFT, handled_by=None):
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
        if ticket.status == STATUS_OPEN and await store.claim(
            self.db, ticket.id, outcome=OUTCOME_CHANNEL_DELETED, handled_by=None
        ):
            self._spawn(self.finalize(ticket.id))
        elif ticket.status == STATUS_CLOSED:
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
        """The work of ``finalize``, without its run-once guard and error handling."""
        ticket = await store.get(self.db, ticket_id)
        if ticket is None or ticket.status == STATUS_CLOSED:
            return
        guild = self.bot.get_guild(ticket.guild_id)
        if guild is None or guild.unavailable:
            return  # retried later by recover()
        ctx = await load_context(self.bot, guild.id)
        if resumed and ticket.outcome == OUTCOME_ACCEPTED:
            await self._confirm_accept_roles(guild, ctx, ticket)
        channel = guild.get_channel(ticket.channel_id) if ticket.channel_id else None
        if not isinstance(channel, discord.TextChannel):
            channel = None

        text: str | None = None
        attachments: list[discord.Attachment] = []
        if channel is not None:
            text, attachments = await self._archive_channel(guild, channel, ticket, ctx)

        # Status stays 'closing' until the log is posted, so a crash here is retried.
        await store.stamp_closed_at(self.db, ticket_id)
        ticket = await store.get(self.db, ticket_id)
        assert ticket is not None
        logged = await post_log(self.bot, guild, ctx, ticket, text, attachments, resumed)
        await store.mark_closed(self.db, ticket_id, logged=logged)

        if channel is None:
            await store.mark_channel_gone(self.db, ticket_id)
            return
        if not logged:
            try:
                await channel.send(
                    "⚠️ I couldn't post the transcript to the log channel, so I did **not** delete this channel "
                    "(nothing is lost). Once the log channel is fixed, I'll post the transcript and delete this "
                    "channel within a few minutes. The dashboard shows what's wrong."
                )
            except discord.HTTPException as e:
                log.debug("Guild %s: couldn't post the kept-channel notice in %s: %s", guild.id, channel, e)
            return
        await asyncio.sleep(ctx.settings.delete_delay_seconds)
        await self._delete_channel(channel, ticket)

    async def _archive_channel(
        self, guild: discord.Guild, channel: discord.TextChannel, ticket: Ticket, ctx: TicketContext
    ) -> tuple[str | None, list[discord.Attachment]]:
        """Announce the close in the ticket channel, stop the applicant posting, and read the transcript.

        Args:
            guild: Guild owning the ticket.
            channel: The ticket channel, still present.
            ticket: The ticket being closed.
            ctx: Fresh ticket settings for the guild.

        Returns:
            ``(transcript_text, attachments)``, or ``(None, [])`` if the channel
            couldn't be read.
        """
        try:
            await channel.send(embed=embeds.closing_embed(ticket, ctx.settings.delete_delay_seconds))
        except discord.HTTPException as e:
            log.debug("Guild %s: couldn't post the closing notice in %s: %s", guild.id, channel, e)
        # Stop the applicant from posting while we archive.
        applicant = guild.get_member(ticket.user_id)
        if applicant is not None:
            try:
                overwrite = channel.overwrites_for(applicant)
                # discord.py sets these permission names on the class at import time, where a
                # type checker can't see them.
                overwrite.send_messages = False  # type: ignore[misc]
                await channel.set_permissions(applicant, overwrite=overwrite, reason="Ticket closing")
            except discord.HTTPException as e:
                log.debug("Guild %s: couldn't stop the applicant posting in %s: %s", guild.id, channel, e)
        try:
            return await build_transcript(channel, ticket)
        except discord.HTTPException as e:
            log.warning("Guild %s: couldn't read %s for the transcript: %s", guild.id, channel, e)
            return None, []

    async def _delete_channel(self, channel: discord.abc.GuildChannel, ticket: Ticket) -> None:
        """Delete a closed ticket's channel and record that it is gone.

        If Discord refuses, the channel stays and the ticket keeps pointing at it,
        so ``recover`` tries again on a later round.

        Args:
            channel: The ticket channel.
            ticket: The closed ticket it belongs to.
        """
        try:
            await channel.delete(reason=f"Ticket #{ticket.id} closed ({ticket.outcome})")
        except discord.NotFound:
            pass  # already gone, which is what we wanted
        except discord.Forbidden:
            await self.bot.report_issue(
                ticket.guild_id,
                ISSUE_DELETE_CHANNEL,
                "I couldn't delete a closed ticket channel. Give JoltBot Manage Channels in the ticket category.",
            )
            return
        except discord.HTTPException as e:
            log.warning("Guild %s: couldn't delete %s: %s", ticket.guild_id, channel, e)
            return
        await store.mark_channel_gone(self.db, ticket.id)

    async def retry_log(self, guild: discord.Guild, ticket_id: int) -> None:
        """Try again to log a closed ticket whose log post failed, then delete its channel.

        Such a ticket keeps its channel so nothing is lost. Once the log channel
        works again, this posts the decision and transcript and removes the
        channel, so admins don't have to clean up by hand. Safe to call more
        than once; a failure is tried again on the next recovery round.

        Args:
            guild: Guild the ticket belongs to.
            ticket_id: A ticket that is ``closed`` with ``logged`` False.
        """
        if ticket_id in self._busy:
            return
        self._busy.add(ticket_id)
        try:
            await self._retry_log(guild, ticket_id)
        except Exception:
            log.exception("Logging ticket #%s again failed; it will be retried automatically", ticket_id)
        finally:
            self._busy.discard(ticket_id)

    async def _retry_log(self, guild: discord.Guild, ticket_id: int) -> None:
        """The work of ``retry_log``, without its run-once guard and error handling."""
        ticket = await store.get(self.db, ticket_id)
        if ticket is None or ticket.status != STATUS_CLOSED or ticket.logged or ticket.channel_gone:
            return
        channel = guild.get_channel(ticket.channel_id) if ticket.channel_id else None
        if not isinstance(channel, discord.TextChannel):
            # The channel was deleted, so there is no transcript left to save. Post nothing:
            # a decision turning up in the log long after the fact would only confuse.
            await store.mark_channel_gone(self.db, ticket_id)
            return
        ctx = await load_context(self.bot, guild.id)
        log_channel = guild.get_channel(ctx.settings.log_channel_id or 0)
        if not isinstance(log_channel, discord.TextChannel):
            return  # still not fixed; the dashboard warning from the first try is still open
        perms = log_channel.permissions_for(guild.me)
        if not all(getattr(perms, name) for name in LOG_CHANNEL_PERMISSIONS):
            return
        try:
            text, attachments = await build_transcript(channel, ticket)
        except discord.HTTPException as e:
            log.warning("Guild %s: couldn't read %s for the transcript: %s", guild.id, channel, e)
            return
        if not await post_log(self.bot, guild, ctx, ticket, text, attachments, resumed=True):
            return
        await store.mark_logged(self.db, ticket_id)
        log.info("Guild %s: logged ticket #%s on a later try", guild.id, ticket_id)
        await asyncio.sleep(ctx.settings.delete_delay_seconds)
        await self._delete_channel(channel, ticket)

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
        for t in await store.list_for_guild(self.db, guild.id, STATUS_OPEN):
            if t.id in self._busy or t.id in self._creating:
                continue
            if t.channel_id is None:
                # Reserved but the channel was never created (crash or error mid-way).
                if store.older_than(t.created_at, STUCK_AFTER_SECONDS):
                    await store.delete_reservation(self.db, t.id)
                continue
            if guild.get_channel(t.channel_id) is None:
                if await store.claim(self.db, t.id, outcome=OUTCOME_CHANNEL_DELETED, handled_by=None):
                    self._spawn(self.finalize(t.id, resumed=True))
            elif guild.get_member(t.user_id) is None and await self._has_left(guild, t.user_id):
                if await store.claim(self.db, t.id, outcome=OUTCOME_LEFT, handled_by=None):
                    self._spawn(self.finalize(t.id, resumed=True))
        for t in await store.list_for_guild(self.db, guild.id, STATUS_CLOSING):
            # claimed_at is None for tickets copied from the old bot; treat those as stuck too.
            if t.id not in self._busy and (t.claimed_at is None or store.older_than(t.claimed_at, STUCK_AFTER_SECONDS)):
                self._spawn(self.finalize(t.id, resumed=True))
        for t in await store.list_closed_unlogged(self.db, guild.id):
            if t.id not in self._busy:
                self._spawn(self.retry_log(guild, t.id))
        for t in await store.list_closed_needing_delete(self.db, guild.id):
            if t.id in self._busy:
                continue
            channel = guild.get_channel(t.channel_id) if t.channel_id else None
            if channel is None:
                await store.mark_channel_gone(self.db, t.id)
            else:
                self._spawn(self._delete_channel(channel, t))


def _log_task_errors(task: asyncio.Task) -> None:
    """Done-callback for background ticket tasks: log an error that would otherwise vanish."""
    if not task.cancelled() and (exc := task.exception()):
        log.error("Background ticket task failed", exc_info=exc)
