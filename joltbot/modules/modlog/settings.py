"""Per-server mod log settings: where each category is posted, what is recorded, what is ignored."""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from ...core.types import OptionalSnowflake, Snowflake
from ..base import ModuleSettingsModel
from .constants import CATEGORIES, CATEGORY_VOICE, EVENTS

MAX_IGNORED = 50  # entries in each ignore list
DEFAULT_RETENTION_DAYS = 30
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 365


class ModLogSettings(ModuleSettingsModel):
    """What one server's mod log records and where it posts.

    Attributes:
        members_channel_id: Log channel for joins, leaves, nicknames and member roles.
        messages_channel_id: Log channel for edited and deleted messages.
        moderation_channel_id: Log channel for bans, kicks and timeouts.
        server_channel_id: Log channel for role, channel, server, emoji and webhook changes.
        voice_channel_id: Log channel for voice events. Empty means the members channel.
        events: On/off per event key, only for events switched away from their default.
        ignored_channel_ids: Channels whose message events are not recorded.
        ignored_user_ids: Members whose own events are not recorded.
        ignored_role_ids: Roles whose holders' own events are not recorded.
        ignore_bots: Skip events about bots (their messages, voice moves, nicknames).
        retention_days: How long recorded entries are kept before the daily cleanup deletes them.
    """

    members_channel_id: OptionalSnowflake = None
    messages_channel_id: OptionalSnowflake = None
    moderation_channel_id: OptionalSnowflake = None
    server_channel_id: OptionalSnowflake = None
    voice_channel_id: OptionalSnowflake = None
    events: dict[str, bool] = Field(default_factory=dict)
    ignored_channel_ids: list[Snowflake] = Field(default_factory=list, max_length=MAX_IGNORED)
    ignored_user_ids: list[Snowflake] = Field(default_factory=list, max_length=MAX_IGNORED)
    ignored_role_ids: list[Snowflake] = Field(default_factory=list, max_length=MAX_IGNORED)
    ignore_bots: bool = True
    retention_days: int = Field(DEFAULT_RETENTION_DAYS, ge=MIN_RETENTION_DAYS, le=MAX_RETENTION_DAYS)

    @field_validator("events", mode="before")
    @classmethod
    def _known_events_only(cls, value: Any) -> Any:
        """Drop toggles for events this version of the code doesn't know, like unknown settings keys."""
        if isinstance(value, dict):
            return {key: on for key, on in value.items() if key in EVENTS}
        return value

    def event_enabled(self, event: str) -> bool:
        """Whether this server records the event: its own toggle, or the event's default.

        Args:
            event: An event key from ``constants.EVENTS``.

        Returns:
            False for an unknown key.
        """
        spec = EVENTS.get(event)
        if spec is None:
            return False
        return self.events.get(event, spec.default_on)

    def channel_for(self, category: str) -> int | None:
        """The log channel a category posts in, or ``None`` if the server set none.

        Voice events use the members channel when no voice channel is set.

        Args:
            category: One of ``constants.CATEGORIES``.

        Returns:
            Channel ID, or ``None``: then nothing in that category is recorded at all.
        """
        if category not in CATEGORIES:
            return None
        channel_id = getattr(self, f"{category}_channel_id")
        if channel_id is None and category == CATEGORY_VOICE:
            return self.members_channel_id
        return channel_id

    @property
    def log_channel_ids(self) -> set[int]:
        """Every channel the mod log posts in. Messages there are never recorded (that would loop)."""
        return {channel_id for category in CATEGORIES if (channel_id := self.channel_for(category)) is not None}

    def setup_problems(self) -> list[str]:
        """Logging can't be switched on until at least one category has a channel.

        Returns:
            Blocking problem strings; empty means the module is ready to enable.
        """
        if not self.log_channel_ids:
            return ["Pick at least one log channel."]
        return []
