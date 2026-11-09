"""Who counts as staff, and can the bot actually do X? Errors are written for
the server admin, in plain words, with the exact fix."""

from __future__ import annotations

from collections.abc import Iterable

import discord

from .settings import TicketSettings


def is_staff(user: discord.abc.User, settings: TicketSettings) -> bool:
    """Whether ``user`` may use ticket control buttons.

    Args:
        user: Interaction user (must be a ``Member`` in the guild).
        settings: Guild ticket settings (staff roles / admin flag).

    Returns:
        True if they have a configured staff role, or Administrator when
        ``allow_administrators`` is enabled.
    """
    if not isinstance(user, discord.Member):
        return False
    if settings.allow_administrators and user.guild_permissions.administrator:
        return True
    staff_ids = set(settings.staff_role_ids)
    return any(role.id in staff_ids for role in user.roles)


def can_run_setup_commands(user: discord.abc.User, settings: TicketSettings) -> bool:
    """Whether ``user`` may run ``/tickets`` setup commands.

    Broader than Discord's ``default_permissions(manage_guild)`` visibility:
    Manage Server **or** configured staff.

    Args:
        user: Interaction user.
        settings: Guild ticket settings.

    Returns:
        True if Manage Server or ``is_staff``.
    """
    if not isinstance(user, discord.Member):
        return False
    return user.guild_permissions.manage_guild or is_staff(user, settings)


def hierarchy_problems(guild: discord.Guild, roles: Iterable[discord.Role]) -> list[str]:
    """Explain any role the bot can't give or take away.

    Args:
        guild: Guild where roles would be assigned.
        roles: Roles the bot needs to manage.

    Returns:
        Human-readable problem strings (empty if the bot can manage them all).
    """
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
    """Why Deny-and-kick would fail for this member, if at all.

    Args:
        guild: Guild to kick from.
        member: Applicant to kick.

    Returns:
        Error text for staff, or ``None`` if a kick should succeed.
    """
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
    """Resolve role IDs against this guild only; missing IDs are skipped.

    Args:
        guild: Guild to look up roles in.
        ids: Discord role snowflakes (possibly stale).

    Returns:
        Existing ``Role`` objects in the order of ``ids``.
    """
    return [role for rid in ids if (role := guild.get_role(rid)) is not None]
