"""Alliance module, bot side: the /alliances commands, the leave listener and startup wiring.

Every handler loads the settings of the guild the event happened in, and does
nothing if the module is switched off or not set up there.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ...core.limits import EMBED_DESCRIPTION_MAX
from ..tickets.checks import hierarchy_problems, roles_by_id
from ..tickets.embeds import ts
from . import store
from .checks import can_manage_requests
from .context import AllianceContext, load_context
from .embeds import panel_embed
from .service import AllianceManager
from .views import AlliancePanelView, RequestDecisionView

if TYPE_CHECKING:
    from ...bot.client import JoltBot

log = logging.getLogger(__name__)

# What JoltBot needs in the panel channel to post the menu.
PANEL_CHANNEL_PERMISSIONS = ("view_channel", "send_messages", "embed_links")
PENDING_LIST_MAX = 25  # requests shown by /alliances pending; the rest are counted


async def _manage_check(interaction: discord.Interaction) -> bool:
    """Slash-command check: Manage Server, or one of this server's override roles.

    Checked in code on every use, because who can *see* the commands is a
    Discord setting that each server's admins can change.

    Args:
        interaction: The slash-command interaction.

    Returns:
        True when the member may run the command.

    Raises:
        app_commands.CheckFailure: Otherwise. Its text is shown to the member (see ``Tree.on_error``).
    """
    bot: JoltBot = interaction.client  # type: ignore[assignment]
    if interaction.guild_id is None:
        raise app_commands.CheckFailure("This command only works inside a server.")
    ctx = await load_context(bot, interaction.guild_id)
    if not can_manage_requests(interaction.user, ctx.settings):
        raise app_commands.CheckFailure("You need Manage Server (or an alliance override role) to use this.")
    return True


@app_commands.guild_only()
# Hides the commands from members without Manage Server. This is only Discord's default: a
# server's admins can change it under Integrations, which is why each command also has a check.
@app_commands.default_permissions(manage_guild=True)
class Alliances(commands.GroupCog, group_name="alliances", group_description="Alliance join request tools"):
    """Slash group ``/alliances`` plus the leave listener.

    Permissions:
        Discord hides the group from members without Manage Server by default.
        The runtime check (``_manage_check``) also allows the server's override roles.

    Cooldowns:
        None on the commands. (The wait after a rejection is a per-server setting.)
    """

    def __init__(self, bot: JoltBot) -> None:
        """Keep the bot; everything else lives in the database."""
        self.bot = bot

    async def cog_load(self) -> None:
        """Create the request manager, reopen requests cut short by a restart, register the persistent views.

        discord.py calls this when the extension loads, before the bot connects.
        """
        self.bot.alliances = AllianceManager(self.bot)
        try:
            if reopened := await store.release_stuck(self.bot.db):
                log.info("Reopened %d alliance request(s) whose accept was cut short by a restart", reopened)
        except Exception:
            # Not worth keeping the module from loading: such a request can still be cancelled by an admin.
            log.exception("Couldn't check for alliance requests cut short by a restart")
        # Persistent views: re-attach the handlers to every old message after a restart.
        # Discord only sends a component's custom_id, so one registered view serves every server.
        self.bot.add_view(AlliancePanelView(self.bot))
        self.bot.add_view(RequestDecisionView(self.bot))

    # ------------------------------------------------------------------ events

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent) -> None:
        """Close the waiting request of a member who leaves, is kicked or is banned.

        The "raw" event is used because it also fires for a member who isn't in the
        bot's cache.

        Args:
            payload: Which user left which server.
        """
        await self.bot.alliances.handle_member_left(payload.guild_id, payload.user.id)

    # ------------------------------------------------------------------ commands

    @app_commands.command(name="panel", description="Post (or re-post) the alliance menu in the panel channel.")
    @app_commands.check(_manage_check)
    async def panel(self, interaction: discord.Interaction) -> None:
        """Post or replace the "choose your alliance" menu in the configured panel channel.

        Run it again after adding, renaming or removing an alliance on the dashboard.

        Who can use it:
            Manage Server, or an override role (see ``_manage_check``).

        Example:
            ``/alliances panel``

        Args:
            interaction: Slash interaction in a guild.
        """
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        ctx = await load_context(self.bot, guild.id)
        channel, problem = self._panel_channel(guild, ctx)
        if channel is None:
            await interaction.followup.send(problem, ephemeral=True)
            return

        await self._delete_old_panel(guild)
        message = await channel.send(
            embed=panel_embed(ctx.settings, guild, ctx.colour),
            view=AlliancePanelView(self.bot, ctx.settings.alliances),
        )
        await store.set_panel(self.bot.db, guild.id, channel.id, message.id)

        lines = [f"✅ Posted the alliance menu in {channel.mention}."]
        try:
            await message.pin(reason="Alliance menu")
            lines.append("📌 Pinned it.")
        except discord.Forbidden:
            lines.append("⚠️ Couldn't pin it (I need **Pin Messages** there). Pin it by hand if you like.")
        except discord.HTTPException:
            lines.append("⚠️ Couldn't pin it (pin limit reached?).")
        roles = roles_by_id(guild, sorted(ctx.settings.member_role_ids))
        if problems := hierarchy_problems(guild, roles):
            lines.append("\n**Fix before accepting anyone:**")
            lines += [f"• {p}" for p in problems]
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    def _panel_channel(
        self, guild: discord.Guild, ctx: AllianceContext
    ) -> tuple[discord.TextChannel | None, str | None]:
        """Find where the menu goes, or say why it can't be posted.

        Returns:
            ``(channel, None)`` when the menu can be posted, or
            ``(None, message)`` with the reason for whoever ran the command.
        """
        if not ctx.enabled:
            return None, "❌ Alliance requests are switched off. Turn them on in the dashboard (Alliances)."
        if problems := ctx.settings.setup_problems():
            return None, "❌ Finish setup on the dashboard first:\n• " + "\n• ".join(problems)
        channel = guild.get_channel(ctx.settings.panel_channel_id or 0)
        if not isinstance(channel, discord.TextChannel):
            return None, "❌ The panel channel no longer exists. Pick another on the dashboard."
        perms = channel.permissions_for(guild.me)
        if missing := [n for n in PANEL_CHANNEL_PERMISSIONS if not getattr(perms, n)]:
            names = ", ".join(m.replace("_", " ").title() for m in missing)
            return None, f"❌ I'm missing {names} in {channel.mention}."
        return channel, None

    async def _delete_old_panel(self, guild: discord.Guild) -> None:
        """Delete the menu posted earlier, so there is only ever one."""
        old = await store.get_panel(self.bot.db, guild.id)
        if old is None:
            return
        old_channel = guild.get_channel(old.channel_id)
        if isinstance(old_channel, discord.TextChannel):
            try:
                await old_channel.get_partial_message(old.message_id).delete()
            except discord.HTTPException as e:
                # Usually it was already deleted by hand. Either way the new menu is still posted.
                log.debug("Guild %s: couldn't delete the old alliance menu: %s", guild.id, e)

    @app_commands.command(name="pending", description="List the alliance join requests still waiting for a decision.")
    @app_commands.check(_manage_check)
    async def pending(self, interaction: discord.Interaction) -> None:
        """Reply privately with every waiting request, oldest first, each with a link to its message.

        Who can use it:
            Manage Server, or an override role.

        Example:
            ``/alliances pending``

        Args:
            interaction: Slash interaction in a guild.
        """
        guild = interaction.guild
        assert guild is not None
        ctx = await load_context(self.bot, guild.id)
        requests = await store.list_active(self.bot.db, guild.id)
        if not requests:
            await interaction.response.send_message("No pending alliance requests.", ephemeral=True)
            return
        lines = []
        for request in requests[:PENDING_LIST_MAX]:
            line = (
                f"**#{request.id}** <@{request.user_id}> → **{request.alliance_name}** • "
                f"{request.ingame_name} (`{request.governor_id}`) • {ts(request.created_at, 'R')}"
            )
            if request.channel_id and request.message_id:
                url = f"https://discord.com/channels/{guild.id}/{request.channel_id}/{request.message_id}"
                line += f" • [open]({url})"
            lines.append(line)
        if len(requests) > PENDING_LIST_MAX:
            lines.append(f"…and {len(requests) - PENDING_LIST_MAX} more.")
        embed = discord.Embed(
            title=f"Pending alliance requests ({len(requests)})",
            description="\n".join(lines)[:EMBED_DESCRIPTION_MAX],
            colour=ctx.colour,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="cancel", description="Cancel a member's pending alliance join request.")
    @app_commands.describe(member="Whose pending request to cancel")
    @app_commands.check(_manage_check)
    async def cancel(self, interaction: discord.Interaction, member: discord.User) -> None:
        """Cancel a member's waiting request, so they can send a new one. No cooldown follows.

        The request message is greyed out, the member is told, and the
        cancellation is posted in the log channel.

        Who can use it:
            Manage Server, or an override role.

        Example:
            ``/alliances cancel member:@Alice``

        Args:
            interaction: Slash interaction in a guild.
            member: Whose request to cancel. May be someone who already left the server.
        """
        guild = interaction.guild
        assert guild is not None and isinstance(interaction.user, discord.Member)
        # Discord wants an answer within 3 seconds; editing the message, the DM and the log can take longer.
        await interaction.response.defer(ephemeral=True, thinking=True)
        request = await store.get_active_by_user(self.bot.db, guild.id, member.id)
        if request is None:
            await interaction.followup.send(f"{member.mention} has no pending request.", ephemeral=True)
            return
        text = await self.bot.alliances.cancel(guild, interaction.user, request)
        await interaction.followup.send(text, ephemeral=True)


async def setup(bot: JoltBot) -> None:
    """discord.py extension entry point for the alliances cog.

    Args:
        bot: The running ``JoltBot`` instance.
    """
    await bot.add_cog(Alliances(bot))
