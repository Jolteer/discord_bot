"""Names for the fixed strings the mod log stores, and the list of events it can record.

Every event key and category here is saved in the database (in ``modlog_entries``
and in each server's settings), so a value must never change: old rows would
stop matching. Imported by both the bot and the dashboard, so this file must
not import discord.py or FastAPI.
"""

from __future__ import annotations

from dataclasses import dataclass

# This module's key in the registry and in module_settings.
MODULE = "modlog"

# Log categories. Each has its own channel per server (see settings.py).
CATEGORY_MEMBERS = "members"
CATEGORY_MESSAGES = "messages"
CATEGORY_MODERATION = "moderation"
CATEGORY_SERVER = "server"
CATEGORY_VOICE = "voice"  # posts in the members channel unless a voice channel is set
CATEGORIES = (CATEGORY_MEMBERS, CATEGORY_MESSAGES, CATEGORY_MODERATION, CATEGORY_SERVER, CATEGORY_VOICE)
CATEGORY_TITLES = {
    CATEGORY_MEMBERS: "Members",
    CATEGORY_MESSAGES: "Messages",
    CATEGORY_MODERATION: "Moderation",
    CATEGORY_SERVER: "Server",
    CATEGORY_VOICE: "Voice",
}

# What sort of thing happened. Only decides the embed colour.
KIND_ADD = "add"
KIND_REMOVE = "remove"
KIND_CHANGE = "change"
KIND_WARN = "warn"  # kick, timeout
KIND_PUNISH = "punish"  # ban


@dataclass(frozen=True)
class EventSpec:
    """One kind of event the mod log can record.

    Attributes:
        key: Stored in the database; never rename.
        category: Which log channel it goes to.
        title: Embed title and the label on the dashboard.
        kind: One of the ``KIND_`` values; picks the embed colour.
        default_on: Whether a server records it before anyone changes the toggle.
    """

    key: str
    category: str
    title: str
    kind: str
    default_on: bool = True


EVENT_MEMBER_JOIN = "member_join"
EVENT_MEMBER_LEAVE = "member_leave"
EVENT_NICKNAME = "nickname_change"
EVENT_MEMBER_ROLES = "member_roles"
EVENT_AVATAR = "avatar_change"
EVENT_BAN = "member_ban"
EVENT_UNBAN = "member_unban"
EVENT_KICK = "member_kick"
EVENT_TIMEOUT_ADD = "timeout_add"
EVENT_TIMEOUT_REMOVE = "timeout_remove"
EVENT_MESSAGE_EDIT = "message_edit"
EVENT_MESSAGE_DELETE = "message_delete"
EVENT_BULK_DELETE = "message_bulk_delete"
EVENT_ROLE_CREATE = "role_create"
EVENT_ROLE_DELETE = "role_delete"
EVENT_ROLE_UPDATE = "role_update"
EVENT_CHANNEL_CREATE = "channel_create"
EVENT_CHANNEL_DELETE = "channel_delete"
EVENT_CHANNEL_UPDATE = "channel_update"
EVENT_SERVER_UPDATE = "server_update"
EVENT_EMOJI = "emoji_change"
EVENT_STICKER = "sticker_change"
EVENT_WEBHOOK = "webhook_change"
EVENT_VOICE_JOIN = "voice_join"
EVENT_VOICE_LEAVE = "voice_leave"
EVENT_VOICE_MOVE = "voice_move"

EVENTS: dict[str, EventSpec] = {
    e.key: e
    for e in (
        EventSpec(EVENT_MEMBER_JOIN, CATEGORY_MEMBERS, "Member joined", KIND_ADD),
        EventSpec(EVENT_MEMBER_LEAVE, CATEGORY_MEMBERS, "Member left", KIND_REMOVE),
        EventSpec(EVENT_NICKNAME, CATEGORY_MEMBERS, "Nickname changed", KIND_CHANGE),
        EventSpec(EVENT_MEMBER_ROLES, CATEGORY_MEMBERS, "Member roles changed", KIND_CHANGE),
        EventSpec(EVENT_AVATAR, CATEGORY_MEMBERS, "Avatar changed", KIND_CHANGE, default_on=False),
        EventSpec(EVENT_BAN, CATEGORY_MODERATION, "Member banned", KIND_PUNISH),
        EventSpec(EVENT_UNBAN, CATEGORY_MODERATION, "Member unbanned", KIND_ADD),
        EventSpec(EVENT_KICK, CATEGORY_MODERATION, "Member kicked", KIND_WARN),
        EventSpec(EVENT_TIMEOUT_ADD, CATEGORY_MODERATION, "Member timed out", KIND_WARN),
        EventSpec(EVENT_TIMEOUT_REMOVE, CATEGORY_MODERATION, "Timeout removed", KIND_ADD),
        EventSpec(EVENT_MESSAGE_EDIT, CATEGORY_MESSAGES, "Message edited", KIND_CHANGE),
        EventSpec(EVENT_MESSAGE_DELETE, CATEGORY_MESSAGES, "Message deleted", KIND_REMOVE),
        EventSpec(EVENT_BULK_DELETE, CATEGORY_MESSAGES, "Messages bulk deleted", KIND_REMOVE),
        EventSpec(EVENT_ROLE_CREATE, CATEGORY_SERVER, "Role created", KIND_ADD),
        EventSpec(EVENT_ROLE_DELETE, CATEGORY_SERVER, "Role deleted", KIND_REMOVE),
        EventSpec(EVENT_ROLE_UPDATE, CATEGORY_SERVER, "Role changed", KIND_CHANGE),
        EventSpec(EVENT_CHANNEL_CREATE, CATEGORY_SERVER, "Channel created", KIND_ADD),
        EventSpec(EVENT_CHANNEL_DELETE, CATEGORY_SERVER, "Channel deleted", KIND_REMOVE),
        EventSpec(EVENT_CHANNEL_UPDATE, CATEGORY_SERVER, "Channel changed", KIND_CHANGE),
        EventSpec(EVENT_SERVER_UPDATE, CATEGORY_SERVER, "Server settings changed", KIND_CHANGE),
        EventSpec(EVENT_EMOJI, CATEGORY_SERVER, "Emojis changed", KIND_CHANGE),
        EventSpec(EVENT_STICKER, CATEGORY_SERVER, "Stickers changed", KIND_CHANGE),
        EventSpec(EVENT_WEBHOOK, CATEGORY_SERVER, "Webhooks changed", KIND_CHANGE),
        EventSpec(EVENT_VOICE_JOIN, CATEGORY_VOICE, "Joined voice", KIND_ADD, default_on=False),
        EventSpec(EVENT_VOICE_LEAVE, CATEGORY_VOICE, "Left voice", KIND_REMOVE, default_on=False),
        EventSpec(EVENT_VOICE_MOVE, CATEGORY_VOICE, "Moved voice channel", KIND_CHANGE, default_on=False),
    )
}

# Ignore lists (users, roles, bots) only apply to these categories. Moderation and
# server events are always recorded: otherwise a moderator could hide their own
# bans or permission changes by putting themself on the ignore list.
IGNORABLE_CATEGORIES = (CATEGORY_MEMBERS, CATEGORY_MESSAGES, CATEGORY_VOICE)

# What the "who did it" line says when the audit log gave no answer.
ACTOR_UNKNOWN = "unknown"
DELETED_BY_UNKNOWN = "author or unknown"  # a member deleting their own message leaves no audit entry
MISSING_AUDIT_SUFFIX = " (missing View Audit Log)"

# What kind of thing an event is about. Decides how its ID is shown as a mention.
TARGET_USER = "user"
TARGET_ROLE = "role"
TARGET_CHANNEL = "channel"
TARGET_OTHER = "other"  # server, emoji, webhook: no mention, just the name

# GuildIssue.key for each problem the module reports to a server's admins.
ISSUE_PREFIX = "modlog."
ISSUE_VIEW_AUDIT_LOG = "modlog.view_audit_log"
ISSUE_CHANNEL_PREFIX = "modlog.channel."  # + category


def channel_issue_key(category: str) -> str:
    """The dashboard issue key for a category's log channel being unusable."""
    return ISSUE_CHANNEL_PREFIX + category
