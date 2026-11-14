"""Ticket module, bot side: events, /tickets commands and the recovery loop.

Every handler loads the settings of the guild the event happened in, and does
nothing if the module is switched off or not set up there.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import select

from ...core.models import PanelMessage
from . import store
from .checks import can_run_setup_commands, hierarchy_problems, roles_by_id
from .constants import ISSUE_PENDING_ROLE, ISSUE_STARTUP_SWEEP, MODULE
from .context import TicketContext, load_context
from .diagnostics import ERROR, WARN, run_diagnostics
from .embeds import panel_embed
from .service import TicketManager
from .views import PanelView, TicketControlView

if TYPE_CHECKING:
    from ...bot.client import JoltBot

log = logging.getLogger(__name__)

RECOVERY_MINUTES = 5  # how often half-finished ticket work is picked up again
GRANT_CONFIRM_TIMEOUT_SECONDS = 120  # how long the grant-existing confirm button stays usable
GRANT_PROGRESS_EVERY = 25  # members handled between updates of the progress message
# The startup sweep gives the pending role only when few members lack both roles: at most
# this many, or one in SWEEP_SAFE_SHARE of the server, whichever is more.
SWEEP_SAFE_COUNT = 25
SWEEP_SAFE_SHARE = 5
# What JoltBot needs in the panel channel to post the panel.
PANEL_CHANNEL_PERMISSIONS = ("view_channel", "send_messages", "embed_links")
DIAGNOSTICS_MAX_CHARS = 4000  # kept just under the 4096 an embed description may hold


async def _setup_check(interaction: discord.Interaction) -> bool:
    """Slash-command check: Manage Server, or one of this server's staff roles.

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
    if not can_run_setup_commands(interaction.user, ctx.settings):
        raise app_commands.CheckFailure("You need Manage Server (or a staff role) to use this.")
    return True


async def _manage_server_check(interaction: discord.Interaction) -> bool:
    """Slash-command check for bulk actions: Manage Server only, a staff role is not enough.

    Args:
        interaction: The slash-command interaction.

    Returns:
        True when the member has Manage Server.

    Raises:
        app_commands.CheckFailure: Otherwise. Its text is shown to the member.
    """
    user = interaction.user
    if not isinstance(user, discord.Member) or not user.guild_permissions.manage_guild:
        raise app_commands.CheckFailure(
            "You need Manage Server to use this: it gives the accepted role to many members at once."
        )
    return True


def _grant_prompt(roles: list[discord.Role], pending: discord.Role | None, count: int, left_out: int) -> str:
    """The "are you sure" text shown before ``grant-existing`` changes anyone.

    Args:
        roles: The accept roles that would be given.
        pending: The server's pending role, if it uses one.
        count: How many members would get the roles.
        left_out: How many were skipped because they have an open application.

    Returns:
        The message text, one fact per line.
    """
    names = ", ".join(r.mention for r in roles)
    who = "who have neither it nor the pending role" if pending is not None else "who don't have it yet"
    lines = [f"This gives {names} to **{count}** member(s) {who} (people who were here before verification)."]
    if pending is None:
        lines.append(
            "⚠️ No pending role is set, so that includes everyone who joined and hasn't applied yet. "
            "After this they count as verified."
        )
    if left_out:
        lines.append(f"**{left_out}** member(s) with an open application are left out; staff decide those.")
    lines.append("Large servers take a few minutes. Continue?")
    return "\n".join(lines)


class GrantConfirmView(discord.ui.View):
    """The confirm prompt of ``/tickets grant-existing``.

    Shown privately to whoever ran the command, so only they can click it (the
    command itself needs Manage Server). Not persistent: it is gone after two
    minutes or a restart, and the command is simply run again.

    Args:
        cog: The tickets cog, which does the work.
        targets: The members who will get the roles.
        roles: The accept roles to give.
    """

    def __init__(self, cog: Tickets, targets: list[discord.Member], roles: list[discord.Role]) -> None:
        """Remember what was shown, so the click acts on exactly that list."""
        super().__init__(timeout=GRANT_CONFIRM_TIMEOUT_SECONDS)
        self.cog, self.targets, self.roles = cog, targets, roles

    @discord.ui.button(label="Give them the role(s)", style=discord.ButtonStyle.success)
    async def go(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Start the bulk grant. Only the member who ran the command can see this button."""
        self.stop()
        await interaction.response.edit_message(content=f"Working… 0/{len(self.targets)}", view=None)
        await self.cog.run_grant(interaction, self.targets, self.roles)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Close the prompt without changing anyone."""
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", view=None)


@app_commands.guild_only()
# Hides the commands from members without Manage Server. This is only Discord's default: a
# server's admins can change it under Integrations, which is why each command also has a check.
@app_commands.default_permissions(manage_guild=True)
class Tickets(commands.GroupCog, group_name="tickets", group_description="Ticket and verification tools"):
    """Slash group ``/tickets`` plus join/leave listeners and recovery.

    Permissions:
        Discord hides the group from members without Manage Server by default.
        Runtime checks also allow configured staff roles (``_setup_check``), except
        for ``grant-existing``, which needs Manage Server (``_manage_server_check``).

    Cooldowns:
        None on slash commands. ``grant-existing`` is serialized per guild via a lock.
    """

    def __init__(self, bot: JoltBot) -> None:
        """Start with no grant running and the one-time startup sweep still to do."""
        self.bot = bot
        self._grant_locks: dict[int, asyncio.Lock] = {}
        self._swept = False

    async def cog_load(self) -> None:
        """Create the ticket manager, register the persistent views and start the recovery loop.

        discord.py calls this when the extension loads, before the bot connects.
        """
        self.bot.tickets = TicketManager(self.bot)
        # Persistent views: re-attach the button handlers to every old message after a restart.
        # Discord only sends a button's custom_id, so one registered view serves every server.
        # Each is registered twice: under JoltBot's IDs, and under the old bot's (legacy=True)
        # for panels and tickets it posted before the migration.
        for legacy in (False, True):
            self.bot.add_view(PanelView(self.bot, legacy=legacy))
            self.bot.add_view(TicketControlView(self.bot, legacy=legacy))
        self.recovery.start()

    async def cog_unload(self) -> None:
        """Stop the recovery loop when the extension unloads."""
        self.recovery.cancel()

    # ------------------------------------------------------------------ background

    @tasks.loop(minutes=RECOVERY_MINUTES)
    async def recovery(self) -> None:
        """Finish anything left half-done (crash, restart, network error). The first run is at startup."""
        for guild in list(self.bot.guilds):
            try:
                await self.bot.tickets.recover(guild)
            except Exception:
                log.exception("Ticket recovery failed for guild %s", guild.id)

    @recovery.before_loop
    async def _before_recovery(self) -> None:
        """Hold the first recovery run until the bot is connected and knows its servers."""
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Once per process: give the pending role to members who joined while the bot was offline.

        Only in servers that switched this on (``assign_pending_on_startup``).
        """
        if self._swept:  # on_ready fires again after reconnects
            return
        self._swept = True
        for guild in list(self.bot.guilds):
            try:
                await self._sweep_pending(guild)
            except Exception:
                log.exception("Startup sweep failed for guild %s", guild.id)

    async def _sweep_pending(self, guild: discord.Guild) -> None:
        """Give the pending role to anyone who joined while the bot was offline (if the server wants that)."""
        ctx = await load_context(self.bot, guild.id)
        if not ctx.ready or not ctx.settings.assign_pending_on_startup:
            return
        pending = guild.get_role(ctx.settings.pending_role_id or 0)
        accept_roles = set(roles_by_id(guild, ctx.settings.accept_role_ids))
        if pending is None or not accept_roles:
            return
        humans = [m for m in guild.members if not m.bot]
        targets = [m for m in humans if pending not in m.roles and not accept_roles.intersection(m.roles)]
        # Safety net: if lots of people have neither role, /tickets grant-existing probably wasn't run.
        if not targets or len(targets) > max(SWEEP_SAFE_COUNT, len(humans) // SWEEP_SAFE_SHARE):
            if targets:
                await self.bot.report_issue(
                    guild.id,
                    ISSUE_STARTUP_SWEEP,
                    f"Skipped giving the pending role at startup: {len(targets)} of {len(humans)} members have "
                    "neither the pending nor the accepted role. Run /tickets grant-existing first.",
                )
            return
        for member in targets:
            try:
                await member.add_roles(pending, reason="Joined while JoltBot was offline")
            except discord.HTTPException as e:
                log.warning("Guild %s: sweep couldn't give pending role to %s: %s", guild.id, member.id, e)
        log.info("Guild %s: startup sweep gave the pending role to %d member(s)", guild.id, len(targets))

    # ------------------------------------------------------------------ events

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Give a new member the server's pending role, if it uses one.

        Does nothing for bots, or where the module is off or not set up. If the role
        can't be given, the server's admins are told through a dashboard issue.

        Args:
            member: The member who just joined.
        """
        if member.bot:
            return
        ctx = await load_context(self.bot, member.guild.id)
        if not ctx.ready or not ctx.settings.pending_role_id:
            return
        role = member.guild.get_role(ctx.settings.pending_role_id)
        if role is None:
            await self.bot.report_issue(
                member.guild.id,
                ISSUE_PENDING_ROLE,
                "The pending role no longer exists, so new members aren't getting it. Pick another on the dashboard.",
            )
            return
        if problems := hierarchy_problems(member.guild, [role]):
            await self.bot.report_issue(
                member.guild.id,
                ISSUE_PENDING_ROLE,
                "New members aren't getting the pending role: " + " ".join(problems),
            )
            return
        try:
            await member.add_roles(role, reason="New member: awaiting verification")
            await self.bot.resolve_issue(member.guild.id, ISSUE_PENDING_ROLE)
        except discord.Forbidden:
            await self.bot.report_issue(
                member.guild.id,
                ISSUE_PENDING_ROLE,
                "Discord blocked giving new members the pending role. Check JoltBot's "
                "role order and Manage Roles permission.",
            )
        except discord.HTTPException as e:
            log.warning("Guild %s: failed to give pending role to %s: %s", member.guild.id, member.id, e)

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent) -> None:
        """Close the applicant's ticket when they leave, are kicked or are banned.

        The "raw" event is used because it also fires for a member who isn't in the
        bot's cache.

        Args:
            payload: Which user left which server.
        """
        await self.bot.tickets.handle_member_left(payload.guild_id, payload.user.id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        """Close a ticket whose channel was deleted by hand.

        Args:
            channel: The deleted channel. Most are not ticket channels and are ignored.
        """
        await self.bot.tickets.handle_channel_deleted(channel.id)

    # ------------------------------------------------------------------ commands

    @app_commands.command(name="panel", description="Post (or re-post) the application panel in the panel channel.")
    @app_commands.check(_setup_check)
    async def panel(self, interaction: discord.Interaction) -> None:
        """Post or replace the application panel in the configured panel channel.

        Who can use it:
            Manage Server, or a configured staff role (see ``_setup_check``).

        Example:
            ``/tickets panel``

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
            view=PanelView(self.bot, label=ctx.settings.messages.panel_button_label),
        )
        await self._remember_panel(guild.id, channel.id, message.id)

        lines = [f"✅ Posted the application panel in {channel.mention}."]
        try:
            await message.pin(reason="Application panel")
            lines.append("📌 Pinned it.")
        except discord.Forbidden:
            lines.append("⚠️ Couldn't pin it (I need **Pin Messages** there). Pin it by hand if you like.")
        except discord.HTTPException:
            lines.append("⚠️ Couldn't pin it (pin limit reached?).")
        roles = roles_by_id(guild, dict.fromkeys(r.role_id for r in ctx.settings.assignable_roles()))
        if problems := hierarchy_problems(guild, roles):
            lines.append("\n**Fix before accepting anyone:**")
            lines += [f"• {p}" for p in problems]
        lines.append("\nRun `/tickets diagnostics` for a full check.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    def _panel_channel(self, guild: discord.Guild, ctx: TicketContext) -> tuple[discord.TextChannel | None, str | None]:
        """Find where the panel goes, or say why it can't be posted.

        Args:
            guild: Server the command was run in.
            ctx: Fresh ticket settings for the guild.

        Returns:
            ``(channel, None)`` when the panel can be posted, or
            ``(None, message)`` with the reason for whoever ran the command.
        """
        if not ctx.enabled:
            return None, "❌ The ticket module is switched off. Turn it on in the dashboard."
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
        """Delete the panel message posted earlier, so there is only ever one.

        Args:
            guild: Server whose previous panel should go.
        """
        async with self.bot.db.session() as session:
            old = (
                await session.execute(
                    select(PanelMessage).where(PanelMessage.guild_id == guild.id, PanelMessage.module == MODULE)
                )
            ).scalar_one_or_none()
        if old is None:
            return
        old_channel = guild.get_channel(old.channel_id)
        if isinstance(old_channel, discord.TextChannel):
            try:
                await old_channel.get_partial_message(old.message_id).delete()
            except discord.HTTPException as e:
                # Usually it was already deleted by hand. Either way the new panel is still posted.
                log.debug("Guild %s: couldn't delete the old panel message: %s", guild.id, e)

    async def _remember_panel(self, guild_id: int, channel_id: int, message_id: int) -> None:
        """Save where the panel was posted, replacing the previous location.

        Args:
            guild_id: Server the panel belongs to.
            channel_id: Channel it was posted in.
            message_id: The panel message.
        """
        async with self.bot.db.session() as session, session.begin():
            row = await session.get(PanelMessage, (guild_id, MODULE))
            if row is None:
                session.add(
                    PanelMessage(guild_id=guild_id, module=MODULE, channel_id=channel_id, message_id=message_id)
                )
            else:
                row.channel_id, row.message_id = channel_id, message_id

    @app_commands.command(name="diagnostics", description="Check JoltBot's permissions, role order and ticket setup.")
    @app_commands.check(_setup_check)
    async def diagnostics(self, interaction: discord.Interaction) -> None:
        """Run a permission and setup self-check; reply ephemerally with findings.

        Who can use it:
            Manage Server, or a configured staff role.

        Example:
            ``/tickets diagnostics``

        Args:
            interaction: Slash interaction in a guild.
        """
        assert interaction.guild is not None
        # Discord wants an answer within 3 seconds; the checks below can take longer on a big server.
        await interaction.response.defer(ephemeral=True, thinking=True)
        ctx = await load_context(self.bot, interaction.guild.id)
        findings = run_diagnostics(interaction.guild, ctx.settings, ctx.enabled)
        errors = sum(f.level == ERROR for f in findings)
        warns = sum(f.level == WARN for f in findings)
        colour = discord.Colour.red() if errors else discord.Colour.orange() if warns else discord.Colour.green()
        embed = discord.Embed(
            title="JoltBot ticket diagnostics",
            description="\n".join(f.render() for f in findings)[:DIAGNOSTICS_MAX_CHARS],
            colour=colour,
        )
        embed.set_footer(text=f"{errors} error(s), {warns} warning(s)")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="grant-existing",
        description="One time: give the accepted-member role(s) to everyone already here.",
    )
    @app_commands.check(_manage_server_check)
    async def grant_existing(self, interaction: discord.Interaction) -> None:
        """Bulk-grant accept roles to members who lack accept and pending roles.

        Members with an open application are left out: staff accept (or deny)
        them in their ticket.

        Who can use it:
            Manage Server only; a staff role is not enough, because this gives
            the accepted role to many members at once. Asks for a confirm
            button before running. Only one grant runs per guild at a time.

        Example:
            ``/tickets grant-existing`` then click **Give them the role(s)**.

        Cooldown / concurrency:
            No Discord cooldown; a per-guild ``asyncio.Lock`` blocks overlapping runs.

        Args:
            interaction: Slash interaction in a guild.
        """
        guild = interaction.guild
        assert guild is not None
        # Discord wants an answer within 3 seconds; walking the member list can take longer.
        await interaction.response.defer(ephemeral=True, thinking=True)
        ctx = await load_context(self.bot, guild.id)
        roles = roles_by_id(guild, ctx.settings.accept_role_ids)
        if not roles:
            await interaction.followup.send("❌ Pick a 'role given on accept' on the dashboard first.", ephemeral=True)
            return
        if problems := hierarchy_problems(guild, roles):
            await interaction.followup.send("❌ " + "\n".join(problems), ephemeral=True)
            return
        if self._grant_locks.setdefault(guild.id, asyncio.Lock()).locked():
            await interaction.followup.send("This is already running.", ephemeral=True)
            return
        pending = guild.get_role(ctx.settings.pending_role_id or 0)
        targets, left_out = await self._grant_targets(guild, roles, pending)
        if not targets:
            await interaction.followup.send("Nobody to update. Everyone already has the role(s).", ephemeral=True)
            return
        await interaction.followup.send(
            _grant_prompt(roles, pending, len(targets), left_out),
            view=GrantConfirmView(self, targets, roles),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _grant_targets(
        self, guild: discord.Guild, roles: list[discord.Role], pending: discord.Role | None
    ) -> tuple[list[discord.Member], int]:
        """Work out who ``grant-existing`` would give the accept role(s) to.

        Args:
            guild: Server the command was run in.
            roles: The server's accept roles.
            pending: Its pending role, if it uses one.

        Returns:
            ``(targets, left_out)``: the members to update, and how many more
            were skipped because they have an open application.
        """
        eligible = [
            m
            for m in guild.members
            if not m.bot and not set(roles).intersection(m.roles) and (pending is None or pending not in m.roles)
        ]
        # Someone with an open application is waiting for staff to decide; this must not decide for them.
        applying = await store.list_active_user_ids(self.bot.db, guild.id)
        targets = [m for m in eligible if m.id not in applying]
        return targets, len(eligible) - len(targets)

    async def run_grant(
        self, interaction: discord.Interaction, targets: list[discord.Member], roles: list[discord.Role]
    ) -> None:
        """Give the accept role(s) to each target, showing progress in the confirm message.

        Holds the server's grant lock, so a second run waits for this one. A member
        that fails is counted and logged; the others still get the roles.

        Args:
            interaction: The confirm-button click. Its message shows progress, then the result.
            targets: The members to update.
            roles: The roles to give each of them.
        """
        assert interaction.guild is not None
        async with self._grant_locks.setdefault(interaction.guild.id, asyncio.Lock()):
            done = failed = 0
            for i, member in enumerate(targets, 1):
                try:
                    await member.add_roles(*roles, reason=f"Existing member, granted by {interaction.user}")
                    done += 1
                except discord.HTTPException as e:
                    failed += 1
                    log.warning("Guild %s: grant-existing failed for %s: %s", interaction.guild.id, member.id, e)
                if i % GRANT_PROGRESS_EVERY == 0:
                    try:
                        await interaction.edit_original_response(content=f"Working… {i}/{len(targets)}")
                    except discord.HTTPException as e:
                        # Interaction tokens expire after 15 minutes; the grant itself keeps going.
                        log.debug("Guild %s: couldn't update grant-existing progress: %s", interaction.guild.id, e)
            summary = f"✅ Done: updated **{done}** member(s)" + (f", **{failed}** failed." if failed else ".")
            try:
                await interaction.edit_original_response(content=summary)
            except discord.HTTPException as e:
                log.debug("Guild %s: couldn't show the grant-existing result: %s", interaction.guild.id, e)


async def setup(bot: JoltBot) -> None:
    """discord.py extension entry point for the tickets cog.

    Args:
        bot: The running ``JoltBot`` instance.
    """
    await bot.add_cog(Tickets(bot))
