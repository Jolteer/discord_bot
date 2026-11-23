"""Decides whether an event is recorded at all. One pure function, no Discord and no database."""

from __future__ import annotations

from .constants import CATEGORY_MESSAGES, EVENTS, IGNORABLE_CATEGORIES
from .events import Facts
from .settings import ModLogSettings


def should_log(enabled: bool, settings: ModLogSettings, facts: Facts) -> bool:
    """Whether this server records the event described by ``facts``.

    Checked in order:

    1. Logging is switched on for the server and this event's toggle is on.
    2. The event's category has a log channel. Without one nothing is recorded,
       not even in the history.
    3. Moderation and server events stop here and are always recorded: the
       ignore lists must not be a way to hide a ban or a permission change.
    4. Bots (if the server ignores them), ignored members and ignored roles.
    5. For message events: ignored channels, and the log channels themselves.
       A log post about something that happened in a log channel would be an
       event in a log channel again, which would loop.

    Args:
        enabled: The module's on/off switch for this server.
        settings: The server's mod log settings.
        facts: What is known about the event.

    Returns:
        True if the event should be saved and posted.
    """
    spec = EVENTS.get(facts.event)
    if spec is None or not enabled or not settings.event_enabled(facts.event):
        return False
    if settings.channel_for(spec.category) is None:
        return False
    if spec.category not in IGNORABLE_CATEGORIES:
        return True
    if facts.user_is_bot and settings.ignore_bots:
        return False
    if facts.user_id is not None and facts.user_id in settings.ignored_user_ids:
        return False
    if facts.user_role_ids & set(settings.ignored_role_ids):
        return False
    if spec.category == CATEGORY_MESSAGES:
        blocked = set(settings.ignored_channel_ids) | settings.log_channel_ids
        if blocked.intersection(facts.channel_ids):
            return False
    return True
