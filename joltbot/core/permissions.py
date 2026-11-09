"""Discord permission bits.

Discord sends permissions as one big integer (as a string in JSON); each
permission is one bit. See https://discord.com/developers/docs/topics/permissions
"""

from __future__ import annotations

from urllib.parse import urlencode

ADMINISTRATOR = 1 << 3
MANAGE_GUILD = 1 << 5  # shown as "Manage Server" in the Discord app

# What JoltBot asks for in its invite link. Same set the old bot used; nothing
# more, and deliberately NOT Administrator.
BOT_PERMISSIONS = {
    "Kick Members": 1 << 1,  # only for Deny -> Kick
    "Manage Channels": 1 << 4,  # create/delete ticket channels
    "View Channels": 1 << 10,
    "Send Messages": 1 << 11,
    "Manage Messages": 1 << 13,
    "Embed Links": 1 << 14,
    "Attach Files": 1 << 15,  # transcripts + re-uploaded attachments
    "Read Message History": 1 << 16,  # transcripts
    "Mention All Roles": 1 << 17,  # so new-ticket pings notify staff roles that aren't mentionable
    "Manage Roles": 1 << 28,  # assign roles; set private permissions on each ticket
    "Pin Messages": 1 << 51,  # pin the application panel
}
BOT_PERMISSIONS_VALUE = sum(BOT_PERMISSIONS.values())  # = 2251800082377746


def can_manage_guild(permissions: int | str | None, *, owner: bool = False) -> bool:
    """Whether a Discord user may manage JoltBot for a server.

    True for the guild owner, anyone with Administrator, or Manage Server.

    Args:
        permissions: Discord permission bitfield (int or decimal string from JSON).
        owner: True if the user owns the guild (short-circuits the bit check).

    Returns:
        True if the user may open dashboard guild pages / change settings.
    """
    if owner:
        return True
    try:
        bits = int(permissions or 0)
    except (TypeError, ValueError):
        return False
    return bool(bits & ADMINISTRATOR) or bool(bits & MANAGE_GUILD)


def bot_invite_url(client_id: str, guild_id: int | str | None = None) -> str:
    """Build an OAuth invite with JoltBot's permission set pre-selected.

    Deliberately omits Administrator. With ``guild_id``, that server is
    pre-selected and the picker is locked.

    Args:
        client_id: Discord application client ID.
        guild_id: Optional guild to pre-select.

    Returns:
        Full ``https://discord.com/oauth2/authorize?...`` URL.
    """
    params = {
        "client_id": client_id,
        "scope": "bot applications.commands",
        "permissions": str(BOT_PERMISSIONS_VALUE),
    }
    if guild_id is not None:
        params["guild_id"] = str(guild_id)
        params["disable_guild_select"] = "true"
    return "https://discord.com/oauth2/authorize?" + urlencode(params)
