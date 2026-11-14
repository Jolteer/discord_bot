"""Small helpers for answering Discord interactions (slash commands, buttons, forms)."""

from __future__ import annotations

import logging

import discord

log = logging.getLogger(__name__)


async def safe_reply(interaction: discord.Interaction, text: str) -> None:
    """Reply privately, whether or not the interaction was already answered.

    Used on error paths, so it never raises: if the reply itself can't be
    delivered there is nobody left to tell.

    Args:
        interaction: Discord component/slash interaction.
        text: Ephemeral message body.
    """
    try:
        # An interaction can be answered only once; after that Discord wants a follow-up.
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except (discord.HTTPException, OSError) as e:
        log.debug("Couldn't deliver a private reply: %s", e)
