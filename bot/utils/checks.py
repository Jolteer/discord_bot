"""Permission helpers: who counts as staff, and can the bot actually do X?"""
from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

import discord
from discord import app_commands

if TYPE_CHECKING:
    from ..config import Config


def is_staff(user: discord.abc.User, cfg: "Config") -> bool:
    if not isinstance(user, discord.Member):
        return False
    if cfg.allow_administrators and user.guild_permissions.administrator:
        return True
    staff_ids = cfg.approval_role_ids
    return any(role.id in staff_ids for role in user.roles)


def staff_only():
    """Slash-command check: only approval roles may run it."""

    async def predicate(interaction: discord.Interaction) -> bool:
        cfg = interaction.client.cfg  # type: ignore[attr-defined]
        if interaction.guild_id != cfg.guild_id:
            raise app_commands.CheckFailure("This command only works in the kingdom server.")
        if not is_staff(interaction.user, cfg):
            raise app_commands.CheckFailure(cfg.messages["not_staff"])
        return True

    return app_commands.check(predicate)


def hierarchy_problems(guild: discord.Guild, roles: Iterable[discord.Role]) -> list[str]:
    """Explain, in plain words, any role the bot can't assign/remove."""
    me = guild.me
    problems: list[str] = []
    if not me.guild_permissions.manage_roles:
        problems.append(
            "I don't have the **Manage Roles** permission. Give the bot's role Manage Roles in "
            "Server Settings → Roles."
        )
    for role in roles:
        if role.managed:
            problems.append(f"**{role.name}** is managed by an integration/bot, so nobody can assign it manually.")
        elif role >= me.top_role:
            problems.append(
                f"My highest role (**{me.top_role.name}**) is not above **{role.name}**. "
                f"Go to Server Settings → Roles and drag my role **above {role.name}**, "
                "otherwise Discord won't let me assign or remove it."
            )
    return problems


def kick_problem(guild: discord.Guild, member: discord.Member) -> str | None:
    me = guild.me
    if not me.guild_permissions.kick_members:
        return "I don't have the **Kick Members** permission, so I can't kick. Grant it or choose 'Keep Unverified'."
    if member.id == guild.owner_id:
        return "That's the server owner — they can't be kicked."
    if member.top_role >= me.top_role:
        return (
            f"{member.mention}'s highest role (**{member.top_role.name}**) is not below mine "
            f"(**{me.top_role.name}**), so Discord won't let me kick them."
        )
    return None

