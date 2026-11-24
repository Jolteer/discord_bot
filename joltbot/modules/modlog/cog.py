"""Mod log module, bot side: the listeners that turn Discord events into log entries.

Who owns what (this is what keeps one change from being logged twice):

* Gateway events (``on_member_update``, ``on_raw_message_delete`` ...) say *what*
  changed. Each kind of change has exactly one gateway listener here, and that
  listener creates the log entry.
* The audit log says *who* did it. ``on_audit_log_entry_create`` only puts each
  entry into a short-lived buffer; the gateway listener looks its actor up there.
  It creates an entry itself for just two things no gateway event describes
  well enough: a kick, and a webhook being created or deleted.

Every listener loads the settings of the guild the event happened in, and does
nothing if logging is switched off there or the event is ignored.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

import discord
from discord.ext import commands, tasks

from ...core.types import utcnow
from . import diffs, retention
from .constants import (
    DELETED_BY_UNKNOWN,
    EVENT_AVATAR,
    EVENT_BAN,
    EVENT_BULK_DELETE,
    EVENT_CHANNEL_CREATE,
    EVENT_CHANNEL_DELETE,
    EVENT_CHANNEL_UPDATE,
    EVENT_EMOJI,
    EVENT_KICK,
    EVENT_MEMBER_JOIN,
    EVENT_MEMBER_LEAVE,
    EVENT_MEMBER_ROLES,
    EVENT_MESSAGE_DELETE,
    EVENT_MESSAGE_EDIT,
    EVENT_NICKNAME,
    EVENT_ROLE_CREATE,
    EVENT_ROLE_DELETE,
    EVENT_ROLE_UPDATE,
    EVENT_SERVER_UPDATE,
    EVENT_STICKER,
    EVENT_TIMEOUT_ADD,
    EVENT_TIMEOUT_REMOVE,
    EVENT_UNBAN,
    EVENT_VOICE_JOIN,
    EVENT_VOICE_LEAVE,
    EVENT_VOICE_MOVE,
    EVENT_WEBHOOK,
    TARGET_CHANNEL,
    TARGET_OTHER,
    TARGET_ROLE,
)
from .correlate import SEEN_BAN, SEEN_MESSAGE_DELETE, SEEN_VOICE_LEAVE, SEEN_VOICE_MOVE, AuditRecord
from .events import (
    STORED_BULK_CONTENT_MAX,
    STORED_BULK_MESSAGES,
    STORED_CONTENT_MAX,
    STORED_VALUE_MAX,
    Facts,
    LogEvent,
    cut,
)
from .service import ModLogManager

if TYPE_CHECKING:
    from ...bot.client import JoltBot

log = logging.getLogger(__name__)

NEW_ACCOUNT_DAYS = 7  # a joining account younger than this is flagged
STORED_ATTACHMENTS = 10  # attachment links kept per message
# An edit of a message that isn't in memory is only logged if Discord says it was edited
# this recently. Pins and link previews also send an "update" for old messages.
EDIT_RECENT_SECONDS = 30
CLEANUP_EVERY_HOURS = 24  # how often entries older than each server's retention are deleted

Action = discord.AuditLogAction
CHANNEL_AUDIT_ACTIONS = (
    Action.channel_update.name,
    Action.overwrite_create.name,
    Action.overwrite_update.name,
    Action.overwrite_delete.name,
)
EMOJI_AUDIT_ACTIONS = (Action.emoji_create.name, Action.emoji_update.name, Action.emoji_delete.name)
STICKER_AUDIT_ACTIONS = (Action.sticker_create.name, Action.sticker_update.name, Action.sticker_delete.name)


def _target_id(entry: discord.AuditLogEntry) -> int | None:
    """The ID of what an audit entry is about, or ``None`` if it has none we can use."""
    try:
        target = entry.target
    except Exception:
        # discord.py builds some targets (invites) from the entry's changes; never let that stop the log.
        return None
    target_id = getattr(target, "id", None)
    return target_id if isinstance(target_id, int) else None


def record_from_entry(entry: discord.AuditLogEntry) -> AuditRecord:
    """Copy the parts of a discord.py audit entry the mod log uses into plain data.

    Args:
        entry: The entry Discord just sent.

    Returns:
        An ``AuditRecord`` for the buffer in ``correlate.py``.
    """
    extra = entry.extra
    target_id = _target_id(entry)
    channel_id = getattr(getattr(extra, "channel", None), "id", None)
    if entry.action is Action.message_bulk_delete:
        channel_id = target_id  # a bulk delete's target is the channel itself
    count = getattr(extra, "count", None)
    try:
        keys = frozenset(name for name, _ in entry.before) | frozenset(name for name, _ in entry.after)
    except Exception:
        keys = frozenset()
    return AuditRecord(
        guild_id=entry.guild.id,
        action=entry.action.name,
        actor_id=entry.user_id,
        target_id=target_id,
        channel_id=channel_id if isinstance(channel_id, int) else None,
        count=count if isinstance(count, int) and count > 0 else 1,
        keys=keys,
        reason=entry.reason,
    )


def _user_facts(event: str, user: Any, channel_ids: tuple[int, ...] = ()) -> Facts:
    """The ignore-rule facts for an event about one member (or a user who already left)."""
    roles = getattr(user, "roles", None)
    return Facts(
        event=event,
        user_id=user.id,
        user_is_bot=bool(user.bot),
        user_role_ids=frozenset(role.id for role in roles) if isinstance(roles, list) else frozenset(),
        channel_ids=channel_ids,
    )


def _user_event(guild: discord.Guild, event: str, user: Any, **fields: Any) -> LogEvent:
    """A ``LogEvent`` about one member, with their name and avatar filled in."""
    log_event = LogEvent(guild.id, event, target_id=user.id, target_name=str(user), **fields)
    avatar = getattr(getattr(user, "display_avatar", None), "url", None)
    if isinstance(avatar, str):
        log_event.extra["thumbnail_url"] = avatar
    return log_event


def _role_mentions(roles: Any, limit: int = STORED_VALUE_MAX) -> str:
    """Roles as mentions (``<@&1>, <@&2>``), without @everyone.

    Mentions instead of plain names so Discord shows each role in its own colour.
    A mention inside an embed never pings anyone. The list stops before ``limit``
    and ends with "+N more", so it is never cut halfway through a mention.
    """
    mentions = [role.mention for role in roles if role.name != "@everyone"]
    if not mentions:
        return "none"
    shown: list[str] = []
    for index, mention in enumerate(mentions):
        rest = len(mentions) - index - 1
        suffix = f", +{rest} more" if rest else ""
        if len(", ".join([*shown, mention])) + len(suffix) > limit:
            return ", ".join(shown) + f", +{len(mentions) - len(shown)} more"
        shown.append(mention)
    return ", ".join(shown)


def _channel_chain(channel: Any, channel_id: int) -> tuple[int, ...]:
    """A channel's ID followed by its parents': a thread's channel, then the category.

    Ignoring a category on the dashboard ignores every channel and thread inside it.
    """
    ids = [channel_id]
    parent = getattr(channel, "parent", None)  # only threads have one
    parent_id = getattr(parent, "id", None)
    if isinstance(parent_id, int):
        ids.append(parent_id)
        channel = parent
    category_id = getattr(channel, "category_id", None)
    if isinstance(category_id, int):
        ids.append(category_id)
    return tuple(ids)


def _channel_name(channel: Any) -> str | None:
    """A channel's name, if the channel is known."""
    name = getattr(channel, "name", None)
    return name if isinstance(name, str) else None


def _message_parts(event: LogEvent, message: discord.Message) -> None:
    """Add a message's attachments, stickers and embed count to an event."""
    if message.attachments:
        event.extra["attachments"] = [
            {"filename": a.filename, "url": a.url} for a in message.attachments[:STORED_ATTACHMENTS]
        ]
    if message.stickers:
        event.add_info("Stickers", ", ".join(s.name for s in message.stickers))
    if message.embeds:
        event.add_info("Embeds", len(message.embeds))


class ModLog(commands.Cog):
    """Listeners for everything the mod log records. Slash commands are added in a later step."""

    def __init__(self, bot: JoltBot) -> None:
        """Keep the bot; the manager is created in ``cog_load``."""
        self.bot = bot

    async def cog_load(self) -> None:
        """Create the manager (audit buffer + channel queues) and start the daily cleanup."""
        self.bot.modlog = ModLogManager(self.bot)
        self.cleanup.start()

    async def cog_unload(self) -> None:
        """Stop the cleanup and the channel workers when the extension unloads."""
        self.cleanup.cancel()
        await self.bot.modlog.close()

    @tasks.loop(hours=CLEANUP_EVERY_HOURS)
    async def cleanup(self) -> None:
        """At startup and then once a day, delete entries older than each server's retention period.

        A failure is logged and tried again the next day; it must not stop the loop.
        """
        try:
            deleted = await retention.purge_expired(self.bot.db)
            if deleted:
                log.info("Deleted %d old mod log entries in %d server(s)", sum(deleted.values()), len(deleted))
        except Exception:
            log.exception("Mod log cleanup failed")

    @cleanup.before_loop
    async def _before_cleanup(self) -> None:
        """Hold the first run until the bot is connected."""
        await self.bot.wait_until_ready()

    @property
    def manager(self) -> ModLogManager:
        """The shared manager, set on the bot in ``cog_load``."""
        return self.bot.modlog

    async def _log_with_actor(
        self,
        guild: discord.Guild,
        event: LogEvent,
        facts: Facts,
        actions: tuple[str, ...],
        *,
        target_id: int | None = None,
        channel_id: int | None = None,
        key: str | None = None,
    ) -> None:
        """The common path: check the rules, wait for the audit log, name the actor, record.

        Args:
            guild: Server the event happened in.
            event: The event, complete except for its actor.
            facts: What the ignore rules need.
            actions: Audit log action names that could explain the event.
            target_id: Match only audit entries about this member, role or channel.
            channel_id: Match only audit entries naming this channel.
            key: Match only audit entries that changed this attribute.
        """
        manager = self.manager
        started = manager.audit.now()
        ctx = await manager.gate(guild.id, facts)
        if ctx is None:
            return
        await manager.settle()
        record = manager.audit.find(
            guild.id, actions, since=manager.since(started), target_id=target_id, channel_id=channel_id, key=key
        )
        await manager.attribute(guild, event, record)
        await manager.record(guild, ctx, event)

    # ------------------------------------------------------------------ audit log

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        """Remember every audit entry for the gateway listeners; log kicks and webhooks directly.

        Needs the ``moderation`` intent and the View Audit Log permission. Without
        the permission Discord simply never sends this event for that server.

        Args:
            entry: The new audit log entry.
        """
        record = record_from_entry(entry)
        self.manager.audit.add(record)
        if entry.action is Action.kick:
            await self._log_kick(entry.guild, record)
        elif entry.action in (Action.webhook_create, Action.webhook_delete):
            await self._log_webhook(entry, record)

    async def _log_kick(self, guild: discord.Guild, record: AuditRecord) -> None:
        """Record a kick. Only the audit log tells a kick apart from someone leaving."""
        if record.target_id is None:
            return
        manager = self.manager
        ctx = await manager.gate(guild.id, Facts(EVENT_KICK, user_id=record.target_id))
        if ctx is None:
            return
        event = LogEvent(
            guild.id,
            EVENT_KICK,
            target_id=record.target_id,
            target_name=await manager.user_name(guild, record.target_id),
        )
        await manager.attribute(guild, event, record)
        await manager.record(guild, ctx, event)

    async def _log_webhook(self, entry: discord.AuditLogEntry, record: AuditRecord) -> None:
        """Record a webhook being created or deleted."""
        guild = entry.guild
        manager = self.manager
        ctx = await manager.gate(guild.id, Facts(EVENT_WEBHOOK))
        if ctx is None:
            return
        created = entry.action is Action.webhook_create
        details = entry.after if created else entry.before
        channel = getattr(details, "channel", None)
        channel_id = getattr(channel, "id", None)
        event = LogEvent(
            guild.id,
            EVENT_WEBHOOK,
            target_id=record.target_id,
            target_name=getattr(details, "name", None),
            target_kind=TARGET_OTHER,
            channel_id=channel_id if isinstance(channel_id, int) else None,
            title="Webhook created" if created else "Webhook deleted",
        )
        event.extra["target_label"] = "Webhook"
        if name := _channel_name(channel):
            event.extra["channel_name"] = name
        await manager.attribute(guild, event, record)
        await manager.record(guild, ctx, event)

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel: discord.abc.GuildChannel) -> None:
        """Fallback for servers without View Audit Log: note that a channel's webhooks changed.

        This gateway event only names the channel. Where the audit log is
        available it gives the full story (see ``_log_webhook``) and this
        listener stays silent, so a webhook change is never logged twice.

        Args:
            channel: The channel whose webhooks changed.
        """
        guild = channel.guild
        manager = self.manager
        if manager.can_see_audit_log(guild):
            return
        ctx = await manager.gate(guild.id, Facts(EVENT_WEBHOOK))
        if ctx is None:
            return
        event = LogEvent(guild.id, EVENT_WEBHOOK, target_kind=TARGET_OTHER, channel_id=channel.id)
        event.extra["channel_name"] = channel.name
        await manager.attribute(guild, event, None)
        await manager.record(guild, ctx, event)

    # ------------------------------------------------------------------ members

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Record a join, with the account's age. Accounts under a week old are flagged.

        Args:
            member: The member who joined.
        """
        guild = member.guild
        ctx = await self.manager.gate(guild.id, _user_facts(EVENT_MEMBER_JOIN, member))
        if ctx is None:
            return
        event = _user_event(guild, EVENT_MEMBER_JOIN, member)
        age = utcnow() - member.created_at
        event.add_time("Account created", member.created_at)
        if age < timedelta(days=NEW_ACCOUNT_DAYS):
            event.title = "Member joined (new account)"
            event.extra["new_account"] = True
            event.add_info("⚠️ New account", f"Created {diffs.human_duration(age.total_seconds())} ago")
        await self.manager.record(guild, ctx, event)

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent) -> None:
        """Record a member leaving, unless a kick or ban entry already covers it.

        The "raw" event is used because it also fires for a member who isn't in
        the bot's cache.

        Args:
            payload: Which user left which server.
        """
        guild = self.bot.get_guild(payload.guild_id)
        user = payload.user
        if guild is None or (self.bot.user is not None and user.id == self.bot.user.id):
            return
        manager = self.manager
        started = manager.audit.now()
        ctx = await manager.gate(guild.id, _user_facts(EVENT_MEMBER_LEAVE, user))
        if ctx is None:
            return
        await manager.settle()
        since = manager.since(started)
        # A kick or a ban also removes the member. If that is being logged, it is the one entry for this.
        kicked = manager.audit.find(guild.id, (Action.kick.name,), since=since, target_id=user.id) is not None
        banned = (
            manager.audit.was_seen(SEEN_BAN, guild.id, user_id=user.id, since=since)
            or manager.audit.find(guild.id, (Action.ban.name,), since=since, target_id=user.id) is not None
        )
        if (kicked and manager.wants(ctx, Facts(EVENT_KICK, user_id=user.id))) or (
            banned and manager.wants(ctx, Facts(EVENT_BAN, user_id=user.id))
        ):
            return
        event = _user_event(guild, EVENT_MEMBER_LEAVE, user)
        if isinstance(getattr(user, "roles", None), list):
            # The cast: only a Member has a role list; a plain User (not in the bot's cache) has none.
            member = cast(discord.Member, user)
            event.add_time("Joined", member.joined_at)
            event.add_info("Roles", _role_mentions(member.roles))
        await manager.record(guild, ctx, event)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """Record nickname, role, timeout and server-avatar changes of a member.

        One update can carry several of these; each becomes its own entry.

        Args:
            before: The member as they were.
            after: The member as they are now.
        """
        guild = after.guild
        manager = self.manager
        started = manager.audit.now()
        # (event, audit actions that could explain it, the attribute that entry must have changed)
        pending: list[tuple[LogEvent, tuple[str, ...], str | None]] = []

        if before.nick != after.nick:
            event = _user_event(guild, EVENT_NICKNAME, after)
            event.add_change("Nickname", before.nick or "none", after.nick or "none")
            pending.append((event, (Action.member_update.name,), "nick"))

        added = [role for role in after.roles if role not in before.roles]
        removed = [role for role in before.roles if role not in after.roles]
        if added or removed:
            event = _user_event(guild, EVENT_MEMBER_ROLES, after)
            if added:
                event.add_change("Roles added", "", _role_mentions(added))
            if removed:
                event.add_change("Roles removed", "", _role_mentions(removed))
            pending.append((event, (Action.member_role_update.name,), None))

        if before.timed_out_until != after.timed_out_until:
            now = utcnow()
            until, was_until = after.timed_out_until, before.timed_out_until
            if until is not None and until > now:
                event = _user_event(guild, EVENT_TIMEOUT_ADD, after)
                event.add_info("Duration", diffs.human_duration((until - now).total_seconds()))
                event.add_time("Until", until)
                pending.append((event, (Action.member_update.name,), "timed_out_until"))
            elif was_until is not None and was_until > now:
                # Removed early by a moderator. A timeout that simply runs out sends no event at all.
                event = _user_event(guild, EVENT_TIMEOUT_REMOVE, after)
                pending.append((event, (Action.member_update.name,), "timed_out_until"))

        if before.guild_avatar != after.guild_avatar:
            pending.append((_user_event(guild, EVENT_AVATAR, after, title="Server avatar changed"), (), None))

        if not pending:
            return
        ctx = await manager.load(guild.id)
        wanted = [item for item in pending if manager.wants(ctx, _user_facts(item[0].event, after))]
        if not wanted:
            return
        await manager.settle()
        for event, actions, key in wanted:
            if actions:
                record = manager.audit.find(
                    guild.id, actions, since=manager.since(started), target_id=after.id, key=key
                )
                await manager.attribute(guild, event, record)
            await manager.record(guild, ctx, event)

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User) -> None:
        """Record a profile avatar change in every server shared with that user (off by default).

        Args:
            before: The user as they were.
            after: The user as they are now.
        """
        if before.avatar == after.avatar:
            return
        for guild in self.bot.guilds:
            member = guild.get_member(after.id)
            # With a server avatar set, the profile avatar isn't what this server sees.
            if member is None or member.guild_avatar is not None:
                continue
            ctx = await self.manager.gate(guild.id, _user_facts(EVENT_AVATAR, member))
            if ctx is not None:
                await self.manager.record(guild, ctx, _user_event(guild, EVENT_AVATAR, member))

    # ------------------------------------------------------------------ moderation

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User | discord.Member) -> None:
        """Record a ban, with who did it and the reason from the audit log.

        Args:
            guild: Server the ban happened in.
            user: Who was banned (a plain user if they weren't a member).
        """
        self.manager.audit.observe(SEEN_BAN, guild.id, user_id=user.id)
        await self._log_with_actor(
            guild,
            _user_event(guild, EVENT_BAN, user),
            _user_facts(EVENT_BAN, user),
            (Action.ban.name,),
            target_id=user.id,
        )

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        """Record an unban, with who did it and the reason from the audit log.

        Args:
            guild: Server the unban happened in.
            user: Who was unbanned.
        """
        await self._log_with_actor(
            guild,
            _user_event(guild, EVENT_UNBAN, user),
            _user_facts(EVENT_UNBAN, user),
            (Action.unban.name,),
            target_id=user.id,
        )

    # ------------------------------------------------------------------ messages

    def _channel(self, guild: discord.Guild, channel_id: int) -> Any:
        """The channel or thread with this ID, or ``None`` if it isn't in memory."""
        return guild.get_channel_or_thread(channel_id)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        """Record an edited message: before, after and a link to it.

        The "raw" event also fires for a message that isn't in memory; then
        "before" is shown as not available. Updates that don't change the text
        (a link preview loading, a pin) are skipped.

        Args:
            payload: The updated message, plus the cached old version if there is one.
        """
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        after, before = payload.message, payload.cached_message
        if guild is None:
            return
        if before is not None:
            if before.content == after.content:
                return
        elif after.edited_at is None or (utcnow() - after.edited_at).total_seconds() > EDIT_RECENT_SECONDS:
            return
        author = after.author
        channel = self._channel(guild, payload.channel_id)
        facts = _user_facts(EVENT_MESSAGE_EDIT, author, _channel_chain(channel, payload.channel_id))
        ctx = await self.manager.gate(guild.id, facts)
        if ctx is None:
            return
        event = _user_event(guild, EVENT_MESSAGE_EDIT, author, channel_id=payload.channel_id)
        event.extra.update(
            target_label="Author",
            message_id=str(payload.message_id),
            jump_url=after.jump_url,
            before=cut(before.content, STORED_CONTENT_MAX) if before is not None else None,
            after=cut(after.content, STORED_CONTENT_MAX),
        )
        if name := _channel_name(channel):
            event.extra["channel_name"] = name
        await self.manager.record(guild, ctx, event)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        """Record a deleted message, and who deleted it when the audit log leaves no doubt.

        A member deleting their own message creates no audit entry, so without a
        matching entry this says "author or unknown" and never guesses.

        Args:
            payload: The deleted message's IDs, plus the message itself if it was in memory.
        """
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        manager = self.manager
        started = manager.audit.now()
        message = payload.cached_message
        author = message.author if message is not None else None
        author_id = author.id if author is not None else None
        # Counted whether or not this delete ends up logged: the audit log counts it too.
        manager.audit.observe(SEEN_MESSAGE_DELETE, guild.id, channel_id=payload.channel_id, user_id=author_id)

        channel = self._channel(guild, payload.channel_id)
        chain = _channel_chain(channel, payload.channel_id)
        facts = (
            _user_facts(EVENT_MESSAGE_DELETE, author, chain)
            if author is not None
            else Facts(EVENT_MESSAGE_DELETE, channel_ids=chain)
        )
        ctx = await manager.gate(guild.id, facts)
        if ctx is None:
            return
        await manager.settle()
        record = manager.audit.who_deleted(guild.id, payload.channel_id, author_id, since=manager.since(started))

        if author is not None:
            event = _user_event(guild, EVENT_MESSAGE_DELETE, author, channel_id=payload.channel_id)
        else:
            event = LogEvent(guild.id, EVENT_MESSAGE_DELETE, channel_id=payload.channel_id)
            if record is not None and record.target_id is not None:
                # The message wasn't in memory, but the audit entry names its author.
                if not manager.wants(ctx, Facts(EVENT_MESSAGE_DELETE, user_id=record.target_id, channel_ids=chain)):
                    return
                event.target_id = record.target_id
                event.target_name = await manager.user_name(guild, record.target_id)
        event.extra.update(
            target_label="Author",
            actor_label="Deleted by",
            message_id=str(payload.message_id),
            cached=message is not None,
        )
        if name := _channel_name(channel):
            event.extra["channel_name"] = name
        if message is not None:
            event.extra["content"] = cut(message.content, STORED_CONTENT_MAX)
            _message_parts(event, message)
        await manager.attribute(guild, event, record, unknown=DELETED_BY_UNKNOWN)
        await manager.record(guild, ctx, event)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        """Record a purge: how many messages, who did it, and the oldest ones still in memory.

        Args:
            payload: The deleted message IDs, plus those messages that were in memory.
        """
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        channel = self._channel(guild, payload.channel_id)
        cached = sorted(payload.cached_messages, key=lambda m: m.id)
        event = LogEvent(guild.id, EVENT_BULK_DELETE, channel_id=payload.channel_id)
        event.extra.update(
            actor_label="Deleted by",
            count=len(payload.message_ids),
            messages=[
                {
                    "author_id": str(m.author.id),
                    "author": str(m.author),
                    "content": cut(m.content, STORED_BULK_CONTENT_MAX),
                    "attachments": len(m.attachments),
                }
                for m in cached[:STORED_BULK_MESSAGES]
            ],
        )
        if name := _channel_name(channel):
            event.extra["channel_name"] = name
        await self._log_with_actor(
            guild,
            event,
            Facts(EVENT_BULK_DELETE, channel_ids=_channel_chain(channel, payload.channel_id)),
            (Action.message_bulk_delete.name,),
            channel_id=payload.channel_id,
        )

    # ------------------------------------------------------------------ server: roles

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        """Record a new role.

        Args:
            role: The role that was created.
        """
        event = LogEvent(role.guild.id, EVENT_ROLE_CREATE, target_id=role.id, target_name=role.name)
        event.target_kind = TARGET_ROLE
        await self._log_with_actor(
            role.guild, event, Facts(EVENT_ROLE_CREATE), (Action.role_create.name,), target_id=role.id
        )

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        """Record a deleted role.

        Args:
            role: The role that was deleted.
        """
        event = LogEvent(role.guild.id, EVENT_ROLE_DELETE, target_id=role.id, target_name=role.name)
        event.target_kind = TARGET_ROLE
        await self._log_with_actor(
            role.guild, event, Facts(EVENT_ROLE_DELETE), (Action.role_delete.name,), target_id=role.id
        )

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        """Record a role's name, colour, display or permission changes.

        Position changes are skipped: moving one role sends an update for every role it passes.

        Args:
            before: The role as it was.
            after: The role as it is now.
        """
        changes = diffs.role_changes(before, after)
        if not changes:
            return
        event = LogEvent(after.guild.id, EVENT_ROLE_UPDATE, target_id=after.id, target_name=after.name)
        event.target_kind = TARGET_ROLE
        for change in changes:
            event.add_change(*change)
        await self._log_with_actor(
            after.guild, event, Facts(EVENT_ROLE_UPDATE), (Action.role_update.name,), target_id=after.id
        )

    # ------------------------------------------------------------------ server: channels

    @staticmethod
    def _channel_event(event_key: str, channel: discord.abc.GuildChannel) -> LogEvent:
        """A ``LogEvent`` about one channel, with its type and category."""
        event = LogEvent(channel.guild.id, event_key, target_id=channel.id, target_name=channel.name)
        event.target_kind = TARGET_CHANNEL
        event.add_info("Type", diffs.show(channel.type))
        if (category := getattr(channel, "category", None)) is not None:
            event.add_info("Category", diffs.show(category))
        return event

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        """Record a new channel.

        Args:
            channel: The channel that was created.
        """
        await self._log_with_actor(
            channel.guild,
            self._channel_event(EVENT_CHANNEL_CREATE, channel),
            Facts(EVENT_CHANNEL_CREATE),
            (Action.channel_create.name,),
            target_id=channel.id,
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        """Record a deleted channel.

        Args:
            channel: The channel that was deleted.
        """
        await self._log_with_actor(
            channel.guild,
            self._channel_event(EVENT_CHANNEL_DELETE, channel),
            Facts(EVENT_CHANNEL_DELETE),
            (Action.channel_delete.name,),
            target_id=channel.id,
        )

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel) -> None:
        """Record a channel's name, topic, slowmode, settings or permission override changes.

        Args:
            before: The channel as it was.
            after: The channel as it is now.
        """
        changes = diffs.channel_changes(before, after)
        if not changes:
            return
        event = LogEvent(after.guild.id, EVENT_CHANNEL_UPDATE, target_id=after.id, target_name=after.name)
        event.target_kind = TARGET_CHANNEL
        for change in changes:
            event.add_change(*change)
        await self._log_with_actor(
            after.guild, event, Facts(EVENT_CHANNEL_UPDATE), CHANNEL_AUDIT_ACTIONS, target_id=after.id
        )

    # ------------------------------------------------------------------ server: settings, emojis, stickers

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        """Record changes to the server's own settings (name, icon, verification level ...).

        Args:
            before: The server as it was.
            after: The server as it is now.
        """
        changes = diffs.guild_changes(before, after)
        if not changes:
            return
        event = LogEvent(after.id, EVENT_SERVER_UPDATE, target_kind=TARGET_OTHER)
        for change in changes:
            event.add_change(*change)
        await self._log_with_actor(
            after, event, Facts(EVENT_SERVER_UPDATE), (Action.guild_update.name,), target_id=after.id
        )

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild: discord.Guild, before: Any, after: Any) -> None:
        """Record emojis being added, removed or renamed.

        Args:
            guild: Server whose emojis changed.
            before: Its emojis as they were.
            after: Its emojis as they are now.
        """
        await self._log_named_changes(guild, EVENT_EMOJI, EMOJI_AUDIT_ACTIONS, before, after)

    @commands.Cog.listener()
    async def on_guild_stickers_update(self, guild: discord.Guild, before: Any, after: Any) -> None:
        """Record stickers being added, removed or renamed.

        Args:
            guild: Server whose stickers changed.
            before: Its stickers as they were.
            after: Its stickers as they are now.
        """
        await self._log_named_changes(guild, EVENT_STICKER, STICKER_AUDIT_ACTIONS, before, after)

    async def _log_named_changes(
        self, guild: discord.Guild, event_key: str, actions: tuple[str, ...], before: Any, after: Any
    ) -> None:
        """Shared body of the emoji and sticker listeners."""
        changes = diffs.named_changes(before, after)
        if not changes:
            return
        event = LogEvent(guild.id, event_key, target_kind=TARGET_OTHER)
        for change in changes:
            event.add_change(*change)
        await self._log_with_actor(guild, event, Facts(event_key), actions)

    # ------------------------------------------------------------------ voice

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        """Record voice joins, leaves and moves (off by default: it is a lot of posts).

        A moderator moving or disconnecting someone is named only when the audit
        log leaves no doubt (see ``correlate.py``); otherwise the entry has no
        "by" line, since the member most likely did it themself.

        Args:
            member: Whose voice state changed.
            before: Their voice state as it was.
            after: Their voice state as it is now.
        """
        if before.channel == after.channel:
            return  # mute, deafen, camera, stream: not logged
        guild = member.guild
        manager = self.manager
        started = manager.audit.now()

        if before.channel is None:
            ctx = await manager.gate(guild.id, _user_facts(EVENT_VOICE_JOIN, member))
            if ctx is not None:
                # The cast: they were in no channel before and the two differ, so they are in one now.
                joined = cast("discord.VoiceChannel | discord.StageChannel", after.channel)
                event = _user_event(guild, EVENT_VOICE_JOIN, member, channel_id=joined.id)
                event.extra["channel_name"] = joined.name
                await manager.record(guild, ctx, event)
            return

        if after.channel is None:
            manager.audit.observe(SEEN_VOICE_LEAVE, guild.id, user_id=member.id)
            event = _user_event(guild, EVENT_VOICE_LEAVE, member, channel_id=before.channel.id)
            event.extra.update(channel_name=before.channel.name, actor_label="Disconnected by")
        else:
            manager.audit.observe(SEEN_VOICE_MOVE, guild.id, channel_id=after.channel.id, user_id=member.id)
            event = _user_event(guild, EVENT_VOICE_MOVE, member, channel_id=after.channel.id)
            event.extra.update(channel_name=after.channel.name, actor_label="Moved by")
            event.add_change("Channel", before.channel.name, after.channel.name)

        ctx = await manager.gate(guild.id, _user_facts(event.event, member))
        if ctx is None:
            return
        await manager.settle()
        since = manager.since(started)
        record = (
            manager.audit.who_disconnected(guild.id, since=since)
            if after.channel is None
            else manager.audit.who_moved(guild.id, after.channel.id, since=since)
        )
        if record is not None:
            await manager.attribute(guild, event, record)
        await manager.record(guild, ctx, event)


async def setup(bot: JoltBot) -> None:
    """discord.py extension entry point for the mod log cog.

    Args:
        bot: The running ``JoltBot`` instance.
    """
    await bot.add_cog(ModLog(bot))
