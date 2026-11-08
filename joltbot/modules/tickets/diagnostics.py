"""Self-check for one guild, shown by /tickets diagnostics.

Catches the setup mistakes that actually break ticket bots: role order,
missing channel permissions, deleted channels/roles, un-pingable roles, and
channels that pending members can see but shouldn't.
"""

from __future__ import annotations

from dataclasses import dataclass

import discord

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


@dataclass(frozen=True)
class Finding:
    level: str
    text: str

    def render(self) -> str:
        return f"{ICONS[self.level]} {self.text}"


def _missing(perms: discord.Permissions, names: tuple[str, ...]) -> list[str]:
    return [n.replace("_", " ").title() for n in names if not getattr(perms, n, False)]


def run_diagnostics(guild: discord.Guild, settings: TicketSettings, enabled: bool) -> list[Finding]:
    out: list[Finding] = []
    me = guild.me
    perms = me.guild_permissions

    if not enabled:
        out.append(Finding(WARN, "The ticket module is switched **off** on the dashboard."))
    for problem in settings.setup_problems():
        out.append(Finding(ERROR, f"Not set up: {problem}"))

    # --- server-wide permissions ---
    if perms.administrator:
        out.append(
            Finding(WARN, "JoltBot has **Administrator**. It works, but it's safer to grant only what it needs.")
        )
    else:
        for attr, (level, label) in REQUIRED_PERMS.items():
            if not getattr(perms, attr, False):
                out.append(Finding(level, f"Missing server permission: **{label}**."))

    # --- roles JoltBot gives or removes: must exist and sit below JoltBot's role ---
    assignable: list[tuple[str, int]] = [("Role given on accept", rid) for rid in settings.accept_role_ids]
    if settings.pending_role_id:
        assignable.append(("Pending role", settings.pending_role_id))
    assignable += [(f"Extra role '{c.label}'", c.role_id) for c in settings.extra_role_choices]
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

    # --- ping ---
    for role in roles_by_id(guild, settings.effective_ping_role_ids):
        if not role.mentionable and not perms.mention_everyone:
            out.append(
                Finding(
                    WARN,
                    f"**{role.name}** isn't mentionable and I lack 'Mention All Roles', so the "
                    "new-ticket ping won't notify anyone. Fix one of the two.",
                )
            )

    # --- channels ---
    panel = guild.get_channel(settings.panel_channel_id or 0)
    if settings.panel_channel_id and not isinstance(panel, discord.TextChannel):
        out.append(Finding(ERROR, "The panel channel no longer exists."))
    elif isinstance(panel, discord.TextChannel) and (missing := _missing(panel.permissions_for(me), CHANNEL_PERMS)):
        out.append(Finding(ERROR, f"In {panel.mention} I'm missing: {', '.join(missing)}."))

    category = guild.get_channel(settings.ticket_category_id or 0)
    if settings.ticket_category_id and not isinstance(category, discord.CategoryChannel):
        out.append(Finding(ERROR, "The ticket category no longer exists."))
    elif isinstance(category, discord.CategoryChannel):
        missing = _missing(
            category.permissions_for(me), ("view_channel", "manage_channels", "manage_roles", "send_messages")
        )
        if missing:
            out.append(
                Finding(
                    ERROR,
                    f"In the **{category.name}** category I'm missing: {', '.join(missing)} "
                    "(category permission overrides can block me even if my role has them).",
                )
            )
        if len(category.channels) >= 45:
            out.append(
                Finding(
                    WARN,
                    f"**{category.name}** has {len(category.channels)}/50 channels. Discord caps categories at 50.",
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

    # --- what can a pending member actually see? ---
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
            names = ", ".join(f"#{c.name}" for c in leaks[:15]) + (" …" if len(leaks) > 15 else "")
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

    if not any(f.level == ERROR for f in out):
        out.append(Finding(OK, "No blocking problems found."))
    return out
