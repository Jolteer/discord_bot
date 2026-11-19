"""Loads one guild's alliance settings for the event being handled.

Every click and command calls ``load_context`` for the guild it happened in.
Settings are read fresh from the database each time, so dashboard changes
apply immediately.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from ...core.guild_settings import load_module
from ..general.settings import MODULE as GENERAL
from ..general.settings import GeneralSettings
from .constants import MODULE
from .settings import AllianceSettings

if TYPE_CHECKING:
    from ...bot.client import JoltBot


@dataclass(frozen=True)
class AllianceContext:
    """Fresh alliance + general settings for one guild event.

    Attributes:
        enabled: The module's switch on the dashboard.
        settings: Validated ``AllianceSettings``.
        colour: Embed colour from general settings.
    """

    enabled: bool
    settings: AllianceSettings
    colour: discord.Colour

    @property
    def ready(self) -> bool:
        """True when the module is on and setup has no blocking gaps."""
        return self.enabled and not self.settings.setup_problems()


async def load_context(bot: JoltBot, guild_id: int) -> AllianceContext:
    """Load alliance and general settings for ``guild_id`` from the database.

    Args:
        bot: Running bot (provides ``db``).
        guild_id: Discord guild snowflake for the event.

    Returns:
        Immutable ``AllianceContext`` used by views, commands and the service.
    """
    async with bot.db.session() as session:
        alliances = await load_module(session, guild_id, MODULE)
        general = await load_module(session, guild_id, GENERAL)
    general_settings: GeneralSettings = general.config
    return AllianceContext(
        enabled=alliances.enabled,
        settings=alliances.config,
        colour=discord.Colour(general_settings.embed_color_int),
    )
