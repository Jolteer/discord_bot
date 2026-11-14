"""Self-check for one guild, shown by /tickets diagnostics.

Catches the setup mistakes that actually break ticket bots: role order,
missing channel permissions, deleted channels/roles, un-pingable roles, and
channels that pending members can see but shouldn't.
"""

from __future__ import annotations

from dataclasses import dataclass

import discord

from ...core.limits import CATEGORY_MAX_CHANNELS
from .checks import roles_by_id
from .settings import TicketSettings

OK, WARN, ERROR = "ok", "warn", "error"
ICONS = {OK: "✅", WARN: "⚠️", ERROR: "❌"}

# Server-wide permissions JoltBot needs for tickets, and why.
REQUIRED_PERMS: dict[str, tuple[str, str]] = {
    "view_channel": (ERROR, "View Channels"),
    "send_messages": (ERROR, "Send Messages"),
    "embed_links": (ERROR, "Embed Links (application and log embeds)"),
    "attach_files": (ERROR, "Attach Files (transcripts and re-uploaded files)"),
    "read_message_history": (ERROR, "Read Message History (transcripts)"),
    "manage_roles": (ERROR, "Manage Roles (give roles, set ticket permissions)"),
    "manage_channels": (ERROR, "Manage Channels (create and delete ticket channels)"),
    "kick_members": (WARN, "Kick Members (only needed for 'Deny and kick')"),
    "pin_messages": (WARN, "Pin Messages (to pin the panel; you can pin it by hand instead)"),
}
CHANNEL_PERMS = ("view_channel", "send_messages", "embed_links", "read_message_history")
# What JoltBot needs in the ticket category to create a private channel there.
CATEGORY_PERMS = ("view_channel", "manage_channels", "manage_roles", "send_messages")
CATEGORY_NEARLY_FULL = 45  # warn a little before Discord's cap of 50 channels per category
MAX_LEAKS_LISTED = 15  # channel names spelled out before the list is cut with "…"


@dataclass(frozen=True)
class Finding:
    """One line of the diagnostics report: how serious it is, and what to tell the admin."""

    level: str
    text: str

    def render(self) -> str:
        """The line as shown in Discord, with its icon."""
        return f"{ICONS[self.level]} {self.text}"


def _missing(perms: discord.Permissions, names: tuple[str, ...]) -> list[str]:
    """Display names of the permissions in ``names`` that ``perms`` doesn't have."""
    return [n.replace("_", " ").title() for n in names if not getattr(perms, n, False)]


def run_diagnostics(guild: discord.Guild, settings: TicketSettings, enabled: bool) -> list[Finding]:
    """Check one server's ticket setup against what Discord will actually allow.

    Args:
        guild: The server to check (uses the bot's cached roles, channels and permissions).
        settings: The server's ticket settings.
        enabled: Whether the ticket module is switched on there.

    Returns:
        Findings in the order they are shown: setup gaps, server permissions,
        roles, ping, channels, then what pending members can see. Ends with an
        all-clear line when nothing is an error.
    """
    out: list[Finding] = []
    if not enabled:
        out.append(Finding(WARN, "The ticket module is switched **off** on the dashboard."))
    for problem in settings.setup_problems():
        out.append(Finding(ERROR, f"Not set up: {problem}"))

    out += _check_server_permissions(guild)
    out += _check_roles(guild, settings)
    out += _check_ping(guild, settings)
    out += _check_channels(guild, settings)
    out += _check_pending_visibility(guild, settings)

    if not any(f.level == ERROR for f in out):
        out.append(Finding(OK, "No blocking problems found."))
    return out


def _check_server_permissions(guild: discord.Guild) -> list[Finding]:
    """JoltBot's server-wide permissions: each missing one, or a warning if it has Administrator."""
    out: list[Finding] = []
    perms = guild.me.guild_permissions
    if perms.administrator:
        out.append(
            Finding(WARN, "JoltBot has **Administrator**. It works, but it's safer to grant only what it needs.")
        )
    else:
        for attr, (level, label) in REQUIRED_PERMS.items():
            if not getattr(perms, attr, False):
                out.append(Finding(level, f"Missing server permission: **{label}**."))
    return out


def _check_roles(guild: discord.Guild, settings: TicketSettings) -> list[Finding]:
    """Every role JoltBot gives or removes must exist and sit below its own; staff roles must exist."""
    out: list[Finding] = []
    me = guild.me
    assignable = [(role.description, role.role_id) for role in settings.assignable_roles()]
    hierarchy_ok = True
    for name, rid in assignable:
        role = guild.get_role(rid)
        if role is None:
            out.append(Finding(ERROR, f"{name} (ID `{rid}`) no longer exists. Pick another on the dashboard."))
            hierarchy_ok = False
        elif role.managed:
            out.append(Finding(ERROR, f"{name} **{role.name}** is managed by an integration and can't be assigned."))
            hierarchy_ok = False
        elif role >= me.top_role:
            out.append(
                Finding(
                    ERROR,
                    f"My highest role **{me.top_role.name}** is NOT above **{role.name}**. "
                    f"Server Settings → Roles → drag JoltBot's role above **{role.name}**.",
                )
            )
            hierarchy_ok = False
    if hierarchy_ok and assignable:
        out.append(Finding(OK, f"Role order OK: **{me.top_role.name}** is above every role JoltBot gives."))

    for rid in settings.staff_role_ids:
        if guild.get_role(rid) is None:
            out.append(Finding(ERROR, f"A staff role (ID `{rid}`) no longer exists."))
    return out


def _check_ping(guild: discord.Guild, settings: TicketSettings) -> list[Finding]:
    """The new-ticket ping only notifies a role that is mentionable, unless JoltBot may mention all roles."""
    out: list[Finding] = []
    perms = guild.me.guild_permissions
    for role in roles_by_id(guild, settings.effective_ping_role_ids):
        if not role.mentionable and not perms.mention_everyone:
            out.append(
                Finding(
                    WARN,
                    f"**{role.name}** isn't mentionable and I lack 'Mention All Roles', so the "
                    "new-ticket ping won't notify anyone. Fix one of the two.",
                )
            )
    return out


def _check_channels(guild: discord.Guild, settings: TicketSettings) -> list[Finding]:
    """The panel channel, ticket category and log channel: still there, and usable by JoltBot."""
    out: list[Finding] = []
    me = guild.me
    panel = guild.get_channel(settings.panel_channel_id or 0)
    if settings.panel_channel_id and not isinstance(panel, discord.TextChannel):
        out.append(Finding(ERROR, "The panel channel no longer exists."))
    elif isinstance(panel, discord.TextChannel) and (missing := _missing(panel.permissions_for(me), CHANNEL_PERMS)):
        out.append(Finding(ERROR, f"In {panel.mention} I'm missing: {', '.join(missing)}."))

    category = guild.get_channel(settings.ticket_category_id or 0)
    if settings.ticket_category_id and not isinstance(category, discord.CategoryChannel):
        out.append(Finding(ERROR, "The ticket category no longer exists."))
    elif isinstance(category, discord.CategoryChannel):
        if missing := _missing(category.permissions_for(me), CATEGORY_PERMS):
            out.append(
                Finding(
                    ERROR,
                    f"In the **{category.name}** category I'm missing: {', '.join(missing)} "
                    "(category permission overrides can block me even if my role has them).",
                )
            )
        if len(category.channels) >= CATEGORY_NEARLY_FULL:
            out.append(
                Finding(
                    WARN,
                    f"**{category.name}** has {len(category.channels)}/{CATEGORY_MAX_CHANNELS} channels. "
                    f"Discord caps categories at {CATEGORY_MAX_CHANNELS}.",
                )
            )

    logs = guild.get_channel(settings.log_channel_id or 0)
    if settings.log_channel_id and not isinstance(logs, discord.TextChannel):
        out.append(Finding(ERROR, "The log channel no longer exists."))
    elif isinstance(logs, discord.TextChannel):
        if missing := _missing(logs.permissions_for(me), CHANNEL_PERMS + ("attach_files",)):
            out.append(Finding(ERROR, f"In {logs.mention} I'm missing: {', '.join(missing)}."))
        if logs.permissions_for(guild.default_role).view_channel:
            out.append(
                Finding(
                    WARN,
                    f"{logs.mention} is visible to @everyone. Transcripts contain applicant "
                    "answers, so make it staff-only.",
                )
            )
    return out


def _check_pending_visibility(guild: discord.Guild, settings: TicketSettings) -> list[Finding]:
    """What a member holding only the pending role can see: ideally just the panel and welcome channels."""
    out: list[Finding] = []
    panel = guild.get_channel(settings.panel_channel_id or 0)
    pending = guild.get_role(settings.pending_role_id or 0)
    if pending is not None:
        allowed = {settings.panel_channel_id, settings.welcome_channel_id}
        leaks = [
            ch
            for ch in guild.channels
            if not isinstance(ch, discord.CategoryChannel)
            and ch.id not in allowed
            and ch.category_id != settings.ticket_category_id
            and ch.permissions_for(pending).view_channel
        ]
        if leaks:
            names = ", ".join(f"#{c.name}" for c in leaks[:MAX_LEAKS_LISTED])
            names += " …" if len(leaks) > MAX_LEAKS_LISTED else ""
            out.append(
                Finding(
                    WARN,
                    f"Members with **{pending.name}** can see {len(leaks)} channel(s) besides "
                    f"the panel/welcome channels: {names}. Usually @everyone has View Channels "
                    "somewhere.",
                )
            )
        else:
            out.append(Finding(OK, f"Members with **{pending.name}** can only see the panel/welcome channels."))
        if isinstance(panel, discord.TextChannel) and not panel.permissions_for(pending).view_channel:
            out.append(
                Finding(
                    ERROR,
                    f"Members with **{pending.name}** can't see {panel.mention}! Allow View Channel for them there.",
                )
            )
    return out
