"""Loads one guild's ticket settings for the event being handled.

Every event and button calls ``load_context`` for the guild it happened in.
Settings are read fresh from the database each time, so dashboard changes
apply immediately.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from ...core.guild_settings import load_module
from ..general.settings import GeneralSettings
from .settings import TicketSettings

if TYPE_CHECKING:
    from ...bot.client import JoltBot


@dataclass(frozen=True)
class TicketContext:
    """Fresh ticket + general settings for one guild event.

    Attributes:
        enabled: Tickets module toggle from the dashboard.
        settings: Validated ``TicketSettings``.
        colour: Embed colour from general settings.
    """

    enabled: bool
    settings: TicketSettings
    colour: discord.Colour

    @property
    def ready(self) -> bool:
        """True when the module is on and setup has no blocking gaps.

        Returns:
            Whether ticket creation / panel flows should proceed.
        """
        return self.enabled and not self.settings.setup_problems()


async def load_context(bot: JoltBot, guild_id: int) -> TicketContext:
    """Load ticket and general settings for ``guild_id`` from the database.

    Args:
        bot: Running bot (provides ``db``).
        guild_id: Discord guild snowflake for the event.

    Returns:
        Immutable ``TicketContext`` used by views, commands, and the service.
    """
    async with bot.db.session() as session:
        tickets = await load_module(session, guild_id, "tickets")
        general = await load_module(session, guild_id, "general")
    general_settings: GeneralSettings = general.config
    return TicketContext(
        enabled=tickets.enabled,
        settings=tickets.config,
        colour=discord.Colour(general_settings.embed_color_int),
    )
