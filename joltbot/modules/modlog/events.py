"""The mod log's own description of one thing that happened.

Listeners turn discord.py objects into a ``LogEvent`` (plain data). Everything
after that (the ignore rules, the database row, the embed, the dashboard) works
from the ``LogEvent`` alone, which is what makes those parts easy to test.
This file must not import discord.py: the dashboard reads the same shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ...core.types import utcnow
from .constants import EVENTS, TARGET_USER

STORED_CONTENT_MAX = 4000  # characters of one message's text kept in the database (Discord's own maximum)
STORED_BULK_MESSAGES = 20  # messages listed for one bulk delete
STORED_BULK_CONTENT_MAX = 500  # characters kept of each of those
STORED_NAME_MAX = 100
STORED_VALUE_MAX = 1000  # one side of an old -> new change


def cut(text: str | None, limit: int) -> str:
    """Shorten text for storage. ``None`` becomes an empty string."""
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(frozen=True)
class Facts:
    """What the ignore rules need to know about an event, before anything is built or stored.

    Attributes:
        event: Event key from ``constants.EVENTS``.
        user_id: The member the event is about (message author, the member who joined ...).
        user_is_bot: Whether that member is a bot.
        user_role_ids: That member's roles, when known.
        channel_ids: For message events: the channel it happened in, then its parents
            (a thread's channel, the category). Ignoring any of them ignores the event.
    """

    event: str
    user_id: int | None = None
    user_is_bot: bool = False
    user_role_ids: frozenset[int] = frozenset()
    channel_ids: tuple[int, ...] = ()

    @property
    def category(self) -> str:
        """The log category of this event."""
        return EVENTS[self.event].category


@dataclass
class LogEvent:
    """One recorded event: who, what, to whom, and the details.

    Attributes:
        guild_id: Server it happened in.
        event: Event key from ``constants.EVENTS``.
        target_id: What it happened to (a member, role, channel ...), if anything.
        target_name: That thing's name at the time, so the entry still reads well after it is gone.
        target_kind: One of the ``TARGET_`` constants; decides how the ID is shown.
        actor_id: Who did it, when the audit log said.
        actor_name: Their name at the time.
        actor_note: Shown in place of an actor when nobody could be named,
            e.g. ``unknown (missing View Audit Log)``.
        channel_id: Channel it happened in, for message and voice events.
        reason: The reason typed in Discord, if any.
        title: Overrides the event's standard title (e.g. "Webhook created").
        changes: ``{"field", "old", "new"}`` rows for old -> new values.
        info: ``{"name", "value"}`` rows of extra facts (account age, duration ...).
        extra: Anything else, JSON-safe: message text, attachments, a jump link.
        created_at: When it happened.
    """

    guild_id: int
    event: str
    target_id: int | None = None
    target_name: str | None = None
    target_kind: str = TARGET_USER
    actor_id: int | None = None
    actor_name: str | None = None
    actor_note: str | None = None
    channel_id: int | None = None
    reason: str | None = None
    title: str | None = None
    changes: list[dict[str, str]] = field(default_factory=list)
    info: list[dict[str, str]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)

    @property
    def category(self) -> str:
        """The log category of this event."""
        return EVENTS[self.event].category

    def add_change(self, name: str, old: Any, new: Any) -> None:
        """Add one old -> new row, shortened for storage."""
        self.changes.append(
            {"field": name, "old": cut(str(old), STORED_VALUE_MAX), "new": cut(str(new), STORED_VALUE_MAX)}
        )

    def add_info(self, name: str, value: Any) -> None:
        """Add one extra fact, shortened for storage."""
        self.info.append({"name": name, "value": cut(str(value), STORED_VALUE_MAX)})

    def add_time(self, name: str, when: datetime | None) -> None:
        """Add a point in time as an extra fact.

        Stored as ISO text under ``time`` instead of ``value``, so Discord can
        show it in each reader's own time zone and the dashboard as a date.
        """
        if when is not None:
            self.info.append({"name": name, "time": when.isoformat()})

    def details(self) -> dict[str, Any]:
        """Everything that has no column of its own, as the JSON saved with the entry.

        Returns:
            A JSON-safe dict. Discord IDs inside it are strings (see ``core/types.py``).
        """
        data: dict[str, Any] = {"target_kind": self.target_kind, **self.extra}
        for key in ("target_name", "actor_name", "actor_note", "title"):
            if (value := getattr(self, key)) is not None:
                data[key] = cut(value, STORED_NAME_MAX)
        if self.changes:
            data["changes"] = self.changes
        if self.info:
            data["info"] = self.info
        return data
