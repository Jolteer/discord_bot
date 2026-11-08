"""Self-check: run on startup (printed to the log) and on /verify-diagnostics (shown in Discord).

It catches the setup mistakes that actually break ticket bots: role hierarchy,
missing channel permissions, wrong IDs, un-pingable roles, and channels that
Unverified members can see but shouldn't.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from ..client import TicketBot

OK, WARN, ERROR = "ok", "warn", "error"
ICONS = {OK: "✅", WARN: "⚠️", ERROR: "❌"}


@dataclass(frozen=True)
class Finding:
    level: str
    text: str

    def render(self) -> str:
        return f"{ICONS[self.level]} {self.text}"


# Guild-level permissions the bot needs, and why.
REQUIRED_PERMS: dict[str, tuple[str, str]] = {
    "view_channel": (ERROR, "View Channels"),
    "send_messages": (ERROR, "Send Messages"),
    "embed_links": (ERROR, "Embed Links (application/log embeds)"),
    "attach_files": (ERROR, "Attach Files (transcripts + screenshot re-uploads)"),
    "read_message_history": (ERROR, "Read Message History (transcripts)"),
    "manage_roles": (ERROR, "Manage Roles (assign Member/Unverified/alliance roles, set ticket permissions)"),
    "manage_channels": (ERROR, "Manage Channels (create/delete ticket channels)"),
    "kick_members": (WARN, "Kick Members (only needed for Deny → Kick)"),
    "pin_messages": (WARN, "Pin Messages (to pin the verify panel; you can pin it by hand instead)"),
}

CHANNEL_PERMS = ("view_channel", "send_messages", "embed_links", "read_message_history")


def _missing(perms: discord.Permissions, names: tuple[str, ...]) -> list[str]:
    return [n.replace("_", " ").title() for n in names if not getattr(perms, n, False)]


def run_diagnostics(bot: "TicketBot") -> list[Finding]:
    cfg = bot.cfg
    out: list[Finding] = []
    guild = bot.get_guild(cfg.guild_id)
    if guild is None:
        return [
            Finding(
                ERROR,
                f"I'm not in a server with ID {cfg.guild_id}. Check guild_id in config.json and that the bot "
                "was invited with the invite link from the README.",
            )
        ]
    me = guild.me
    perms = me.guild_permissions

    # --- server-wide permissions ---
    if perms.administrator:
        out.append(Finding(WARN, "The bot has **Administrator**. It works, but it's safer to grant only the permissions listed in the README."))
    else:
        for attr, (level, label) in REQUIRED_PERMS.items():
            if not getattr(perms, attr, False):
                out.append(Finding(level, f"Missing server permission: **{label}**."))

    # --- roles + hierarchy ---
    assignable = {"Member": cfg.member_role_id, "Unverified": cfg.unverified_role_id}
    assignable.update({f"Alliance '{a.label}'": a.role_id for a in cfg.alliance_roles})
    hierarchy_ok = True
    for name, rid in assignable.items():
        role = guild.get_role(rid)
        if role is None:
            out.append(Finding(ERROR, f"{name} role (ID `{rid}`) doesn't exist in this server. Fix the ID in config.json."))
            hierarchy_ok = False
        elif role.managed:
            out.append(Finding(ERROR, f"{name} role **{role.name}** is managed by an integration and can't be assigned."))
            hierarchy_ok = False
        elif role >= me.top_role:
            out.append(
                Finding(
                    ERROR,
                    f"My highest role **{me.top_role.name}** (position {me.top_role.position}) is NOT above "
                    f"**{role.name}** (position {role.position}). Server Settings → Roles → drag my role above "
                    f"**{role.name}**, or I can't assign it.",
                )
            )
            hierarchy_ok = False
    if hierarchy_ok:
        out.append(Finding(OK, f"Role hierarchy OK — **{me.top_role.name}** is above Member, Unverified and alliance roles."))

    for name, rid in cfg.approval_roles.items():
        if guild.get_role(rid) is None:
            out.append(Finding(ERROR, f"Approval role '{name}' (ID `{rid}`) doesn't exist in this server."))

    # --- ping ---
    if cfg.ping_enabled:
        for rid in cfg.effective_ping_role_ids:
            role = guild.get_role(rid)
            if role is None:
                out.append(Finding(ERROR, f"Ping role ID `{rid}` doesn't exist."))
            elif not role.mentionable and not perms.mention_everyone:
                out.append(
                    Finding(
                        WARN,
                        f"**{role.name}** isn't mentionable and I lack 'Mention @everyone, @here, and All Roles', "
                        "so the new-ticket ping will show but won't notify anyone. Fix one of the two.",
                    )
                )

    # --- channels ---
    verify = guild.get_channel(cfg.verify_channel_id)
    if not isinstance(verify, discord.TextChannel):
        out.append(Finding(ERROR, f"channels.verify (`{cfg.verify_channel_id}`) is not a text channel in this server."))
    elif missing := _missing(verify.permissions_for(me), CHANNEL_PERMS):
        out.append(Finding(ERROR, f"In {verify.mention} I'm missing: {', '.join(missing)}."))

    category = guild.get_channel(cfg.tickets_category_id)
    if not isinstance(category, discord.CategoryChannel):
        out.append(Finding(ERROR, f"channels.tickets_category (`{cfg.tickets_category_id}`) is not a category in this server."))
    else:
        cat_perms = category.permissions_for(me)
        missing = _missing(cat_perms, ("view_channel", "manage_channels", "manage_roles", "send_messages"))
        if missing:
            out.append(
                Finding(
                    ERROR,
                    f"In the **{category.name}** category I'm missing: {', '.join(missing)} "
                    "(category permission overrides can block me even if my role has them).",
                )
            )
        if len(category.channels) >= 45:
            out.append(Finding(WARN, f"**{category.name}** has {len(category.channels)}/50 channels. Discord caps categories at 50."))

    logs = guild.get_channel(cfg.ticket_logs_channel_id)
    if not isinstance(logs, discord.TextChannel):
        out.append(Finding(ERROR, f"channels.ticket_logs (`{cfg.ticket_logs_channel_id}`) is not a text channel in this server."))
    else:
        if missing := _missing(logs.permissions_for(me), CHANNEL_PERMS + ("attach_files",)):
            out.append(Finding(ERROR, f"In {logs.mention} I'm missing: {', '.join(missing)}."))
        if logs.permissions_for(guild.default_role).view_channel:
            out.append(Finding(WARN, f"{logs.mention} is visible to @everyone. Transcripts contain applicant info — make it staff-only."))

    # --- what can an Unverified member actually see? ---
    unverified = guild.get_role(cfg.unverified_role_id)
    if unverified is not None:
        allowed = {cfg.verify_channel_id, cfg.welcome_channel_id}
        leaks = [
            ch
            for ch in guild.channels
            if not isinstance(ch, discord.CategoryChannel)
            and ch.id not in allowed
            and ch.category_id != cfg.tickets_category_id
            and ch.permissions_for(unverified).view_channel
        ]
        if leaks:
            names = ", ".join(f"#{c.name}" for c in leaks[:15]) + (" …" if len(leaks) > 15 else "")
            out.append(
                Finding(
                    WARN,
                    f"Unverified members can currently see {len(leaks)} channel(s) besides verify/welcome: {names}. "
                    "See README step 4 (usually @everyone has View Channels somewhere).",
                )
            )
        else:
            out.append(Finding(OK, "Unverified members can only see the verify/welcome channels."))
        if isinstance(verify, discord.TextChannel) and not verify.permissions_for(unverified).view_channel:
            out.append(Finding(ERROR, f"Unverified members can't see {verify.mention}! Allow View Channel for Unverified there."))

    # --- intents (privileged) ---
    if not bot.intents.members:
        out.append(Finding(ERROR, "Server Members intent is off in code."))
    if not bot.intents.message_content:
        out.append(Finding(WARN, "Message Content intent is off — transcripts will have empty messages."))

    if not any(f.level == ERROR for f in out):
        out.append(Finding(OK, "No blocking problems found."))
    return out
