"""Matches a gateway event ("this changed") to the audit log entry that says who did it.

Discord tells the bot about a change twice: once as a gateway event (what
changed, but not who) and once as an audit log entry (who, but slightly earlier
or later). ``RecentAudit`` keeps the last few seconds of audit entries in
memory so a gateway handler can look up its actor.

The rule throughout is *never guess*. A name is only returned when exactly one
person could have done it:

* If entries from two different people both fit, nobody is named.
* Discord merges repeated message deletes, voice moves and voice disconnects by
  one moderator into a single entry with a ``count``, and those entries don't
  say which message or member. So the handlers also report what they observed
  (``observe``), and a name is only returned while the audit log accounts for
  every observed event. One delete by a moderator plus one self-delete at the
  same moment looks the same as two self-deletes plus an unrelated entry, so
  neither gets a name.

Nothing here touches Discord or the database; the clock is passed in so tests
can move time by hand.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Collection
from dataclasses import dataclass

AUDIT_TTL_SECONDS = 30.0  # how long an audit entry or an observation stays in memory
LOOKBACK_SECONDS = 5.0  # an audit entry may arrive this long before its gateway event

# Audit log action names (discord.AuditLogAction.<name>.name), kept as plain strings
# so this file needs no discord.py import.
ACTION_MESSAGE_DELETE = "message_delete"
ACTION_MEMBER_MOVE = "member_move"
ACTION_MEMBER_DISCONNECT = "member_disconnect"

# Kinds of gateway events the handlers report with ``observe``.
SEEN_MESSAGE_DELETE = "message_delete"
SEEN_VOICE_MOVE = "voice_move"
SEEN_VOICE_LEAVE = "voice_leave"
SEEN_BAN = "ban"


@dataclass(frozen=True)
class AuditRecord:
    """The parts of one audit log entry the mod log uses.

    Attributes:
        guild_id: Server the entry belongs to.
        action: ``discord.AuditLogAction`` name, e.g. ``"member_update"``.
        actor_id: Who did it.
        target_id: What it was done to (member, role, channel ...), when the entry says.
        channel_id: Channel named by the entry (message deletes, voice moves, bulk deletes).
        count: How many events the entry stands for (1 unless Discord merged several).
        keys: Names of the attributes the entry changed, e.g. ``{"nick"}``.
        reason: The reason typed in Discord, if any.
    """

    guild_id: int
    action: str
    actor_id: int | None
    target_id: int | None = None
    channel_id: int | None = None
    count: int = 1
    keys: frozenset[str] = frozenset()
    reason: str | None = None


@dataclass(frozen=True)
class _Seen:
    """One gateway event a handler observed, for the counting described in the module docstring."""

    kind: str
    guild_id: int
    channel_id: int | None
    user_id: int | None


class RecentAudit:
    """The last few seconds of audit entries and observed events, for one bot process."""

    def __init__(self, *, ttl: float = AUDIT_TTL_SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        """Start empty.

        Args:
            ttl: Seconds an entry is remembered.
            clock: Returns the current time in seconds; replaced in tests.
        """
        self._ttl = ttl
        self._clock = clock
        self._records: deque[tuple[float, AuditRecord]] = deque()
        self._seen: deque[tuple[float, _Seen]] = deque()

    def now(self) -> float:
        """The current time on this buffer's clock."""
        return self._clock()

    def _forget_old(self) -> None:
        """Drop everything older than the time-to-live."""
        oldest = self._clock() - self._ttl
        for items in (self._records, self._seen):
            while items and items[0][0] < oldest:
                items.popleft()

    def add(self, record: AuditRecord) -> None:
        """Remember an audit entry that just arrived."""
        self._forget_old()
        self._records.append((self._clock(), record))

    def observe(self, kind: str, guild_id: int, *, channel_id: int | None = None, user_id: int | None = None) -> None:
        """Note that a gateway event of this kind just happened.

        Args:
            kind: One of the ``SEEN_`` constants.
            guild_id: Server it happened in.
            channel_id: Channel it happened in, where that matters.
            user_id: Member it happened to, when known.
        """
        self._forget_old()
        self._seen.append((self._clock(), _Seen(kind, guild_id, channel_id, user_id)))

    def was_seen(self, kind: str, guild_id: int, *, user_id: int, since: float) -> bool:
        """Whether an event of this kind was observed for this member since ``since``."""
        return any(
            at >= since and seen.kind == kind and seen.guild_id == guild_id and seen.user_id == user_id
            for at, seen in self._seen
        )

    def _matching(
        self,
        guild_id: int,
        actions: Collection[str],
        since: float,
        *,
        target_id: int | None = None,
        channel_id: int | None = None,
        key: str | None = None,
    ) -> list[AuditRecord]:
        """Entries since ``since`` that fit every filter given, oldest first."""
        return [
            record
            for at, record in self._records
            if at >= since
            and record.guild_id == guild_id
            and record.action in actions
            and (target_id is None or record.target_id == target_id)
            and (channel_id is None or record.channel_id == channel_id)
            and (key is None or key in record.keys)
        ]

    def find(
        self,
        guild_id: int,
        actions: Collection[str],
        *,
        since: float,
        target_id: int | None = None,
        channel_id: int | None = None,
        key: str | None = None,
    ) -> AuditRecord | None:
        """The audit entry that explains a gateway event, if exactly one person fits.

        Args:
            guild_id: Server the event happened in.
            actions: Audit action names that could explain it.
            since: Ignore entries that arrived before this time.
            target_id: Only entries about this member, role or channel.
            channel_id: Only entries naming this channel.
            key: Only entries that changed this attribute (e.g. ``"nick"``).

        Returns:
            The newest fitting entry, or ``None`` if there is none or if entries
            from more than one person fit.
        """
        records = self._matching(guild_id, actions, since, target_id=target_id, channel_id=channel_id, key=key)
        if not records or len({r.actor_id for r in records}) != 1:
            return None
        return records[-1]

    def _count_seen(
        self, kind: str, guild_id: int, since: float, *, channel_id: int | None = None, user_id: int | None = None
    ) -> int:
        """How many events of a kind were observed. An event with an unknown member counts for any member."""
        return sum(
            1
            for at, seen in self._seen
            if at >= since
            and seen.kind == kind
            and seen.guild_id == guild_id
            and (channel_id is None or seen.channel_id == channel_id)
            and (user_id is None or seen.user_id is None or seen.user_id == user_id)
        )

    @staticmethod
    def _accounts_for(records: list[AuditRecord], seen: int) -> AuditRecord | None:
        """The entry to name, if one person's entries cover every observed event."""
        if not records or len({r.actor_id for r in records}) != 1:
            return None
        if seen > sum(r.count for r in records):
            return None
        return records[-1]

    def who_deleted(self, guild_id: int, channel_id: int, author_id: int | None, *, since: float) -> AuditRecord | None:
        """Who deleted a message, if the audit log leaves no doubt.

        Args:
            guild_id: Server the message was in.
            channel_id: Channel the message was in.
            author_id: The message's author, or ``None`` if the message wasn't cached.
            since: Ignore entries and observations before this time.

        Returns:
            A ``message_delete`` entry for this channel (and author, when known),
            or ``None``. With an unknown author the entry is only returned when it
            is the only delete seen in the channel; its ``target_id`` is then the author.
        """
        records = self._matching(guild_id, (ACTION_MESSAGE_DELETE,), since, target_id=author_id, channel_id=channel_id)
        seen = self._count_seen(SEEN_MESSAGE_DELETE, guild_id, since, channel_id=channel_id, user_id=author_id)
        if author_id is None and len(records) > 1:
            return None
        return self._accounts_for(records, seen)

    def who_moved(self, guild_id: int, channel_id: int, *, since: float) -> AuditRecord | None:
        """Who moved a member into a voice channel, if the audit log leaves no doubt.

        Args:
            guild_id: Server it happened in.
            channel_id: The voice channel the member ended up in.
            since: Ignore entries and observations before this time.

        Returns:
            A ``member_move`` entry, or ``None`` (the member most likely moved themself).
        """
        records = self._matching(guild_id, (ACTION_MEMBER_MOVE,), since, channel_id=channel_id)
        return self._accounts_for(records, self._count_seen(SEEN_VOICE_MOVE, guild_id, since, channel_id=channel_id))

    def who_disconnected(self, guild_id: int, *, since: float) -> AuditRecord | None:
        """Who disconnected a member from voice, if the audit log leaves no doubt.

        Args:
            guild_id: Server it happened in.
            since: Ignore entries and observations before this time.

        Returns:
            A ``member_disconnect`` entry, or ``None`` (the member most likely left by themself).
        """
        records = self._matching(guild_id, (ACTION_MEMBER_DISCONNECT,), since)
        return self._accounts_for(records, self._count_seen(SEEN_VOICE_LEAVE, guild_id, since))
