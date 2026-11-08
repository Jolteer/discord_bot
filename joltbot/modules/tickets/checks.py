"""Who counts as staff, and can the bot actually do X? Errors are written for
the server admin, in plain words, with the exact fix."""

from __future__ import annotations

from collections.abc import Iterable

import discord

from .settings import TicketSettings


def is_staff(user: discord.abc.User, settings: TicketSettings) -> bool:
    if not isinstance(user, discord.Member):
        return False
    if settings.allow_administrators and user.guild_permissions.administrator:
        return True
    staff_ids = set(settings.staff_role_ids)
    return any(role.id in staff_ids for role in user.roles)


def can_run_setup_commands(user: discord.abc.User, settings: TicketSettings) -> bool:
    """Setup commands: anyone with Manage Server, plus the configured staff."""
    if not isinstance(user, discord.Member):
        return False
    return user.guild_permissions.manage_guild or is_staff(user, settings)


def hierarchy_problems(guild: discord.Guild, roles: Iterable[discord.Role]) -> list[str]:
    """Explain any role the bot can't give or take away."""
    me = guild.me
    problems: list[str] = []
    if not me.guild_permissions.manage_roles:
        problems.append(
            "I don't have the **Manage Roles** permission. Give JoltBot's role Manage Roles in Server Settings → Roles."
        )
    for role in roles:
        if role.managed:
            problems.append(f"**{role.name}** is managed by an integration or bot, so nobody can assign it.")
        elif role >= me.top_role:
            problems.append(
                f"My highest role (**{me.top_role.name}**) is not above **{role.name}**. "
                f"In Server Settings → Roles, drag JoltBot's role **above {role.name}**."
            )
    return problems


def kick_problem(guild: discord.Guild, member: discord.Member) -> str | None:
    me = guild.me
    if not me.guild_permissions.kick_members:
        return "I don't have the **Kick Members** permission. Grant it, or choose the option that doesn't kick."
    if member.id == guild.owner_id:
        return "That's the server owner; they can't be kicked."
    if member.top_role >= me.top_role:
        return (
            f"{member.mention}'s highest role (**{member.top_role.name}**) is not below mine "
            f"(**{me.top_role.name}**), so Discord won't let me kick them."
        )
    return None


def roles_by_id(guild: discord.Guild, ids: Iterable[int]) -> list[discord.Role]:
    """Look roles up in THIS guild only; IDs that don't exist here are skipped."""
    return [role for rid in ids if (role := guild.get_role(rid)) is not None]
