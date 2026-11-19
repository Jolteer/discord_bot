"""Who may decide an alliance's join requests, and who may run the /alliances commands.

These read the member's roles as they are right now, so taking a role away in
Discord (or on the dashboard) takes effect on the next click.
"""

from __future__ import annotations

import discord

from .settings import Alliance, AllianceSettings


def _has_any(user: object, role_ids: set[int]) -> bool:
    """Whether ``user`` is a member of the server holding one of the roles."""
    return isinstance(user, discord.Member) and any(role.id in role_ids for role in user.roles)


def can_review(user: object, alliance: Alliance, settings: AllianceSettings) -> bool:
    """Whether ``user`` may accept or reject requests to join ``alliance``.

    This is the permission check. Being able to see the request channel means
    nothing here, and neither does Administrator or Manage Server: only a role
    listed in the settings counts.

    Args:
        user: Whoever clicked (a ``Member`` when the click came from inside a server).
        alliance: The alliance the request is for.
        settings: The server's alliance settings (its override roles).

    Returns:
        True if they hold one of that alliance's reviewer roles, or an override role.
    """
    return _has_any(user, {*alliance.reviewer_role_ids, *settings.override_role_ids})


def can_manage_requests(user: object, settings: AllianceSettings) -> bool:
    """Whether ``user`` may run the ``/alliances`` commands (post the panel, list, cancel).

    Args:
        user: Whoever ran the command.
        settings: The server's alliance settings (its override roles).

    Returns:
        True for Manage Server, or an override role. An alliance's own
        reviewers decide requests but don't manage the module.
    """
    if not isinstance(user, discord.Member):
        return False
    return user.guild_permissions.manage_guild or _has_any(user, set(settings.override_role_ids))
