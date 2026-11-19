"""Alliance request lifecycle: apply, accept, reject, cancel, and what follows a decision.

Views, commands and listeners stay thin and call into AllianceManager, so all
the rules (who may decide, one request at a time, the cooldown, the role swap,
race protection) live in this one file.

Every decision method returns the text to show whoever triggered it: a ✅/❌
line when it went through, or the reason it didn't. Nothing here raises for an
ordinary problem.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import discord

from ...core.db import Database
from ...core.limits import AUDIT_REASON_MAX
from ...core.models import AllianceRequest
from ...core.types import utcnow

# hierarchy_problems() explains, role by role, why JoltBot can't give or remove one.
from ..tickets.checks import hierarchy_problems
from ..tickets.embeds import ts
from . import embeds, store
from .checks import can_review
from .constants import (
    ISSUE_LOG_CHANNEL,
    ISSUE_REQUEST_CHANNEL,
    STATUS_ACCEPTED,
    STATUS_CANCELLED,
    STATUS_CLOSED,
    STATUS_PENDING,
    STATUS_REJECTED,
)
from .context import AllianceContext, load_context
from .settings import Alliance, AllianceSettings
from .views import GONE, NOT_SET_UP, REJECT_REASON_MAX, RequestDecisionView, already_handled

if TYPE_CHECKING:
    from ...bot.client import JoltBot

log = logging.getLogger(__name__)

DISCORD_TROUBLE = "Discord didn't respond properly. Nothing was changed; please try again in a moment."
SWITCHED_OFF = "Alliance requests are switched off on this server."
OWN_REQUEST = "You can't decide your own request. Ask another leader of the alliance."
ROLE_CHANGE_BLOCKED = (
    "❌ Discord blocked the role change, so nothing was changed. JoltBot needs the **Manage Roles** permission, "
    "and its own role must sit above every alliance member role (Server Settings → Roles). "
    "Once that is fixed, press Accept again."
)
DM_FAILED_NOTE = "Couldn't DM the member (their DMs are closed), so they were not told directly."
MESSAGE_GONE_NOTE = "The request message couldn't be updated (it may have been deleted)."
# Statuses the member is told about. Someone who left the server isn't messaged.
DM_STATUSES = (STATUS_ACCEPTED, STATUS_REJECTED, STATUS_CANCELLED)


def _pending_text(request: AllianceRequest) -> str:
    """What a member is told when they already have a request waiting."""
    return (
        f"You already have a pending request with **{request.alliance_name}**. "
        "Wait for its leadership to decide before sending another."
    )


class AllianceManager:
    """Discord-side alliance request lifecycle used by the views and the alliances cog.

    Args:
        bot: Parent ``JoltBot`` (provides ``db`` and issue reporting).
    """

    def __init__(self, bot: JoltBot) -> None:
        """Create the manager. It keeps no state of its own: everything is in the database."""
        self.bot = bot

    @property
    def db(self) -> Database:
        """The bot's database, for the ``store`` functions."""
        return self.bot.db

    async def _get_member(self, guild: discord.Guild, user_id: int) -> discord.Member | None:
        """Find a member, asking Discord when they are not in the bot's cache.

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

    # ------------------------------------------------------------------ asking to join

    def _retry_at(self, request: AllianceRequest, settings: AllianceSettings) -> datetime | None:
        """When a rejected member may ask that alliance again. ``None`` if they already may."""
        if request.decided_at is None or settings.reject_cooldown_hours <= 0:
            return None
        retry_at = request.decided_at + timedelta(hours=settings.reject_cooldown_hours)
        return retry_at if retry_at > utcnow() else None

    async def apply_problem(self, member: discord.Member, alliance: Alliance, settings: AllianceSettings) -> str | None:
        """Why this member can't ask to join this alliance right now, if anything.

        Checked when they pick from the menu and again when they submit the form.

        Args:
            member: The member asking.
            alliance: The alliance they picked.
            settings: The server's alliance settings (the cooldown).

        Returns:
            Text for the member, or ``None`` if they may go ahead. Holding
            another alliance's role is fine: that is how a member switches, and
            the old role stays until the new alliance accepts.
        """
        if any(role.id == alliance.member_role_id for role in member.roles):
            return f"You're already a member of **{alliance.name}**."
        existing = await store.get_active_by_user(self.db, member.guild.id, member.id)
        if existing is not None:
            return _pending_text(existing)
        # The cooldown is per alliance: picking the wrong one by mistake shouldn't block the right one.
        last = await store.last_rejection(self.db, member.guild.id, member.id, alliance.member_role_id)
        if last is not None and (retry_at := self._retry_at(last, settings)) is not None:
            return (
                f"Your last request to join **{alliance.name}** was not accepted. "
                f"You can ask again {ts(retry_at, 'R')}."
            )
        return None

    async def create_request(
        self, member: discord.Member, alliance_role_id: int, ingame_name: str, governor_id: str
    ) -> str:
        """Save a join request and post it in that alliance's request channel.

        The row is saved first, so a second request sent at the same moment is
        refused by the database. If the message can't be posted, the row is
        removed again and the member can simply retry.

        Args:
            member: The member asking.
            alliance_role_id: The picked alliance's member role (its identity).
            ingame_name: Their in-game name, as typed.
            governor_id: Their governor ID, as typed.

        Returns:
            Text for the member: a ✅ confirmation, or why it wasn't sent.
        """
        guild = member.guild
        ctx = await load_context(self.bot, guild.id)
        if not ctx.ready:
            return NOT_SET_UP
        alliance = ctx.settings.alliance_for(alliance_role_id)
        if alliance is None:
            return "❌ That alliance is no longer available here. Ask an admin to post a fresh menu."
        if problem := await self.apply_problem(member, alliance, ctx.settings):
            return problem
        failed = f"❌ Requests for **{alliance.name}** aren't set up correctly yet. Please let a staff member know."
        channel = guild.get_channel(alliance.request_channel_id)
        if not isinstance(channel, discord.TextChannel):
            await self.bot.report_issue(
                guild.id,
                ISSUE_REQUEST_CHANNEL,
                f"The request channel of {alliance.name} no longer exists, so members can't ask to join it. "
                "Pick another on the dashboard (Alliances).",
            )
            return failed

        request_id = await store.create(
            self.db,
            guild_id=guild.id,
            user_id=member.id,
            username=member.name,
            alliance_role_id=alliance.member_role_id,
            alliance_name=alliance.name,
            ingame_name=ingame_name,
            governor_id=governor_id,
            member_joined_at=member.joined_at,
        )
        request = await store.get(self.db, request_id) if request_id is not None else None
        if request is None:
            existing = await store.get_active_by_user(self.db, guild.id, member.id)
            return _pending_text(existing) if existing else DISCORD_TROUBLE

        try:
            message = await channel.send(
                embed=embeds.request_embed(request, ctx.colour),
                view=RequestDecisionView(self.bot),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            await store.delete_unposted(self.db, request.id)
            await self.bot.report_issue(
                guild.id,
                ISSUE_REQUEST_CHANNEL,
                f"I can't post join requests in #{channel.name}. JoltBot needs View Channel, Send Messages and "
                "Embed Links there.",
            )
            return failed
        except (discord.HTTPException, OSError) as e:
            await store.delete_unposted(self.db, request.id)
            log.warning("Guild %s: couldn't post alliance request #%s: %s", guild.id, request.id, e)
            return "❌ Discord didn't take your request right now. Please try again shortly."

        if not await store.set_message(self.db, request.id, channel.id, message.id):
            # Closed while it was being posted (the member left). Nothing points at the message.
            try:
                await message.delete()
            except (discord.HTTPException, OSError) as e:
                log.debug("Guild %s: couldn't remove an unused request message: %s", guild.id, e)
            return "❌ Your request was closed before it finished sending. Please try again."
        await self.bot.resolve_issue(guild.id, ISSUE_REQUEST_CHANNEL)
        log.info("Guild %s: alliance request #%s by %s for %s", guild.id, request.id, member.id, alliance.name)
        return (
            f"✅ Your request to join **{alliance.name}** was sent to its leadership. "
            "You'll get a message when they decide."
        )

    # ------------------------------------------------------------------ who may decide

    async def _review(
        self, guild: discord.Guild, reviewer: object, request: AllianceRequest
    ) -> tuple[AllianceContext | None, Alliance | None, str | None]:
        """Check that ``reviewer`` may decide ``request`` in ``guild``, right now.

        Settings and roles are read fresh, so removing someone's reviewer role
        (or changing the roles on the dashboard) takes effect on their next click.

        Returns:
            ``(ctx, alliance, None)`` when they may, or ``(None, None, text)``
            with the reason for the reviewer.
        """
        # The guild check stops a request of one server ever being decided from another.
        if request.guild_id != guild.id:
            return None, None, GONE
        ctx = await load_context(self.bot, guild.id)
        if not ctx.enabled:
            return None, None, SWITCHED_OFF
        alliance = ctx.settings.alliance_for(request.alliance_role_id)
        if alliance is None:
            return (
                None,
                None,
                f"**{request.alliance_name}** is no longer set up as an alliance here, so this request can't be "
                "decided. An admin can clear it with `/alliances cancel`.",
            )
        if not can_review(reviewer, alliance, ctx.settings):
            return None, None, f"Only **{alliance.name}** leadership can decide this request."
        if isinstance(reviewer, discord.Member) and reviewer.id == request.user_id:
            return None, None, OWN_REQUEST
        if request.status != STATUS_PENDING:
            return None, None, already_handled(request)
        return ctx, alliance, None

    async def review_problem(self, guild: discord.Guild, reviewer: object, request: AllianceRequest) -> str | None:
        """Why ``reviewer`` can't decide ``request``, if anything. Used before the reject form opens.

        Args:
            guild: The server the click came from.
            reviewer: Whoever clicked.
            request: The request under the buttons.

        Returns:
            Text for the reviewer, or ``None`` if they may decide it.
        """
        _, _, problem = await self._review(guild, reviewer, request)
        return problem

    async def _already_handled(self, request_id: int) -> str:
        """Say who got to a request first, after losing the race for it."""
        current = await store.get(self.db, request_id)
        return already_handled(current) if current is not None else GONE

    # ------------------------------------------------------------------ decisions

    async def accept(self, guild: discord.Guild, reviewer: discord.Member, request: AllianceRequest) -> str:
        """Accept a request: give the alliance's member role and take away any other alliance's.

        The member's role list is set in a single Discord call, so there is no
        moment where they hold two alliance roles, or lost the old one without
        getting the new one. If that call fails, the request is reopened.

        Args:
            guild: The server the click came from.
            reviewer: Whoever clicked Accept. Checked here against the alliance's
                reviewer roles and the override roles.
            request: The request under the buttons.

        Returns:
            Text for the reviewer: a ✅ line, or why nothing was changed.
        """
        ctx, alliance, problem = await self._review(guild, reviewer, request)
        if ctx is None or alliance is None:
            return problem or GONE
        try:
            member = await self._get_member(guild, request.user_id)
        except (discord.HTTPException, OSError):
            return DISCORD_TROUBLE
        if member is None:
            return await self._close_left(guild, ctx, request)
        role = guild.get_role(alliance.member_role_id)
        if role is None:
            return f"❌ The member role of **{alliance.name}** no longer exists. Pick another on the dashboard."
        other_ids = ctx.settings.member_role_ids - {role.id}
        leaving = [r for r in member.roles if r.id in other_ids]
        if problems := hierarchy_problems(guild, [role, *leaving]):
            return "❌ " + "\n".join(problems)

        if not await store.claim(self.db, request.id, decided_by=reviewer.id):
            return await self._already_handled(request.id)
        keep = [r for r in member.roles if r.id not in other_ids and r.id != role.id and r != guild.default_role]
        reason = f"Joined {alliance.name}: accepted by {reviewer} (request #{request.id})"
        try:
            await member.edit(roles=[*keep, role], reason=reason[:AUDIT_REASON_MAX])
        except discord.Forbidden:
            await store.release(self.db, request.id)
            return ROLE_CHANGE_BLOCKED
        except Exception:
            # e.g. the connection dropped. Reopen, so the request never gets stuck.
            await store.release(self.db, request.id)
            log.exception("Guild %s: accept of alliance request #%s failed", guild.id, request.id)
            return DISCORD_TROUBLE
        await store.finish(self.db, request.id, status=STATUS_ACCEPTED)
        log.info("Guild %s: alliance request #%s accepted by %s", guild.id, request.id, reviewer.id)
        await self._announce(guild, ctx, request.id, member)
        swapped = f" Removed: {', '.join(r.mention for r in leaving)}." if leaving else ""
        return f"✅ Accepted <@{request.user_id}> into **{alliance.name}**.{swapped}"

    async def reject(
        self, guild: discord.Guild, reviewer: discord.Member, request: AllianceRequest, reason: str | None
    ) -> str:
        """Reject a request. No roles change; the member is told, with the reason if one was typed.

        Args:
            guild: The server the form came from.
            reviewer: Whoever submitted the reject form. Checked again here.
            request: The request being rejected.
            reason: Optional reason, sent to the member.

        Returns:
            Text for the reviewer: a ❌ line, or why nothing was changed.
        """
        ctx, alliance, problem = await self._review(guild, reviewer, request)
        if ctx is None or alliance is None:
            return problem or GONE
        try:
            member = await self._get_member(guild, request.user_id)
        except (discord.HTTPException, OSError):
            return DISCORD_TROUBLE
        if member is None:
            return await self._close_left(guild, ctx, request)
        cleaned = (reason or "").strip()[:REJECT_REASON_MAX] or None
        if not await store.decide(self.db, request.id, status=STATUS_REJECTED, decided_by=reviewer.id, reason=cleaned):
            return await self._already_handled(request.id)
        log.info("Guild %s: alliance request #%s rejected by %s", guild.id, request.id, reviewer.id)
        await self._announce(guild, ctx, request.id, member)
        return f"❌ Rejected <@{request.user_id}>'s request to join **{alliance.name}**."

    async def cancel(self, guild: discord.Guild, admin: discord.Member, request: AllianceRequest) -> str:
        """Cancel a waiting request (``/alliances cancel``). No cooldown follows.

        The command checks who may run it; this only makes sure the request
        belongs to this server and is still waiting.

        Returns:
            Text for the admin: a ✅ line, or why nothing was changed.
        """
        if request.guild_id != guild.id:
            return GONE
        if not await store.decide(self.db, request.id, status=STATUS_CANCELLED, decided_by=admin.id):
            return await self._already_handled(request.id)
        try:
            member = await self._get_member(guild, request.user_id)
        except (discord.HTTPException, OSError):
            member = None  # the request is cancelled either way; they just don't get the DM
        ctx = await load_context(self.bot, guild.id)
        log.info("Guild %s: alliance request #%s cancelled by %s", guild.id, request.id, admin.id)
        await self._announce(guild, ctx, request.id, member)
        return f"✅ Cancelled <@{request.user_id}>'s request to join **{request.alliance_name}**."

    async def _close_left(self, guild: discord.Guild, ctx: AllianceContext, request: AllianceRequest) -> str:
        """Close the request of a member who is no longer in the server; returns the text for the reviewer."""
        if await store.decide(self.db, request.id, status=STATUS_CLOSED, decided_by=None):
            await self._announce(guild, ctx, request.id, None)
        return f"🚪 <@{request.user_id}> has left the server, so this request was closed."

    async def handle_member_left(self, guild_id: int, user_id: int) -> None:
        """Close the waiting request of a member who left, was kicked or was banned.

        Args:
            guild_id: The server they left.
            user_id: Who left.
        """
        request = await store.get_active_by_user(self.db, guild_id, user_id)
        if request is None:
            return
        if not await store.decide(self.db, request.id, status=STATUS_CLOSED, decided_by=None):
            return  # being accepted at this very moment; that click finishes it
        guild = self.bot.get_guild(guild_id)
        if guild is not None:
            await self._announce(guild, await load_context(self.bot, guild_id), request.id, None)

    # ------------------------------------------------------------------ after a decision

    async def _announce(
        self, guild: discord.Guild, ctx: AllianceContext, request_id: int, member: discord.Member | None
    ) -> None:
        """Show a decision everywhere it belongs: the request message, a DM to the member, the log channel.

        None of these can undo the decision. Whatever fails is written as a
        note in the log post instead.

        Args:
            guild: The server the request belongs to.
            ctx: Fresh alliance settings for the server.
            request_id: The request, already saved with its decision.
            member: The member to DM, or ``None`` if they left.
        """
        request = await store.get(self.db, request_id)
        if request is None:
            return
        notes: list[str] = []
        if not await self._update_message(guild, ctx, request):
            notes.append(MESSAGE_GONE_NOTE)
        if member is not None and request.status in DM_STATUSES:
            if request.status == STATUS_ACCEPTED:
                dm = embeds.accepted_dm(request, guild)
            elif request.status == STATUS_REJECTED:
                dm = embeds.rejected_dm(request, guild, self._retry_at(request, ctx.settings))
            else:
                dm = embeds.cancelled_dm(request, guild)
            try:
                await member.send(embed=dm)
            except (discord.HTTPException, OSError):
                # Usually closed DMs. The decision stands; the log says they weren't told.
                notes.append(DM_FAILED_NOTE)
        await self._post_log(guild, ctx, request, notes)

    async def _update_message(self, guild: discord.Guild, ctx: AllianceContext, request: AllianceRequest) -> bool:
        """Redraw the request's message with its decision and greyed-out buttons.

        Returns:
            False if the message couldn't be edited (deleted, or the channel is gone).
        """
        channel = guild.get_channel(request.channel_id or 0)
        if not isinstance(channel, discord.TextChannel) or request.message_id is None:
            return False
        try:
            await channel.get_partial_message(request.message_id).edit(
                embed=embeds.request_embed(request, ctx.colour), view=RequestDecisionView(self.bot, disabled=True)
            )
        except (discord.HTTPException, OSError) as e:
            log.debug("Guild %s: couldn't update the message of alliance request #%s: %s", guild.id, request.id, e)
            return False
        return True

    async def _post_log(
        self, guild: discord.Guild, ctx: AllianceContext, request: AllianceRequest, notes: list[str]
    ) -> None:
        """Post the decision in the log channel.

        A missing log channel, or one JoltBot can't post in, is reported to the
        server's admins as a dashboard issue that clears itself on the next
        post that works.
        """
        channel = guild.get_channel(ctx.settings.log_channel_id or 0)
        if not isinstance(channel, discord.TextChannel):
            await self.bot.report_issue(
                guild.id,
                ISSUE_LOG_CHANNEL,
                "The alliance log channel is missing, so decisions aren't being logged. "
                "Pick one on the dashboard (Alliances).",
            )
            return
        try:
            await channel.send(embed=embeds.log_embed(request, notes), allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            await self.bot.report_issue(
                guild.id,
                ISSUE_LOG_CHANNEL,
                f"I can't post in #{channel.name}, so alliance decisions aren't being logged. JoltBot needs "
                "View Channel, Send Messages and Embed Links there.",
            )
        except (discord.HTTPException, OSError) as e:
            log.warning("Guild %s: couldn't log alliance request #%s: %s", guild.id, request.id, e)
        else:
            await self.bot.resolve_issue(guild.id, ISSUE_LOG_CHANNEL)
