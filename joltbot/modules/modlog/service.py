"""The mod log's entry point for the bot: decide, find the actor, save, queue the post.

Every listener in ``cog.py`` follows the same four steps through this class:

1. ``gate`` / ``wants``: load the server's settings and apply the ignore rules.
2. ``settle``: wait a moment, so the audit log entry for the same change has arrived.
3. ``attribute``: copy the actor and reason from that entry, or say honestly that nobody could be named.
4. ``record``: save the entry and queue its embed for the log channel.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import discord

from . import store
from .constants import ACTOR_UNKNOWN, ISSUE_VIEW_AUDIT_LOG, MISSING_AUDIT_SUFFIX
from .context import ModLogContext, load_context
from .correlate import LOOKBACK_SECONDS, AuditRecord, RecentAudit
from .dispatch import Dispatcher
from .embeds import build_embed
from .events import Facts, LogEvent
from .rules import should_log

if TYPE_CHECKING:
    from ...bot.client import JoltBot

log = logging.getLogger(__name__)

# How long a handler waits for the matching audit log entry before deciding who did it.
# The entry usually arrives within a few hundred milliseconds of the gateway event.
CORRELATION_WAIT_SECONDS = 2.0


class ModLogManager:
    """Shared state and steps for recording events. One per bot process (``bot.modlog``)."""

    def __init__(
        self,
        bot: JoltBot,
        *,
        wait: float = CORRELATION_WAIT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create the audit buffer and the channel queues.

        Args:
            bot: Running bot.
            wait: Seconds ``settle`` waits for the audit log. Tests pass 0.
            clock: Time source for the audit buffer. Tests pass a fake.
        """
        self.bot = bot
        self.wait = wait
        self.audit = RecentAudit(clock=clock)
        self.dispatcher = Dispatcher(bot)
        # Last known answer to "can JoltBot read this server's audit log?", so the
        # dashboard issue is written when the answer changes and not on every event.
        self._audit_access: dict[int, bool] = {}

    async def close(self) -> None:
        """Stop the channel workers. Called when the cog unloads."""
        await self.dispatcher.close()

    # ------------------------------------------------------------------ step 1: decide

    async def load(self, guild_id: int) -> ModLogContext:
        """Load the server's mod log settings, fresh from the database."""
        return await load_context(self.bot, guild_id)

    @staticmethod
    def wants(ctx: ModLogContext, facts: Facts) -> bool:
        """Whether the server records this event (see ``rules.should_log``)."""
        return should_log(ctx.enabled, ctx.settings, facts)

    async def gate(self, guild_id: int, facts: Facts) -> ModLogContext | None:
        """Load the settings and apply the rules in one go.

        Args:
            guild_id: Server the event happened in.
            facts: What is known about the event.

        Returns:
            The settings if the event should be recorded, else ``None``.
        """
        ctx = await self.load(guild_id)
        return ctx if self.wants(ctx, facts) else None

    # ------------------------------------------------------------------ step 2: wait for the audit log

    async def settle(self) -> None:
        """Give the audit log entry for the current change time to arrive."""
        await asyncio.sleep(self.wait)

    def since(self, started: float) -> float:
        """The earliest arrival time of an audit entry that can belong to an event seen at ``started``."""
        return started - LOOKBACK_SECONDS

    # ------------------------------------------------------------------ step 3: who did it

    @staticmethod
    def can_see_audit_log(guild: discord.Guild) -> bool:
        """Whether JoltBot has View Audit Log in the server. Without it Discord sends no audit entries."""
        me = guild.me
        return me is not None and me.guild_permissions.view_audit_log

    async def user_name(self, guild: discord.Guild, user_id: int) -> str | None:
        """A user's name for the record: from memory if possible, else fetched from Discord.

        Args:
            guild: Server the event happened in.
            user_id: The user to name.

        Returns:
            Their name, or ``None`` if Discord couldn't say. The ID is recorded either way.
        """
        user = guild.get_member(user_id) or self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.HTTPException:
                return None
        return str(user)

    async def attribute(
        self, guild: discord.Guild, event: LogEvent, record: AuditRecord | None, *, unknown: str = ACTOR_UNKNOWN
    ) -> None:
        """Fill in who did it, or say that nobody could be named and why.

        Args:
            guild: Server the event happened in.
            event: The event being built; its actor fields are set here.
            record: The matching audit log entry, if one was found.
            unknown: What to say when there is none.
        """
        if record is not None and record.actor_id is not None:
            event.actor_id = record.actor_id
            event.actor_name = await self.user_name(guild, record.actor_id)
            event.reason = record.reason or event.reason
            await self._track_audit_access(guild, True)
            return
        can_see = self.can_see_audit_log(guild)
        await self._track_audit_access(guild, can_see)
        event.actor_note = unknown if can_see else unknown + MISSING_AUDIT_SUFFIX

    async def _track_audit_access(self, guild: discord.Guild, can_see: bool) -> None:
        """Raise or clear the "missing View Audit Log" dashboard warning when the answer changes."""
        if self._audit_access.get(guild.id) is can_see:
            return
        self._audit_access[guild.id] = can_see
        if can_see:
            await self.bot.resolve_issue(guild.id, ISSUE_VIEW_AUDIT_LOG)
        else:
            await self.bot.report_issue(
                guild.id,
                ISSUE_VIEW_AUDIT_LOG,
                "JoltBot can't see who did things: it is missing the View Audit Log permission. Events are still "
                "logged, with the actor shown as unknown. Give JoltBot's role View Audit Log in Server Settings.",
            )

    # ------------------------------------------------------------------ step 4: save and post

    async def record(self, guild: discord.Guild, ctx: ModLogContext, event: LogEvent) -> None:
        """Save the event and queue its embed for the category's log channel.

        The database row is the record; the channel post is best effort. A failed
        save is logged and the post still goes out.

        Args:
            guild: Server the event happened in.
            ctx: The settings ``gate`` returned for it.
            event: The finished event.
        """
        channel_id = ctx.settings.channel_for(event.category)
        if channel_id is None:
            return
        try:
            await store.insert(self.bot.db, event)
        except Exception:
            log.exception("Guild %s: couldn't save a %s log entry", guild.id, event.event)
        self.dispatcher.enqueue(guild.id, channel_id, event.category, build_embed(event))
