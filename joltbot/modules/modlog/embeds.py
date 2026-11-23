"""Turns a ``LogEvent`` into the embed posted in a log channel.

Discord rejects an embed if any part is too long, or if all its text together
passes 6000 characters, so every piece is cut to size here and the whole embed
is checked once more at the end.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import discord

from ...core.limits import EMBED_FIELD_NAME_MAX, EMBED_FIELD_VALUE_MAX, EMBED_TITLE_MAX
from ..tickets.embeds import clip, ts
from .constants import (
    EVENT_BULK_DELETE,
    EVENT_MESSAGE_DELETE,
    EVENT_MESSAGE_EDIT,
    EVENTS,
    KIND_ADD,
    KIND_CHANGE,
    KIND_PUNISH,
    KIND_REMOVE,
    KIND_WARN,
    TARGET_CHANNEL,
    TARGET_ROLE,
    TARGET_USER,
)
from .events import LogEvent

COLOURS = {
    KIND_ADD: discord.Colour.green(),
    KIND_REMOVE: discord.Colour.red(),
    KIND_CHANGE: discord.Colour.blue(),
    KIND_WARN: discord.Colour.orange(),
    KIND_PUNISH: discord.Colour.dark_red(),
}

EMBED_TOTAL_BUDGET = 5800  # Discord's limit is 6000 for all text in one embed; this leaves room for the footer
CONTENT_SHOWN_MAX = 2000  # of a deleted message's text
EDIT_SIDE_MAX = 1000  # of each of "before" and "after" for an edit
BULK_LINE_MAX = 150  # of each message listed for a bulk delete
BULK_SHOWN_MAX = 2500  # of the whole list
NAME_SHOWN_MAX = 100  # of a member, role or channel name
MAX_CHANGE_FIELDS = 10
MAX_INFO_FIELDS = 6
SHORTENED_FOOTER = "Shortened to fit. The full entry is in the log history."
NOT_IN_MEMORY = "*Not available: the message was sent before JoltBot started keeping messages in memory.*"

TARGET_LABELS = {TARGET_USER: "Member", TARGET_ROLE: "Role", TARGET_CHANNEL: "Channel"}
MENTIONS = {TARGET_USER: "<@{}>", TARGET_ROLE: "<@&{}>", TARGET_CHANNEL: "<#{}>"}


def describe(kind: str, item_id: int | None, name: str | None) -> str:
    """A member, role or channel as a mention plus its name and raw ID.

    The mention is the clickable part. The name and ID are there because a
    mention of someone who has left the server shows as "unknown user".

    Args:
        kind: One of the ``TARGET_`` constants.
        item_id: Its Discord ID, if it has one.
        name: Its name at the time of the event, if known.

    Returns:
        e.g. ``<@123> · alice · `123```.
    """
    parts = []
    if item_id is not None and kind in MENTIONS:
        parts.append(MENTIONS[kind].format(item_id))
    if name:
        parts.append(discord.utils.escape_markdown(clip(name, NAME_SHOWN_MAX)))
    if item_id is not None:
        parts.append(f"`{item_id}`")
    return clip(" · ".join(parts))


def _change_text(change: dict[str, str]) -> str:
    """One old -> new row as a field value. A row without an old value shows only the new one."""
    old, new = change.get("old", ""), change.get("new", "")
    if not old:
        return clip(new)
    half = (EMBED_FIELD_VALUE_MAX - 3) // 2
    return f"{clip(old, half)} → {clip(new, half)}"


def _info_text(row: dict[str, str]) -> str:
    """One extra fact as a field value. A stored time becomes a Discord timestamp tag."""
    if "time" in row:
        when = datetime.fromisoformat(row["time"])
        return f"{ts(when, 'F')} ({ts(when, 'R')})"
    return clip(row.get("value"))


def _attachment_lines(attachments: list[dict[str, str]]) -> str:
    """Attachments as one link per line."""
    return clip("\n".join(f"[{a['filename']}]({a['url']})" for a in attachments))


def _bulk_lines(messages: list[dict[str, Any]]) -> str:
    """The cached messages of a bulk delete, one per line, oldest first."""
    lines = []
    for message in messages:
        text = clip(message.get("content") or "*no text*", BULK_LINE_MAX).replace("\n", " ")
        lines.append(f"**{discord.utils.escape_markdown(message.get('author') or 'unknown')}**: {text}")
    return clip("\n".join(lines), BULK_SHOWN_MAX)


def _message_text(event: LogEvent) -> str | None:
    """The embed description for message events; ``None`` for everything else."""
    extra = event.extra
    if event.event == EVENT_MESSAGE_DELETE:
        if not extra.get("cached", True):
            return NOT_IN_MEMORY
        return clip(extra.get("content") or "*No text.*", CONTENT_SHOWN_MAX)
    if event.event == EVENT_MESSAGE_EDIT and extra.get("jump_url"):
        return f"[Jump to message]({extra['jump_url']})"
    if event.event == EVENT_BULK_DELETE:
        count, messages = extra.get("count", 0), extra.get("messages") or []
        head = f"**{count}** message{'s' if count != 1 else ''} deleted."
        if not messages:
            return head + " None of them were in memory, so their text isn't available."
        return f"{head} The {len(messages)} oldest still in memory:\n{_bulk_lines(messages)}"
    return None


def build_embed(event: LogEvent) -> discord.Embed:
    """Build the log post for one event.

    Layout: a title and colour from the event type, the thing it happened to,
    the channel, who did it, the reason, then extra facts and old -> new rows.

    Args:
        event: The event to show.

    Returns:
        An embed that fits Discord's size limits.
    """
    spec = EVENTS[event.event]
    extra = event.extra
    embed = discord.Embed(
        title=(event.title or spec.title)[:EMBED_TITLE_MAX],
        description=_message_text(event),
        colour=COLOURS.get(spec.kind, discord.Colour.light_grey()),
        timestamp=event.created_at,
    )

    if thumbnail := extra.get("thumbnail_url"):
        embed.set_thumbnail(url=thumbnail)

    def add(name: str, value: str, *, inline: bool = False) -> None:
        embed.add_field(name=name[:EMBED_FIELD_NAME_MAX], value=value, inline=inline)

    if event.target_id is not None or event.target_name:
        label = extra.get("target_label") or TARGET_LABELS.get(event.target_kind, "Name")
        add(label, describe(event.target_kind, event.target_id, event.target_name))
    if event.channel_id is not None:
        add("Channel", describe(TARGET_CHANNEL, event.channel_id, extra.get("channel_name")))
    if event.actor_id is not None:
        add(extra.get("actor_label", "By"), describe(TARGET_USER, event.actor_id, event.actor_name))
    elif event.actor_note:
        add(extra.get("actor_label", "By"), event.actor_note)
    if event.reason:
        add("Reason", clip(event.reason))

    if event.event == EVENT_MESSAGE_EDIT:
        before = extra.get("before")
        add("Before", clip(before or "*No text.*", EDIT_SIDE_MAX) if before is not None else NOT_IN_MEMORY)
        add("After", clip(extra.get("after") or "*No text.*", EDIT_SIDE_MAX))
    if attachments := extra.get("attachments"):
        add("Attachments", _attachment_lines(attachments))

    for row in event.info[:MAX_INFO_FIELDS]:
        add(row["name"], _info_text(row), inline=True)
    for change in event.changes[:MAX_CHANGE_FIELDS]:
        add(change["field"], _change_text(change))
    if (hidden := len(event.changes) - MAX_CHANGE_FIELDS) > 0:
        add("More changes", f"+{hidden} more. The full entry is in the log history.")

    # Last line of defence: drop fields from the end until everything fits.
    shortened = False
    while len(embed) > EMBED_TOTAL_BUDGET and embed.fields:
        embed.remove_field(len(embed.fields) - 1)
        shortened = True
    if shortened:
        embed.set_footer(text=SHORTENED_FOOTER)
    return embed
