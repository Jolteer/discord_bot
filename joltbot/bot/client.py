"""The JoltBot client: intents, module loading, slash-command sync, issue reporting."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..core.config import AppConfig
from ..core.db import Database
from ..core.guild_settings import load_module
from ..core.issues import clear_issue, record_issue
from ..core.limits import MESSAGE_MAX
from ..core.models import CLEAR_GUILD_COMMANDS_KEY, COMMANDS_HASH_KEY, BotMeta
from ..modules.general.settings import MODULE as GENERAL
from ..modules.registry import MODULES
from .interactions import safe_reply

if TYPE_CHECKING:
    from ..modules.alliances.service import AllianceManager
    from ..modules.tickets.service import TicketManager

log = logging.getLogger(__name__)

# Dashboard warning shown when a server's notices channel is gone or JoltBot can't post in it.
NOTICE_CHANNEL_ISSUE = "general.notice_channel"
NOTICE_HEADER = "⚠️ **JoltBot needs attention**\n"
NOTICE_FOOTER = "\n\nThis is also shown on the JoltBot dashboard and clears itself once it's fixed."

# Registering slash commands is retried with a doubling wait, from the first value up to the second.
SYNC_RETRY_FIRST_SECONDS = 30
SYNC_RETRY_MAX_SECONDS = 900


class Tree(app_commands.CommandTree):
    """Command tree that turns check/permission failures into ephemeral replies."""

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        """Reply ephemerally to slash failures; log unexpected errors.

        Args:
            interaction: The failed slash interaction.
            error: Raised by discord.py's app command layer.
        """
        # BotMissingPermissions is itself a CheckFailure, so it has to be tested first.
        if isinstance(error, app_commands.BotMissingPermissions):
            names = ", ".join(p.replace("_", " ").title() for p in error.missing_permissions)
            message = f"I'm missing permissions: {names}."
        elif isinstance(error, app_commands.CheckFailure):
            message = str(error) or "You can't use this command."
        else:
            log.exception("Slash command failed", exc_info=error)
            message = "Something went wrong running that command. Please try again in a moment."
        await safe_reply(interaction, message)


class JoltBot(commands.Bot):
    """Discord bot process: privileged intents, module cogs, slash sync, issues.

    Args:
        config: Process settings (token is passed separately to ``start``).

    Attributes:
        failed_modules: Names of modules whose cog failed to load at startup.
        tickets: Set by the tickets cog on load; ticket lifecycle entry point.
        alliances: Set by the alliances cog on load; alliance join request entry point.
        config: Shared ``AppConfig``.
        db: Shared ``Database`` for this process.
    """

    tickets: TicketManager  # set by the tickets cog when it loads
    alliances: AllianceManager  # set by the alliances cog when it loads

    def __init__(self, config: AppConfig) -> None:
        """Set up the intents, the command tree and the database.

        Nothing connects to Discord until ``start`` is called.
        """
        intents = discord.Intents.default()
        intents.members = True  # PRIVILEGED: join/leave events + member cache
        intents.message_content = True  # PRIVILEGED: needed to read messages for transcripts
        super().__init__(
            command_prefix=commands.when_mentioned,  # required by discord.py; JoltBot has slash commands only
            intents=intents,
            help_command=None,
            tree_cls=Tree,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
            # Keep every member in memory and load each server's member list at startup: the
            # ticket module looks members up by ID, and grant-existing walks guild.members.
            member_cache_flags=discord.MemberCacheFlags.all(),
            chunk_guilds_at_startup=True,
        )
        self.config = config
        self.db = Database(config.database_url)
        self._background: set[asyncio.Task] = set()
        self.failed_modules: list[str] = []  # names of modules whose cog didn't load

    async def setup_hook(self) -> None:
        """Load guild_sync + module cogs; start background slash sync with retry.

        A module whose cog fails to load is logged and skipped, so one broken
        module can't keep the whole bot offline. ``guild_sync`` is core: if it
        fails, startup fails.
        """
        await self.load_extension("joltbot.bot.guild_sync")
        await self.load_extension("joltbot.bot.heartbeat")
        self.failed_modules = []
        for module in MODULES.values():
            if not module.cog:
                continue
            try:
                await self.load_extension(module.cog)
            except commands.ExtensionError:
                log.exception(
                    "Module %r failed to load; it stays off until this is fixed and the bot restarts", module.name
                )
                self.failed_modules.append(module.name)
        if self.failed_modules:
            # The command list no longer includes the broken module's commands, so syncing
            # now would make Discord delete them in every server.
            log.error("Not registering slash commands because of failed module(s): %s", ", ".join(self.failed_modules))
            return
        # Registering slash commands is an HTTP call. The old bot did it right here, so a
        # Discord hiccup at startup crashed the whole bot. Now it runs in the background
        # and simply retries until it works.
        task = asyncio.create_task(self._sync_commands_with_retry())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _sync_commands_with_retry(self) -> None:
        """Register slash commands once the bot is ready, retrying until Discord accepts.

        Runs as a background task (see ``setup_hook``). The wait between tries doubles,
        from ``SYNC_RETRY_FIRST_SECONDS`` up to ``SYNC_RETRY_MAX_SECONDS``.
        """
        await self.wait_until_ready()
        delay = SYNC_RETRY_FIRST_SECONDS
        while not self.is_closed():
            try:
                await self.sync_commands()
                return
            except Exception:
                log.exception("Couldn't register slash commands; retrying in %ss", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, SYNC_RETRY_MAX_SECONDS)

    async def sync_commands(self) -> None:
        """Register global slash commands only when the payload hash changed.

        Discord rate-limits blank syncs, so unchanged trees are skipped via
        ``commands_hash``. Afterwards, clears legacy per-guild commands when
        ``bot_meta.clear_guild_commands`` is set (KOC migration). The global
        registration comes first so a problem with one old server can never
        stop every server from getting its commands.

        Raises:
            discord.HTTPException: Propagated from ``tree.sync`` (caller retries).
        """
        payload = [cmd.to_dict(self.tree) for cmd in self.tree.get_commands()]
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        async with self.db.session() as session:
            stored = await session.get(BotMeta, COMMANDS_HASH_KEY)

        if stored is not None and stored.value == digest:
            log.info("Slash commands unchanged; not re-registering")
        else:
            synced = await self.tree.sync()
            async with self.db.session() as session, session.begin():
                await session.merge(BotMeta(key=COMMANDS_HASH_KEY, value=digest))
            log.info("Registered %d global slash command(s)", len(synced))

        await self._clear_legacy_guild_commands()

    async def _clear_legacy_guild_commands(self) -> None:
        """Remove the per-guild commands the old bot registered, for each listed guild.

        A guild Discord refuses (the bot is no longer in it, or was added without
        the ``applications.commands`` scope) is dropped from the list: retrying
        can't fix that. A guild that hit a temporary error stays on the list.

        Raises:
            discord.HTTPException: A temporary error left guilds to clear (caller retries).
        """
        async with self.db.session() as session:
            row = await session.get(BotMeta, CLEAR_GUILD_COMMANDS_KEY)
        if row is None:
            return

        remaining: list[str] = []
        failure: discord.HTTPException | None = None
        for raw_id in filter(None, (part.strip() for part in row.value.split(","))):
            if not raw_id.isdigit():
                log.warning("Ignoring invalid guild ID %r in %s", raw_id, CLEAR_GUILD_COMMANDS_KEY)
                continue
            guild = discord.Object(id=int(raw_id))
            try:
                self.tree.clear_commands(guild=guild)
                await self.tree.sync(guild=guild)  # an empty list removes the old per-guild commands
                log.info("Removed old per-server slash commands from guild %s", raw_id)
            except (discord.Forbidden, discord.NotFound) as e:
                log.warning(
                    "Can't remove old slash commands from guild %s (not in it any more?); skipping: %s", raw_id, e
                )
            except discord.HTTPException as e:
                remaining.append(raw_id)
                failure = e

        async with self.db.session() as session, session.begin():
            if (current := await session.get(BotMeta, CLEAR_GUILD_COMMANDS_KEY)) is not None:
                if remaining:
                    current.value = ",".join(remaining)
                else:
                    await session.delete(current)
        if failure is not None:
            raise failure

    # ------------------------------------------------------------------ issues

    async def report_issue(self, guild_id: int, key: str, message: str) -> None:
        """Record a problem for the server's admins (dashboard) and log a warning.

        Args:
            guild_id: Affected Discord guild.
            key: Stable issue key (e.g. ``tickets.pending_role``); upserts by key.
            message: Human-readable text shown on the overview page.
        """
        log.warning("Guild %s: %s", guild_id, message, extra={"guild_id": guild_id, "issue": key})
        try:
            async with self.db.session() as session, session.begin():
                is_new = await record_issue(session, guild_id, key, message)
        except Exception:
            log.exception("Couldn't record issue %s for guild %s", key, guild_id)
            return
        if is_new:  # repeats of an open problem are counted on the dashboard, not posted again
            await self._post_notice(guild_id, message)

    async def _post_notice(self, guild_id: int, message: str) -> None:
        """Post a new problem in the server's notices channel, if an admin picked one.

        Never raises: callers are in the middle of ticket work and must not be
        interrupted because a notice couldn't be delivered. If the notices
        channel itself is unusable, that is recorded as a dashboard warning
        (``NOTICE_CHANNEL_ISSUE``) and cleared once posting works again. That
        warning is never posted as a notice, which would loop.

        Args:
            guild_id: Affected Discord guild.
            message: The problem text, as shown on the dashboard.
        """
        try:
            guild = self.get_guild(guild_id)
            if guild is None:
                return
            async with self.db.session() as session:
                general = await load_module(session, guild_id, GENERAL)
            channel_id = general.config.notice_channel_id
            if channel_id is None:
                return

            channel = guild.get_channel(channel_id)
            problem: str | None = None
            if not isinstance(channel, discord.TextChannel):
                problem = "The bot notices channel no longer exists. Pick another on the dashboard (General)."
            else:
                room = MESSAGE_MAX - len(NOTICE_HEADER) - len(NOTICE_FOOTER)
                text = NOTICE_HEADER + (message if len(message) <= room else message[: room - 1] + "…") + NOTICE_FOOTER
                try:
                    await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
                except discord.Forbidden:
                    problem = (
                        f"I can't post in the bot notices channel #{channel.name}. Give JoltBot View Channel and "
                        "Send Messages there, or pick another channel on the dashboard (General)."
                    )

            async with self.db.session() as session, session.begin():
                if problem is None:
                    await clear_issue(session, guild_id, NOTICE_CHANNEL_ISSUE)
                else:
                    await record_issue(session, guild_id, NOTICE_CHANNEL_ISSUE, problem)
        except Exception as e:
            log.warning("Guild %s: couldn't post a notice: %s", guild_id, e, extra={"guild_id": guild_id})

    async def resolve_issue(self, guild_id: int, key: str) -> None:
        """Clear a previously recorded guild issue if it still exists.

        Args:
            guild_id: Discord guild snowflake.
            key: Same key used in ``report_issue``.
        """
        try:
            async with self.db.session() as session, session.begin():
                await clear_issue(session, guild_id, key)
        except Exception:
            log.exception("Couldn't clear issue %s for guild %s", key, guild_id)

    async def close(self) -> None:
        """Stop background work, disconnect from Discord and dispose the database engine."""
        for task in self._background:
            task.cancel()
        await super().close()
        await self.db.dispose()
