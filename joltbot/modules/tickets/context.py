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
    enabled: bool
    settings: TicketSettings
    colour: discord.Colour

    @property
    def ready(self) -> bool:
        """Switched on AND fully set up."""
        return self.enabled and not self.settings.setup_problems()


async def load_context(bot: JoltBot, guild_id: int) -> TicketContext:
    async with bot.db.session() as session:
        tickets = await load_module(session, guild_id, "tickets")
        general = await load_module(session, guild_id, "general")
    general_settings: GeneralSettings = general.config
    return TicketContext(
        enabled=tickets.enabled,
        settings=tickets.config,
        colour=discord.Colour(general_settings.embed_color_int),
    )
