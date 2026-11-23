"""Small pieces every settings page of the dashboard needs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException, Request
from starlette.datastructures import FormData

from ..core.discord_rest import DiscordError, GuildData
from .security import get_discord

# Matches no saved version, so a form that sent none (or nonsense) is refused as out of date.
NO_VERSION = -1
STALE_SETTINGS = "Someone else saved settings while you were editing. Reload the page and try again."


def form_text(form: Mapping[str, Any], name: str) -> str:
    """One form field as text, with line endings normalised and the ends trimmed.

    Args:
        form: The submitted form.
        name: The field's name.

    Returns:
        The text; ``""`` for a missing field, or a file where text was expected.
    """
    value = form.get(name)
    return value.replace("\r\n", "\n").strip() if isinstance(value, str) else ""


def form_ids(form: Any, name: str) -> list[str]:
    """Every non-empty value of a field sent more than once (a tick list sends one per ticked box).

    Args:
        form: The submitted form (supports ``getlist``).
        name: The field's name.

    Returns:
        The values in the order they were sent, without repeats.
    """
    return list(dict.fromkeys(v.strip() for v in form.getlist(name) if isinstance(v, str) and v.strip()))


def form_version(form: FormData, field: str = "version") -> int:
    """Read the settings version a form was loaded with.

    Every settings form carries the version it was rendered from in a hidden
    field. ``save_module`` refuses the save if someone else saved in between.

    Args:
        form: The submitted form.
        field: Name of the hidden version field.

    Returns:
        The version, or ``NO_VERSION`` if the field is missing or not a number.
    """
    try:
        # The ignore: a file uploaded under this name is not a number. See B7 in docs/refactor/AUDIT.md.
        return int(form.get(field, str(NO_VERSION)))  # type: ignore[arg-type]
    except ValueError:
        return NO_VERSION


async def require_guild_data(request: Request, guild_id: int, what: str) -> GuildData:
    """Load a server's roles and channels from Discord for a page that can't work without them.

    Args:
        request: Incoming HTTP request.
        guild_id: The server, already checked by ``guild_access``.
        what: What the page needs, for the error text (e.g. ``"roles and channels"``).

    Returns:
        The server's roles, channels and emojis (cached for a minute).

    Raises:
        HTTPException: 503 if Discord can't be reached.
    """
    try:
        return await get_discord(request).guild_data(guild_id)
    except DiscordError as e:
        raise HTTPException(503, f"Couldn't load this server's {what} from Discord. {e}") from None
