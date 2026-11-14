"""Bot side of the example module: one slash command that uses the server's own settings."""

import discord
from discord import app_commands
from discord.ext import commands

from joltbot.core.guild_settings import load_module
from joltbot.core.templating import render


class Hello(commands.Cog):
    """The /hello command."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @app_commands.command(name="hello", description="Get a hello from JoltBot.")
    @app_commands.guild_only()
    async def hello(self, interaction: discord.Interaction) -> None:
        """Reply privately with the server's greeting. Any member can use it, inside a server only."""
        # Read the settings fresh on every use, so dashboard changes apply at once.
        async with self.bot.db.session() as session:
            state = await load_module(session, interaction.guild_id, "hello")
        if not state.enabled:
            await interaction.response.send_message("That feature is switched off on this server.", ephemeral=True)
            return
        text = render(state.config.greeting, user=interaction.user.mention, server=interaction.guild.name)
        await interaction.response.send_message(text, ephemeral=True)


async def setup(bot) -> None:
    """discord.py calls this when the bot loads the module."""
    await bot.add_cog(Hello(bot))
