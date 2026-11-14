"""Roles and channels in the shape the dashboard's dropdowns and tick lists need.

IDs go out as strings: a Discord ID is too big for a JavaScript number, so a
browser would silently round it (see ``core/types.py``). No FastAPI here, so a
module's own form code can use these too.
"""

from __future__ import annotations

from typing import Any

from ..core.discord_rest import GuildData, Role


def role_option(role: Role) -> dict[str, Any]:
    """One role as a template row.

    Args:
        role: A role of the server.

    Returns:
        ``{"id", "name", "color", "managed"}``; ``color`` is ``#rrggbb``, or
        ``None`` for a role with no colour of its own.
    """
    return {
        "id": str(role.id),
        "name": role.name,
        "color": f"#{role.color:06x}" if role.color else None,
        "managed": role.managed,
    }


def text_channel_options(data: GuildData) -> list[dict[str, Any]]:
    """The server's text channels as template rows, each with its category's name for grouping.

    Args:
        data: Live roles and channels for the server.

    Returns:
        ``{"id", "name", "category"}`` per channel; ``category`` is ``""`` for
        a channel outside any category.
    """
    categories = {c.id: c.name for c in data.categories()}
    return [
        {"id": str(c.id), "name": c.name, "category": categories.get(c.parent_id or 0, "")}
        for c in data.text_channels()
    ]
