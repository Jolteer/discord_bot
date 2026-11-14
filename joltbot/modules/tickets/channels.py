"""The private channel a ticket lives in: its name, and who may see and use it."""

from __future__ import annotations

import re

import discord

from ...core.limits import CHANNEL_NAME_MAX
from .checks import roles_by_id
from .settings import TicketSettings

Overwrites = dict[discord.Role | discord.Member, discord.PermissionOverwrite]


def ticket_channel_name(member: discord.Member) -> str:
    """Build a Discord-safe channel name from the member's username.

    Args:
        member: Applicant whose name becomes the channel slug.

    Returns:
        Name like ``ticket-alice`` truncated to 100 characters.
    """
    slug = re.sub(r"[^a-z0-9-]+", "-", member.name.lower()).strip("-") or str(member.id)
    return f"ticket-{slug}"[:CHANNEL_NAME_MAX]


def ticket_overwrites(guild: discord.Guild, member: discord.Member, settings: TicketSettings) -> Overwrites:
    """Who may see a new ticket channel: the applicant, staff, the ping roles and JoltBot.

    Args:
        guild: Server the ticket is opened in.
        member: The applicant.
        settings: The server's ticket settings (staff and ping roles).

    Returns:
        Permission overwrites for ``guild.create_text_channel``. Everyone not
        listed is shut out through the @everyone entry.
    """
    staff_perms = discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        attach_files=True,
        embed_links=True,
        read_message_history=True,
        manage_messages=True,
    )
    overwrites: Overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            embed_links=True,
            attach_files=True,
            read_message_history=True,
            manage_channels=True,
            manage_messages=True,
        ),
        member: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            attach_files=True,
            embed_links=True,
            read_message_history=True,
        ),
    }
    for role in roles_by_id(guild, settings.staff_role_ids):
        overwrites[role] = staff_perms
    # A separate ping role must be able to see the ticket or the ping won't notify them.
    # It gets read/write access but NOT button powers (those are checked by role in the view).
    for role in roles_by_id(guild, settings.effective_ping_role_ids):
        overwrites.setdefault(
            role, discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        )
    return overwrites
