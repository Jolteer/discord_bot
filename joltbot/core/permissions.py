"""Discord permission bits.

Discord sends permissions as one big integer (as a string in JSON); each
permission is one bit. See https://discord.com/developers/docs/topics/permissions
"""

from __future__ import annotations

from urllib.parse import urlencode

KICK_MEMBERS = 1 << 1
BAN_MEMBERS = 1 << 2
ADMINISTRATOR = 1 << 3
MANAGE_CHANNELS = 1 << 4
MANAGE_GUILD = 1 << 5  # shown as "Manage Server" in the Discord app
VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
MANAGE_MESSAGES = 1 << 13
EMBED_LINKS = 1 << 14
ATTACH_FILES = 1 << 15
READ_MESSAGE_HISTORY = 1 << 16
MENTION_EVERYONE = 1 << 17  # one bit for "@everyone, @here and all roles"
MANAGE_NICKNAMES = 1 << 27
MANAGE_ROLES = 1 << 28
MANAGE_WEBHOOKS = 1 << 29
MANAGE_EXPRESSIONS = 1 << 30
MANAGE_EVENTS = 1 << 33
MANAGE_THREADS = 1 << 34
MODERATE_MEMBERS = 1 << 40
PIN_MESSAGES = 1 << 51

# Where both the bot invite and the dashboard login send the browser.
DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"

# What JoltBot asks for in its invite link. Same set the old bot used; nothing
# more, and deliberately NOT Administrator.
BOT_PERMISSIONS = {
    "Kick Members": KICK_MEMBERS,  # only for Deny -> Kick
    "Manage Channels": MANAGE_CHANNELS,  # create/delete ticket channels
    "View Channels": VIEW_CHANNEL,
    "Send Messages": SEND_MESSAGES,
    "Manage Messages": MANAGE_MESSAGES,
    "Embed Links": EMBED_LINKS,
    "Attach Files": ATTACH_FILES,  # transcripts + re-uploaded attachments
    "Read Message History": READ_MESSAGE_HISTORY,  # transcripts
    "Mention All Roles": MENTION_EVERYONE,  # so new-ticket pings notify staff roles that aren't mentionable
    "Manage Roles": MANAGE_ROLES,  # assign roles; set private permissions on each ticket
    "Pin Messages": PIN_MESSAGES,  # pin the application panel
}
BOT_PERMISSIONS_VALUE = sum(BOT_PERMISSIONS.values())  # = 2251800082377746


# Permissions that let a member moderate or reconfigure a server. A role carrying any
# of these is "powerful": only an Administrator or the owner may make JoltBot hand it
# out, because in Discord itself Manage Server alone can't assign roles at all.
ELEVATED_PERMISSIONS = {
    "Administrator": ADMINISTRATOR,
    "Kick Members": KICK_MEMBERS,
    "Ban Members": BAN_MEMBERS,
    "Manage Channels": MANAGE_CHANNELS,
    "Manage Server": MANAGE_GUILD,
    "Manage Messages": MANAGE_MESSAGES,
    "Mention @everyone": MENTION_EVERYONE,
    "Manage Nicknames": MANAGE_NICKNAMES,
    "Manage Roles": MANAGE_ROLES,
    "Manage Webhooks": MANAGE_WEBHOOKS,
    "Manage Expressions": MANAGE_EXPRESSIONS,
    "Manage Events": MANAGE_EVENTS,
    "Manage Threads": MANAGE_THREADS,
    "Moderate Members": MODERATE_MEMBERS,
}


def _bits(permissions: int | str | None) -> int:
    """Discord sends permissions as a decimal string; anything unreadable counts as none."""
    try:
        return int(permissions or 0)
    except (TypeError, ValueError):
        return 0


def can_manage_guild(permissions: int | str | None, *, owner: bool = False) -> bool:
    """Whether a Discord user may manage JoltBot for a server.

    True for the guild owner, anyone with Administrator, or Manage Server.

    Args:
        permissions: Discord permission bitfield (int or decimal string from JSON).
        owner: True if the user owns the guild (short-circuits the bit check).

    Returns:
        True if the user may open dashboard guild pages / change settings.
    """
    return owner or bool(_bits(permissions) & (ADMINISTRATOR | MANAGE_GUILD))


def is_administrator(permissions: int | str | None, *, owner: bool = False) -> bool:
    """Whether a Discord user has full control of a server (owner or Administrator).

    Args:
        permissions: Discord permission bitfield (int or decimal string from JSON).
        owner: True if the user owns the guild.

    Returns:
        True for the owner or anyone with Administrator. Manage Server alone is not enough.
    """
    return owner or bool(_bits(permissions) & ADMINISTRATOR)


def elevated_permission_names(permissions: int | str | None) -> list[str]:
    """Names of the moderation/management permissions in a role's bitfield.

    Args:
        permissions: The role's Discord permission bitfield.

    Returns:
        Display names from ``ELEVATED_PERMISSIONS``; empty for an ordinary role.
    """
    bits = _bits(permissions)
    return [name for name, bit in ELEVATED_PERMISSIONS.items() if bits & bit]


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
    return f"{DISCORD_AUTHORIZE_URL}?{urlencode(params)}"
