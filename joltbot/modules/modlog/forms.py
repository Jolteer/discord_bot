"""Turns the dashboard's Logging form into settings, and checks them against the
server's real roles and channels.

No FastAPI or HTML here: these are plain functions, so a future JSON API can
reuse them unchanged.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from ...core.discord_rest import GuildData
from ...core.permissions import ADMINISTRATOR, BOT_PERMISSIONS
from ...web.form_helpers import form_ids, form_text
from ...web.options import role_option, text_channel_options
from .constants import (
    CATEGORIES,
    CATEGORY_MEMBERS,
    CATEGORY_MESSAGES,
    CATEGORY_MODERATION,
    CATEGORY_SERVER,
    CATEGORY_TITLES,
    CATEGORY_VOICE,
    EVENTS,
)
from .settings import MAX_IGNORED, ModLogSettings

EVENT_FIELD_PREFIX = "ev_"  # each event's tick box is named ev_<event key>

# What each log channel receives, shown under its dropdown.
CHANNEL_HELP = {
    CATEGORY_MEMBERS: "Joins, leaves, nicknames and member roles.",
    CATEGORY_MESSAGES: "Edited and deleted messages, with their text.",
    CATEGORY_MODERATION: "Bans, unbans, kicks and timeouts.",
    CATEGORY_SERVER: "Role, channel, server, emoji, sticker and webhook changes.",
    CATEGORY_VOICE: "Voice joins, leaves and moves. Leave empty to post them in the Members channel.",
}
FIELD_LABELS = {
    **{f"{category}_channel_id": f"{CATEGORY_TITLES[category]} channel" for category in CATEGORIES},
    "ignored_channel_ids": "Ignored channels",
    "ignored_role_ids": "Ignored roles",
    "ignored_user_ids": "Ignored members",
}
# Server-wide permissions the mod log needs to post, as (bit, name). Bits come from the invite list.
POST_PERMISSIONS = [
    (BOT_PERMISSIONS[name], name) for name in ("View Channels", "Send Messages", "Embed Links", "Read Message History")
]
VIEW_AUDIT_LOG = BOT_PERMISSIONS["View Audit Log"]


def _user_ids(text: str) -> list[str]:
    """The IDs typed into the "ignored members" box: one per line, or separated by spaces or commas."""
    return list(dict.fromkeys(part for part in re.split(r"[\s,;]+", text) if part))


def parse_form(form: Any) -> dict[str, Any]:
    """Map HTML form fields to a dict shaped like ``ModLogSettings`` (IDs as strings).

    Each event has a tick box named ``ev_<event key>``. A box that is not
    ticked is not sent by the browser at all, hence ``name in form``. Only the
    events switched away from their default are kept, so a server that never
    touched an event follows its default, also if that default changes later.

    Settings this page doesn't show (``retention_days``) are not in the result:
    the route keeps their saved values.

    Args:
        form: Starlette form data (supports ``get`` / ``getlist``).

    Returns:
        Unvalidated settings dict ready for ``ModLogSettings.model_validate``.
    """
    events = {}
    for key, spec in EVENTS.items():
        ticked = EVENT_FIELD_PREFIX + key in form
        if ticked != spec.default_on:
            events[key] = ticked
    return {
        **{f"{category}_channel_id": form_text(form, f"{category}_channel_id") or None for category in CATEGORIES},
        "events": events,
        "ignore_bots": "ignore_bots" in form,
        "ignored_role_ids": form_ids(form, "ignored_role_ids"),
        "ignored_channel_ids": form_ids(form, "ignored_channel_ids"),
        "ignored_user_ids": _user_ids(form_text(form, "ignored_user_ids")),
    }


def validation_errors(exc: ValidationError) -> list[str]:
    """Turn Pydantic errors into short sentences an admin can act on.

    Args:
        exc: Validation failure from ``ModLogSettings.model_validate``.

    Returns:
        Human-readable error lines for the settings form, without repeats.
    """
    errors = []
    for err in exc.errors():
        loc = [str(part) for part in err["loc"]]
        label = FIELD_LABELS.get(loc[0], loc[0]) if loc else "Settings"
        if err["type"] == "too_long":
            errors.append(f"{label}: at most {MAX_IGNORED} entries.")
        elif loc[:1] == ["ignored_user_ids"]:
            errors.append(
                f"{label}: every entry must be a member's user ID, a long number. In Discord, switch on "
                "Developer Mode, then right-click the member and choose Copy User ID."
            )
        else:
            errors.append(f"{label}: {err['msg'].removeprefix('Value error, ')}")
    return list(dict.fromkeys(errors))


def foreign_id_errors(settings: ModLogSettings, data: GuildData) -> list[str]:
    """Ensure every channel and role ID belongs to this server.

    Without this check an admin could paste a channel ID from another server
    into the form, and JoltBot would post this server's deleted messages there.

    Args:
        settings: Candidate mod log settings from the form.
        data: Live roles and channels for this server.

    Returns:
        Error lines naming what is foreign or missing.
    """
    not_here = "isn't in this server. Refresh the page and pick again."
    text_ids = {c.id for c in data.text_channels()}
    errors = []
    for category in CATEGORIES:
        channel_id = getattr(settings, f"{category}_channel_id")
        if channel_id is not None and channel_id not in text_ids:
            errors.append(f"{CATEGORY_TITLES[category]} channel: that channel {not_here}")
    if any(channel_id not in {c.id for c in data.channels} for channel_id in settings.ignored_channel_ids):
        errors.append(f"Ignored channels: a selected channel {not_here}")
    if any(role_id not in data.role_ids for role_id in settings.ignored_role_ids):
        errors.append(f"Ignored roles: a selected role {not_here}")
    return errors


def setup_warnings(settings: ModLogSettings, data: GuildData) -> list[str]:
    """Soft problems visible from the dashboard (permissions, deleted channels and roles).

    Args:
        settings: Current mod log settings.
        data: Live Discord guild snapshot.

    Returns:
        Warning lines shown above the form (non-blocking).
    """
    warnings = []
    perms = data.bot_permissions()
    if not perms & ADMINISTRATOR:
        if missing := [name for bit, name in POST_PERMISSIONS if not perms & bit]:
            warnings.append(
                f"JoltBot's role is missing these permissions: {', '.join(missing)}. "
                "Add them in Server Settings → Roles."
            )
        if not perms & VIEW_AUDIT_LOG:
            warnings.append(
                "JoltBot's role is missing View Audit Log, so entries can't say who did something. Events are "
                "still logged, with the actor shown as unknown. Add it in Server Settings → Roles."
            )

    channel_ids = {c.id for c in data.channels}
    for category in CATEGORIES:
        channel_id = getattr(settings, f"{category}_channel_id")
        if channel_id is not None and channel_id not in channel_ids:
            warnings.append(f"The {CATEGORY_TITLES[category]} channel you picked was deleted. Pick a new one.")
    if any(channel_id not in channel_ids for channel_id in settings.ignored_channel_ids):
        warnings.append("One of the ignored channels was deleted. Review them below and save.")
    if any(role_id not in data.role_ids for role_id in settings.ignored_role_ids):
        warnings.append("One of the ignored roles was deleted. Review them below and save.")
    return warnings


def form_options(data: GuildData) -> dict[str, Any]:
    """Roles and channels in the shape the template needs (IDs as strings).

    Args:
        data: Live roles and channels for this server.

    Returns:
        ``roles`` and ``text_channels`` for the shared form macros, and
        ``ignorable_channels``: every category, then every text channel, each
        as ``{"id", "label"}``.
    """
    categories = [{"id": str(c.id), "label": f"Category: {c.name}"} for c in data.categories()]
    channels = text_channel_options(data)
    return {
        "roles": [role_option(role) for role in data.roles],
        "text_channels": channels,
        "ignorable_channels": categories + [{"id": c["id"], "label": f"#{c['name']}"} for c in channels],
    }


def event_groups(values: dict[str, Any]) -> list[dict[str, Any]]:
    """The page's five blocks: each category's channel, and a tick box per event in it.

    Args:
        values: Settings values (saved, or as typed when a save failed).

    Returns:
        One block per category, in display order:
        ``{"category", "title", "field", "help", "channel_id", "posts", "events"}``.
        ``posts`` is False when the category has no channel to post in, so
        nothing in it is recorded. ``events`` holds ``{"key", "title", "on"}``.
    """
    toggles = values.get("events") or {}
    members_channel = values.get(f"{CATEGORY_MEMBERS}_channel_id")
    groups = []
    for category in CATEGORIES:
        field = f"{category}_channel_id"
        channel_id = values.get(field) or ""
        groups.append(
            {
                "category": category,
                "title": CATEGORY_TITLES[category],
                "field": field,
                "help": CHANNEL_HELP[category],
                "channel_id": channel_id,
                # Voice events fall back to the members channel (see ModLogSettings.channel_for).
                "posts": bool(channel_id or (category == CATEGORY_VOICE and members_channel)),
                "events": [
                    {"key": spec.key, "title": spec.title, "on": bool(toggles.get(spec.key, spec.default_on))}
                    for spec in EVENTS.values()
                    if spec.category == category
                ],
            }
        )
    return groups
