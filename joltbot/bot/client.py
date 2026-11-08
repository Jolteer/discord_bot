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
from ..core.issues import clear_issue, record_issue
from ..core.models import BotMeta
from ..modules.registry import MODULES

if TYPE_CHECKING:
    from ..modules.tickets.service import TicketManager

log = logging.getLogger(__name__)

# bot_meta key the KOC migration sets: guild IDs whose old per-guild slash commands
# (registered by KOC Gatekeeper) should be removed, so they don't show up twice.
CLEAR_GUILD_COMMANDS_KEY = "clear_guild_commands"
COMMANDS_HASH_KEY = "commands_hash"


class Tree(app_commands.CommandTree):
    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            message = str(error) or "You can't use this command."
        elif isinstance(error, app_commands.BotMissingPermissions):
            message = f"I'm missing permissions: {', '.join(error.missing_permissions)}."
        else:
            log.exception("Slash command failed", exc_info=error)
            message = "Something went wrong running that command. Please try again in a moment."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except (discord.HTTPException, OSError):
            pass


class JoltBot(commands.Bot):
    tickets: TicketManager  # set by the tickets cog when it loads

    def __init__(self, config: AppConfig) -> None:
        intents = discord.Intents.default()
        intents.members = True  # PRIVILEGED: join/leave events + member cache
        intents.message_content = True  # PRIVILEGED: needed to read messages for transcripts
        super().__init__(
            command_prefix=commands.when_mentioned,  # required by discord.py; JoltBot has slash commands only
            intents=intents,
            help_command=None,
            tree_cls=Tree,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
            member_cache_flags=discord.MemberCacheFlags.all(),
            chunk_guilds_at_startup=True,
        )
        self.config = config
        self.db = Database(config.database_url)
        self._background: set[asyncio.Task] = set()

    async def setup_hook(self) -> None:
        await self.load_extension("joltbot.bot.guild_sync")
        for module in MODULES.values():
            if module.cog:
                await self.load_extension(module.cog)
        # Registering slash commands is an HTTP call. The old bot did it right here, so a
        # Discord hiccup at startup crashed the whole bot. Now it runs in the background
        # and simply retries until it works.
        task = asyncio.create_task(self._sync_commands_with_retry())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _sync_commands_with_retry(self) -> None:
        await self.wait_until_ready()
        delay = 30
        while not self.is_closed():
            try:
                await self.sync_commands()
                return
            except Exception:
                log.exception("Couldn't register slash commands; retrying in %ss", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 900)

    async def sync_commands(self) -> None:
        """Register global slash commands, but only when they changed (Discord rate-limits this)."""
        payload = [cmd.to_dict(self.tree) for cmd in self.tree.get_commands()]
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        async with self.db.session() as session:
            stored = await session.get(BotMeta, COMMANDS_HASH_KEY)
            clear = await session.get(BotMeta, CLEAR_GUILD_COMMANDS_KEY)

        if clear is not None:
            for raw_id in filter(None, clear.value.split(",")):
                guild = discord.Object(id=int(raw_id))
                self.tree.clear_commands(guild=guild)
                await self.tree.sync(guild=guild)  # an empty list removes the old per-guild commands
                log.info("Removed old per-server slash commands from guild %s", raw_id)
            async with self.db.session() as session, session.begin():
                if (row := await session.get(BotMeta, CLEAR_GUILD_COMMANDS_KEY)) is not None:
                    await session.delete(row)

        if stored is not None and stored.value == digest:
            log.info("Slash commands unchanged; not re-registering")
            return
        synced = await self.tree.sync()
        async with self.db.session() as session, session.begin():
            await session.merge(BotMeta(key=COMMANDS_HASH_KEY, value=digest))
        log.info("Registered %d global slash command(s)", len(synced))

    # ------------------------------------------------------------------ issues

    async def report_issue(self, guild_id: int, key: str, message: str) -> None:
        """Record a problem for the server's admins (shown on the dashboard) and log it."""
        log.warning("Guild %s: %s", guild_id, message)
        try:
            async with self.db.session() as session, session.begin():
                await record_issue(session, guild_id, key, message)
        except Exception:
            log.exception("Couldn't record issue %s for guild %s", key, guild_id)

    async def resolve_issue(self, guild_id: int, key: str) -> None:
        try:
            async with self.db.session() as session, session.begin():
                await clear_issue(session, guild_id, key)
        except Exception:
            log.exception("Couldn't clear issue %s for guild %s", key, guild_id)

    async def close(self) -> None:
        await super().close()
        await self.db.dispose()
