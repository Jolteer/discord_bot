from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from .config import Config
from .database import Database
from .tickets import TicketManager
from .views.ticket_controls import TicketControlView
from .views.verify_panel import VerifyPanelView

log = logging.getLogger(__name__)

EXTENSIONS = ("bot.cogs.onboarding", "bot.cogs.lifecycle", "bot.cogs.admin")


class Tree(app_commands.CommandTree):
    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            message = str(error) or "You can't use this command."
        elif isinstance(error, app_commands.BotMissingPermissions):
            message = f"I'm missing permissions: {', '.join(error.missing_permissions)}."
        else:
            log.exception("Slash command failed", exc_info=error)
            message = "Something went wrong running that command. Check the bot's console log."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass


class TicketBot(commands.Bot):
    def __init__(self, cfg: Config) -> None:
        intents = discord.Intents.default()
        intents.members = True          # PRIVILEGED: join/leave events + member cache
        intents.message_content = True  # PRIVILEGED: needed to read messages for transcripts
        super().__init__(
            command_prefix=commands.when_mentioned,  # no text commands; slash only
            intents=intents,
            help_command=None,
            tree_cls=Tree,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
            member_cache_flags=discord.MemberCacheFlags.all(),
            chunk_guilds_at_startup=True,
        )
        self.cfg = cfg
        self.db = Database(cfg.database_path)
        self.tickets = TicketManager(self)

    async def setup_hook(self) -> None:
        await self.db.connect()
        # Persistent views: re-attach button handlers to every old message after a restart.
        self.add_view(VerifyPanelView(self))
        self.add_view(TicketControlView(self))
        for ext in EXTENSIONS:
            await self.load_extension(ext)

        guild = discord.Object(id=self.cfg.guild_id)
        self.tree.copy_global_to(guild=guild)
        try:
            synced = await self.tree.sync(guild=guild)
            log.info("Registered %d slash commands in guild %s", len(synced), self.cfg.guild_id)
        except discord.Forbidden:
            log.error(
                "Couldn't register slash commands in guild %s. Re-invite the bot with BOTH the 'bot' and "
                "'applications.commands' scopes, and double-check guild_id in config.json.",
                self.cfg.guild_id,
            )

    async def close(self) -> None:
        await self.db.close()
        await super().close()
