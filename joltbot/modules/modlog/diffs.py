"""Works out what changed between the "before" and "after" a gateway event hands over.

Every function returns rows of ``(field, old, new)`` text, ready for
``LogEvent.add_change``. An empty ``old`` means "there is no old value to
show" (something was added, or the row is a list of names).

The objects are only read by attribute, so tests can pass simple stand-ins.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import discord

Change = tuple[str, str, str]

_MISSING = object()

# (attribute, label) pairs compared for each kind of object. Position changes are
# left out on purpose: moving one role or channel shifts many others, and Discord
# sends an update for each.
ROLE_FIELDS = (("name", "Name"), ("colour", "Colour"), ("hoist", "Shown separately"), ("mentionable", "Mentionable"))
CHANNEL_FIELDS = (
    ("name", "Name"),
    ("topic", "Topic"),
    ("nsfw", "Age-restricted"),
    ("category", "Category"),
    ("bitrate", "Bitrate"),
    ("user_limit", "User limit"),
)
GUILD_FIELDS = (
    ("name", "Name"),
    ("description", "Description"),
    ("owner_id", "Owner ID"),
    ("verification_level", "Verification level"),
    ("explicit_content_filter", "Explicit content filter"),
    ("default_notifications", "Default notifications"),
    ("mfa_level", "2FA requirement for moderators"),
    ("afk_channel", "AFK channel"),
    ("afk_timeout", "AFK timeout (seconds)"),
    ("system_channel", "System messages channel"),
    ("rules_channel", "Rules channel"),
    ("public_updates_channel", "Community updates channel"),
    ("preferred_locale", "Language"),
    ("vanity_url_code", "Vanity URL"),
    ("premium_progress_bar_enabled", "Boost progress bar"),
)
GUILD_IMAGES = (("icon", "Icon"), ("banner", "Banner"), ("splash", "Invite background"))


def show(value: Any) -> str:
    """One value as short readable text: names for Discord objects, yes/no for flags, "none" for nothing."""
    if value is None or value == "":
        return "none"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, discord.Enum):
        return str(value.name).replace("_", " ")
    name = getattr(value, "name", None)
    return name if isinstance(name, str) else str(value)


def permission_name(name: str) -> str:
    """``manage_guild`` -> ``Manage Server``, the way Discord's own settings spell it."""
    return name.replace("_", " ").title().replace("Guild", "Server")


def field_changes(before: Any, after: Any, fields: Iterable[tuple[str, str]]) -> list[Change]:
    """Compare the named attributes of two objects.

    Args:
        before: The object as it was.
        after: The object as it is now.
        fields: ``(attribute, label)`` pairs to compare. An attribute the object
            doesn't have (a voice channel has no topic) is skipped.

    Returns:
        One row per attribute whose value changed.
    """
    rows: list[Change] = []
    for attribute, label in fields:
        old, new = getattr(before, attribute, _MISSING), getattr(after, attribute, _MISSING)
        if old is _MISSING or new is _MISSING or old == new:
            continue
        rows.append((label, show(old), show(new)))
    return rows


def permission_changes(before: discord.Permissions, after: discord.Permissions) -> list[Change]:
    """Which permissions a role gained and lost.

    Returns:
        Up to two rows: "Permissions granted" and "Permissions revoked", each a list of names.
    """
    granted = [permission_name(name) for name, on in after if on and not getattr(before, name)]
    revoked = [permission_name(name) for name, on in before if on and not getattr(after, name)]
    rows: list[Change] = []
    if granted:
        rows.append(("Permissions granted", "", ", ".join(granted)))
    if revoked:
        rows.append(("Permissions revoked", "", ", ".join(revoked)))
    return rows


def role_changes(before: Any, after: Any) -> list[Change]:
    """What changed on a role: name, colour, display flags and permissions."""
    rows = field_changes(before, after, ROLE_FIELDS)
    if before.permissions != after.permissions:
        rows += permission_changes(before.permissions, after.permissions)
    return rows


def _overwrite_label(target: Any) -> str:
    """Who a channel permission override is for, as text."""
    name = getattr(target, "name", None)
    if not isinstance(name, str):
        return f"ID {target.id}"
    if isinstance(target, discord.Role):
        return f"role @{name.lstrip('@')}"
    return f"member {name}"


def _overwrite_state(value: bool | None) -> str:
    """One permission inside an override: allowed, denied, or left to the roles."""
    return {True: "allow", False: "deny"}.get(value, "default")  # type: ignore[arg-type]


def _describe_overwrite(overwrite: discord.PermissionOverwrite) -> str:
    """A whole override as text, e.g. ``allow: Send Messages; deny: Attach Files``."""
    allowed = [permission_name(name) for name, value in overwrite if value is True]
    denied = [permission_name(name) for name, value in overwrite if value is False]
    parts = []
    if allowed:
        parts.append("allow: " + ", ".join(allowed))
    if denied:
        parts.append("deny: " + ", ".join(denied))
    return "; ".join(parts) or "no overrides"


def overwrite_changes(before: Mapping[Any, discord.PermissionOverwrite], after: Mapping[Any, Any]) -> list[Change]:
    """What changed in a channel's permission overrides.

    Args:
        before: ``channel.overwrites`` as it was: role or member -> override.
        after: The same, as it is now.

    Returns:
        One row per role or member whose override was added, removed or changed.
    """
    old = {target.id: (target, overwrite) for target, overwrite in before.items()}
    new = {target.id: (target, overwrite) for target, overwrite in after.items()}
    rows: list[Change] = []
    for target_id in sorted(old.keys() | new.keys()):
        if target_id not in old:
            target, overwrite = new[target_id]
            rows.append((f"Permissions added for {_overwrite_label(target)}", "", _describe_overwrite(overwrite)))
        elif target_id not in new:
            target, overwrite = old[target_id]
            rows.append(
                (f"Permissions removed for {_overwrite_label(target)}", _describe_overwrite(overwrite), "removed")
            )
        else:
            target, was = old[target_id]
            now = new[target_id][1]
            if was == now:
                continue
            was_values = dict(was)
            parts = [
                f"{permission_name(name)}: {_overwrite_state(was_values.get(name))} → {_overwrite_state(value)}"
                for name, value in now
                if was_values.get(name) != value
            ]
            rows.append((f"Permissions for {_overwrite_label(target)}", "", "; ".join(parts)))
    return rows


def slowmode(seconds: int | None) -> str:
    """A slowmode delay as text: ``off`` or ``30s``."""
    return f"{seconds}s" if seconds else "off"


def channel_changes(before: Any, after: Any) -> list[Change]:
    """What changed on a channel: name, topic, slowmode, other settings and permission overrides."""
    rows = field_changes(before, after, CHANNEL_FIELDS)
    old_delay, new_delay = getattr(before, "slowmode_delay", None), getattr(after, "slowmode_delay", None)
    if old_delay != new_delay:
        rows.append(("Slowmode", slowmode(old_delay), slowmode(new_delay)))
    old_overwrites, new_overwrites = getattr(before, "overwrites", None), getattr(after, "overwrites", None)
    if isinstance(old_overwrites, Mapping) and isinstance(new_overwrites, Mapping):
        rows += overwrite_changes(old_overwrites, new_overwrites)
    return rows


def guild_changes(before: Any, after: Any) -> list[Change]:
    """What changed in the server's own settings."""
    rows = field_changes(before, after, GUILD_FIELDS)
    for attribute, label in GUILD_IMAGES:
        if getattr(before, attribute, None) != getattr(after, attribute, None):
            rows.append((label, "", "changed"))
    return rows


def named_changes(before: Iterable[Any], after: Iterable[Any]) -> list[Change]:
    """What changed in a list of named things with IDs (the server's emojis or stickers).

    Returns:
        Rows for the names added, the names removed, and one per rename.
    """
    old = {item.id: item for item in before}
    new = {item.id: item for item in after}
    rows: list[Change] = []
    if added := [new[i].name for i in sorted(new.keys() - old.keys())]:
        rows.append(("Added", "", ", ".join(added)))
    if removed := [old[i].name for i in sorted(old.keys() - new.keys())]:
        rows.append(("Removed", "", ", ".join(removed)))
    for item_id in sorted(old.keys() & new.keys()):
        if old[item_id].name != new[item_id].name:
            rows.append(("Renamed", old[item_id].name, new[item_id].name))
    return rows


def human_duration(seconds: float) -> str:
    """A length of time in its two largest units, e.g. ``1 day 2 hours`` or ``10 minutes``."""
    remaining = max(0, round(seconds / 60)) * 60  # timeouts are set in whole minutes; drop the lost second
    if remaining < 60:
        return "under a minute"
    parts = []
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        amount, remaining = divmod(remaining, size)
        if amount:
            parts.append(f"{amount} {unit}{'s' if amount != 1 else ''}")
    return " ".join(parts[:2])
