"""Loads one guild's mod log settings for the event being handled.

Settings are read fresh from the database each time, like every other module,
so dashboard changes apply immediately.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ...core.guild_settings import load_module
from .constants import MODULE
from .settings import ModLogSettings

if TYPE_CHECKING:
    from ...bot.client import JoltBot


@dataclass(frozen=True)
class ModLogContext:
    """Fresh mod log settings for one guild event.

    Attributes:
        enabled: The module's on/off switch for the server.
        settings: Validated ``ModLogSettings``.
        version: Settings version, needed to save a change (see ``save_module``).
    """

    enabled: bool
    settings: ModLogSettings
    version: int


async def load_context(bot: JoltBot, guild_id: int) -> ModLogContext:
    """Load the mod log settings for ``guild_id`` from the database.

    Args:
        bot: Running bot (provides ``db``).
        guild_id: Discord guild snowflake for the event.

    Returns:
        Immutable ``ModLogContext``.
    """
    async with bot.db.session() as session:
        state = await load_module(session, guild_id, MODULE)
    return ModLogContext(enabled=state.enabled, settings=state.config, version=state.version)
