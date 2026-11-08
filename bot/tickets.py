"""Ticket lifecycle: create, accept, deny, close, finalize (transcript → log → delete), recover.

Views and listeners stay thin and call into TicketManager, so all the rules
(ordering, rollback on failure, race protection) live in one file.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from .database import Ticket
from .utils import embeds
from .utils.checks import hierarchy_problems, kick_problem
from .utils.transcript import build_transcript, transcript_file

if TYPE_CHECKING:
    from .client import TicketBot

log = logging.getLogger(__name__)

MAX_REUPLOADS = 25  # safety cap on attachments re-uploaded to the log channel per ticket


@dataclass(frozen=True)
class ApplicationAnswers:
    governor_name: str
    governor_id: str
    power: str
    alliance: str
    extra: str | None


def ticket_channel_name(member: discord.Member) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", member.name.lower()).strip("-") or str(member.id)
    return f"ticket-{slug}"[:100]


class TicketManager:
    def __init__(self, bot: "TicketBot") -> None:
        self.bot = bot
        self._finalizing: set[int] = set()
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ helpers

    @property
    def cfg(self):
        return self.bot.cfg

    @property
    def db(self):
        return self.bot.db

    def guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.cfg.guild_id)

    def _spawn(self, coro) -> None:
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
        except (discord.Forbidden, discord.HTTPException):
            return False

    def _overwrites(self, guild: discord.Guild, member: discord.Member) -> dict:
        staff_perms = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, attach_files=True, embed_links=True,
            read_message_history=True, manage_messages=True,
        )
        ow: dict = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, embed_links=True, attach_files=True,
                read_message_history=True, manage_channels=True, manage_messages=True,
            ),
            member: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, attach_files=True, embed_links=True,
                read_message_history=True,
            ),
        }
        for rid in self.cfg.approval_role_ids:
            if role := guild.get_role(rid):
                ow[role] = staff_perms
        # A custom ping role (e.g. [Staff]) must be able to see the ticket, or the ping won't notify them.
        # It gets read/write access but NOT button powers — those are checked by role in the view.
        for rid in self.cfg.effective_ping_role_ids:
            role = guild.get_role(rid)
            if role and role not in ow:
                ow[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        return ow

    # ------------------------------------------------------------------ open

    async def active_ticket_for(self, member: discord.Member) -> Ticket | None:
        t = await self.db.get_active_by_user(member.guild.id, member.id)
        if t and t.status == "open" and t.channel_id and member.guild.get_channel(t.channel_id) is None:
            # Channel vanished while the bot was offline. Clean up and treat as no ticket.
            if await self.db.claim(t.id, outcome="channel_deleted", handled_by=None):
                self._spawn(self.finalize(t.id))
            return None
        return t

    async def create_ticket(
        self, member: discord.Member, answers: ApplicationAnswers
    ) -> tuple[discord.TextChannel | None, str | None]:
        """Returns (channel, None) on success or (None, error message for the applicant)."""
        guild = member.guild
        category = guild.get_channel(self.cfg.tickets_category_id)
        if not isinstance(category, discord.CategoryChannel):
            log.error("Tickets category %s not found — check channels.tickets_category", self.cfg.tickets_category_id)
            return None, "The ticket system isn't set up correctly yet. Please ping a staff member."

        ticket_id = await self.db.reserve_ticket(
            guild_id=guild.id, user_id=member.id, username=member.name,
            governor_name=answers.governor_name, governor_id=answers.governor_id,
            power=answers.power, alliance=answers.alliance, extra=answers.extra,
        )
        if ticket_id is None:
            existing = await self.db.get_active_by_user(guild.id, member.id)
            if existing and existing.channel_id and existing.status == "open":
                return None, f"You already have an open application: <#{existing.channel_id}>"
            return None, "Your previous ticket is still being closed. Try again in a few seconds."

        try:
            channel = await guild.create_text_channel(
                name=ticket_channel_name(member),
                category=category,
                overwrites=self._overwrites(guild, member),
                topic=f"Verification ticket #{ticket_id} for {member} ({member.id})",
                reason=f"Verification ticket for {member} ({member.id})",
            )
        except discord.Forbidden:
            await self.db.delete_ticket(ticket_id)
            log.error(
                "Forbidden creating ticket channel. The bot needs Manage Channels + Manage Roles in the "
                "'%s' category, and must itself have every permission it grants (View, Send, Attach, Embed, "
                "Read History, Manage Messages). Run /verify-diagnostics.", category.name,
            )
            return None, "I couldn't create your ticket (missing permissions). Please ping a staff member."
        except discord.HTTPException as e:
            await self.db.delete_ticket(ticket_id)
            log.error("Failed to create ticket channel: %s", e)
            hint = " The ticket category may be full (50 channels max)." if "50" in str(e) else ""
            return None, f"Discord refused to create the ticket right now.{hint} Please try again shortly or ping staff."

        await self.db.set_ticket_channel(ticket_id, channel.id)
        ticket = await self.db.get(ticket_id)
        assert ticket is not None

        from .views.ticket_controls import TicketControlView  # local import avoids a cycle

        pings = " ".join(f"<@&{rid}>" for rid in self.cfg.effective_ping_role_ids)
        intro = embeds.fmt(self.cfg.messages["ticket_intro"], user=member.mention, guild=guild.name)
        try:
            await channel.send(
                content=f"{member.mention} {pings}".strip(),
                embed=embeds.application_embed(member, ticket),
                view=TicketControlView(self.bot),
                allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False),
            )
            await channel.send(intro, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            log.error("Created %s but couldn't post the application in it: %s", channel, e)
        log.info("Opened ticket #%s (%s) for %s (%s)", ticket_id, channel.name, member, member.id)
        return channel, None

    # ------------------------------------------------------------------ decisions

    async def accept(
        self, guild: discord.Guild, staff: discord.Member, ticket: Ticket, alliance_role: discord.Role | None
    ) -> str | None:
        """Returns None on success, or an error message for the staff member."""
        member = await self._get_member(guild, ticket.user_id)
        if member is None:
            return "The applicant is no longer in the server. Use 🔒 Close instead."
        member_role = guild.get_role(self.cfg.member_role_id)
        unverified = guild.get_role(self.cfg.unverified_role_id)
        if member_role is None:
            return "The Member role in config.json doesn't exist. Run /verify-diagnostics."
        problems = hierarchy_problems(guild, [r for r in (member_role, unverified, alliance_role) if r])
        if problems:
            return "\n".join(problems)

        if not await self.db.claim(
            ticket.id, outcome="accepted", handled_by=staff.id,
            alliance_role_id=alliance_role.id if alliance_role else None,
        ):
            return "Someone else already handled this ticket."

        reason = f"Application accepted by {staff} (ticket #{ticket.id})"
        try:
            # Add first, then remove: if removal fails they keep access instead of being locked out.
            await member.add_roles(*[r for r in (member_role, alliance_role) if r], reason=reason)
            if unverified and unverified in member.roles:
                await member.remove_roles(unverified, reason=reason)
        except discord.Forbidden:
            await self.db.release(ticket.id)
            return ("Discord blocked the role change. My role must sit ABOVE Member, Unverified and the "
                    "alliance roles (Server Settings → Roles). Run /verify-diagnostics.")
        except discord.HTTPException as e:
            await self.db.release(ticket.id)
            return f"Discord error while assigning roles ({e.status}). Try again."

        if not await self._dm(member, embeds.welcome_dm(self.cfg, guild, alliance_role)):
            await self.db.add_note(ticket.id, "Welcome DM failed (applicant's DMs are closed).")
        log.info("Ticket #%s accepted by %s", ticket.id, staff)
        self._spawn(self.finalize(ticket.id))
        return None

    async def deny(
        self, guild: discord.Guild, staff: discord.Member, ticket: Ticket, reason: str, kick: bool
    ) -> str | None:
        member = await self._get_member(guild, ticket.user_id)
        if kick and member is not None and (problem := kick_problem(guild, member)):
            return problem

        if not await self.db.claim(ticket.id, outcome="denied", handled_by=staff.id, reason=reason, kicked=False):
            return "Someone else already handled this ticket."

        if member is None:
            await self.db.add_note(ticket.id, "Applicant had already left; no DM sent.")
        else:
            # DM *before* kicking — once they're gone we share no server and the DM usually fails.
            if not await self._dm(member, embeds.deny_dm(self.cfg, guild, reason, kick)):
                await self.db.add_note(ticket.id, "Denial DM failed (applicant's DMs are closed).")
            if kick:
                try:
                    await member.kick(reason=f"Application denied by {staff}: {reason}"[:512])
                    await self.db.set_kicked(ticket.id, True)
                except discord.HTTPException as e:
                    await self.db.add_note(ticket.id, f"Kick failed ({e.status}); applicant kept Unverified.")
            else:
                unverified = guild.get_role(self.cfg.unverified_role_id)
                if unverified and unverified not in member.roles:
                    try:
                        await member.add_roles(unverified, reason="Application denied — kept Unverified")
                    except discord.HTTPException:
                        pass
        log.info("Ticket #%s denied by %s (kick=%s)", ticket.id, staff, kick)
        self._spawn(self.finalize(ticket.id))
        return None

    async def close(self, staff: discord.Member, ticket: Ticket) -> str | None:
        if not await self.db.claim(ticket.id, outcome="closed", handled_by=staff.id):
            return "Someone else already handled this ticket."
        log.info("Ticket #%s closed by %s", ticket.id, staff)
        self._spawn(self.finalize(ticket.id))
        return None

    # ------------------------------------------------------------------ automatic closes

    async def handle_member_left(self, user_id: int) -> None:
        guild = self.guild()
        if guild is None:
            return
        t = await self.db.get_active_by_user(guild.id, user_id)
        if t and await self.db.claim(t.id, outcome="left", handled_by=None):
            log.info("Applicant %s left; auto-closing ticket #%s", user_id, t.id)
            self._spawn(self.finalize(t.id))

    async def handle_channel_deleted(self, channel_id: int) -> None:
        t = await self.db.get_by_channel(channel_id)
        if t is None:
            return
        if t.status == "open" and await self.db.claim(t.id, outcome="channel_deleted", handled_by=None):
            log.info("Ticket #%s channel deleted manually; logging it", t.id)
            self._spawn(self.finalize(t.id))
        elif t.status == "closed":
            await self.db.mark_channel_gone(t.id)

    # ------------------------------------------------------------------ finalize

    async def finalize(self, ticket_id: int, *, resumed: bool = False) -> None:
        """Transcript → #ticket-logs → mark closed → delete channel after a delay.

        Safe to call more than once; also used to resume work interrupted by a restart.
        """
        if ticket_id in self._finalizing:
            return
        self._finalizing.add(ticket_id)
        try:
            await self._finalize(ticket_id, resumed)
        finally:
            self._finalizing.discard(ticket_id)

    async def _finalize(self, ticket_id: int, resumed: bool) -> None:
        guild = self.guild()
        ticket = await self.db.get(ticket_id)
        if guild is None or ticket is None or ticket.status == "closed":
            return
        channel = guild.get_channel(ticket.channel_id) if ticket.channel_id else None
        if not isinstance(channel, discord.TextChannel):
            channel = None

        text: str | None = None
        attachments: list[discord.Attachment] = []
        if channel is not None:
            delay = self.cfg.delete_delay_seconds
            try:
                await channel.send(embed=embeds.closing_embed(ticket, delay, will_delete=True))
            except discord.HTTPException:
                pass
            # Stop the applicant from posting while we archive.
            applicant = guild.get_member(ticket.user_id)
            if applicant is not None:
                try:
                    ow = channel.overwrites_for(applicant)
                    ow.send_messages = False
                    await channel.set_permissions(applicant, overwrite=ow, reason="Ticket closing")
                except discord.HTTPException:
                    pass
            try:
                text, attachments = await build_transcript(channel, ticket)
            except discord.HTTPException as e:
                log.error("Couldn't read history of %s for the transcript: %s", channel, e)

        # Re-read so notes added during accept/deny are included, and stamp closed_at now.
        # Status stays 'closing' until the log is posted, so a crash here is retried on restart.
        await self.db.stamp_closed_at(ticket_id)
        ticket = await self.db.get(ticket_id)
        assert ticket is not None
        logged = await self._post_log(guild, ticket, text, attachments, resumed)
        await self.db.mark_closed(ticket_id, logged=logged)

        if channel is None:
            await self.db.mark_channel_gone(ticket_id)
            return
        if not logged:
            try:
                await channel.send(
                    "⚠️ I couldn't post the transcript to the ticket-log channel, so I did **not** delete this "
                    "channel (nothing is lost). Run `/verify-diagnostics`, fix the log channel permissions, then "
                    "delete this channel manually."
                )
            except discord.HTTPException:
                pass
            return
        await asyncio.sleep(self.cfg.delete_delay_seconds)
        await self._delete_channel(channel, ticket)

    async def _delete_channel(self, channel: discord.abc.GuildChannel, ticket: Ticket) -> None:
        try:
            await channel.delete(reason=f"Ticket #{ticket.id} closed ({ticket.outcome})")
        except discord.NotFound:
            pass
        except discord.Forbidden:
            log.error("Missing Manage Channels to delete %s (ticket #%s)", channel, ticket.id)
            return
        except discord.HTTPException as e:
            log.error("Couldn't delete %s: %s", channel, e)
            return
        await self.db.mark_channel_gone(ticket.id)

    async def _post_log(
        self,
        guild: discord.Guild,
        ticket: Ticket,
        text: str | None,
        attachments: list[discord.Attachment],
        resumed: bool,
    ) -> bool:
        log_ch = guild.get_channel(self.cfg.ticket_logs_channel_id)
        if not isinstance(log_ch, discord.TextChannel):
            log.error("Ticket-log channel %s not found — check channels.ticket_logs", self.cfg.ticket_logs_channel_id)
            return False
        files = [transcript_file(text, ticket)] if text else []
        try:
            await log_ch.send(
                embed=embeds.log_embed(ticket, resumed=resumed, transcript_ok=text is not None),
                files=files,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            log.error("Missing permission to post in #%s (need View, Send, Embed Links, Attach Files)", log_ch.name)
            return False
        except discord.HTTPException as e:
            log.error("Failed to post ticket log: %s", e)
            return False

        if self.cfg.reupload_attachments and attachments:
            await self._reupload(log_ch, ticket, attachments)
        return True

    async def _reupload(self, log_ch: discord.TextChannel, ticket: Ticket, attachments: list[discord.Attachment]) -> None:
        """Discord deletes attachments with the channel, so copy screenshots into the log channel."""
        limit = log_ch.guild.filesize_limit
        batch: list[discord.File] = []
        batch_size = 0
        skipped: list[str] = []

        async def flush() -> None:
            nonlocal batch, batch_size
            if not batch:
                return
            try:
                await log_ch.send(f"📎 Attachments from ticket #{ticket.id} (<@{ticket.user_id}>)", files=batch,
                                  allowed_mentions=discord.AllowedMentions.none())
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
            extra = len(attachments) - MAX_REUPLOADS
            msg = f"⚠️ Ticket #{ticket.id}: couldn't re-upload {', '.join(skipped) or ''}"
            if extra > 0:
                msg += f" (+{extra} more over the {MAX_REUPLOADS}-file cap)"
            try:
                await log_ch.send(msg[:2000])
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------------ startup recovery

    async def reconcile(self) -> None:
        """Bring the DB and Discord back in sync after a restart/crash."""
        guild = self.guild()
        if guild is None:
            return
        for t in await self.db.list_by_status("open"):
            if t.channel_id is None:
                # Crashed between reserving the row and creating the channel.
                await self.db.delete_ticket(t.id)
                continue
            if guild.get_channel(t.channel_id) is None:
                if await self.db.claim(t.id, outcome="channel_deleted", handled_by=None):
                    self._spawn(self.finalize(t.id, resumed=True))
            elif guild.get_member(t.user_id) is None:
                if await self.db.claim(t.id, outcome="left", handled_by=None):
                    self._spawn(self.finalize(t.id, resumed=True))
        for t in await self.db.list_by_status("closing"):
            self._spawn(self.finalize(t.id, resumed=True))
        for t in await self.db.list_closed_needing_delete():
            channel = guild.get_channel(t.channel_id) if t.channel_id else None
            if channel is None:
                await self.db.mark_channel_gone(t.id)
            else:
                self._spawn(self._delete_channel(channel, t))


def _log_task_errors(task: asyncio.Task) -> None:
    if not task.cancelled() and (exc := task.exception()):
        log.error("Background ticket task failed", exc_info=exc)
