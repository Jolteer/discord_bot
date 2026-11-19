"""Database tables (SQLAlchemy 2.0 style).

Every table except web_sessions and bot_meta is keyed by guild_id, and every
guild-owned row is deleted automatically (ON DELETE CASCADE) when its guild row
is purged 30 days after JoltBot leaves a server.
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    text,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .types import utcnow

# Discord IDs need 64 bits (BIGINT). But SQLite only auto-numbers a primary key
# declared exactly as "INTEGER", so our own auto-increment IDs use INTEGER there.
AutoId = BigInteger().with_variant(Integer(), "sqlite")


class UTCDateTime(TypeDecorator):
    """Always hands back timezone-aware UTC datetimes.

    SQLite has no real datetime type and drops timezone info, so we store naive
    UTC and re-attach UTC on the way out. Postgres gets a proper timestamptz.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """On the way in: require a timezone, convert to UTC, and drop the timezone for SQLite."""
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Use timezone-aware datetimes (joltbot.core.types.utcnow())")
        value = value.astimezone(timezone.utc)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """On the way out: always hand back a timezone-aware UTC datetime."""
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


# Predictable constraint names make future Alembic migrations (especially on SQLite) much easier.
NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class of every table. Sets the constraint naming convention."""

    metadata = MetaData(naming_convention=NAMING)


def _guild_fk() -> Any:
    """Foreign key to the owning guild. Such rows are deleted together with their guild."""
    return ForeignKey("guilds.id", ondelete="CASCADE")


class Guild(Base):
    """One row per Discord server JoltBot has ever been in (until purged)."""

    __tablename__ = "guilds"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(100), default="")
    icon: Mapped[str | None] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    joined_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # Set when JoltBot is removed; the guild and all its data are purged 30 days later.
    left_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class ModuleSetting(Base):
    """One row per (guild, module). ``config`` is validated by the module's
    Pydantic settings model (e.g. modules/tickets/settings.py)."""

    __tablename__ = "module_settings"

    guild_id: Mapped[int] = mapped_column(BigInteger, _guild_fk(), primary_key=True)
    module: Mapped[str] = mapped_column(String(32), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Bumped on every save. A save that names an old version is rejected, so two
    # admins editing at once can't silently overwrite each other.
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)


class Ticket(Base):
    """Ticket lifecycle (``status``): open -> closing -> closed.

    'closing' means a decision was made and the transcript/log/delete work is in
    progress. The partial unique index below is what really prevents duplicate
    tickets: a second open/closing row for the same member fails atomically.
    """

    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, _guild_fk(), index=True)
    channel_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(16), default="open")
    # The applicant's form answers: [{"label": "...", "value": "..."}, ...]
    answers: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    outcome: Mapped[str | None] = mapped_column(String(32))
    handled_by: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str | None] = mapped_column(Text)
    extra_role_id: Mapped[int | None] = mapped_column(BigInteger)
    kicked: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    # When the claim (open -> closing) happened. Used to spot tickets stuck in 'closing'.
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    logged: Mapped[bool] = mapped_column(Boolean, default=False)
    channel_gone: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        Index(
            "one_active_ticket_per_user",
            "guild_id",
            "user_id",
            unique=True,
            sqlite_where=text("status IN ('open', 'closing')"),
            postgresql_where=text("status IN ('open', 'closing')"),
        ),
        Index("ix_tickets_status", "status"),
    )

    @property
    def note_lines(self) -> list[str]:
        """The ticket's notes as a list. ``notes`` stores them as text, one note per line."""
        return [n for n in (self.notes or "").split("\n") if n]


class AllianceRequest(Base):
    """A member asking to join one alliance (``status``): pending -> accepted | rejected | closed | cancelled.

    'deciding' is the short step between an Accept click and the role change
    having worked. The partial unique index below is what really prevents a
    second request: another pending/deciding row for the same member fails
    atomically. An alliance is identified by its member role (``alliance_role_id``).
    """

    __tablename__ = "alliance_requests"

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, _guild_fk(), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str] = mapped_column(String(100))
    alliance_role_id: Mapped[int] = mapped_column(BigInteger)
    # The alliance's name when the request was made, so the record still reads well if it is renamed or removed.
    alliance_name: Mapped[str] = mapped_column(String(100))
    ingame_name: Mapped[str] = mapped_column(String(100))
    governor_id: Mapped[str] = mapped_column(String(32))
    # When the member joined the server, kept so the request message can be redrawn after they left.
    member_joined_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    # The message with the Accept / Reject buttons. A click finds its request by message_id.
    channel_id: Mapped[int | None] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    decided_by: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # When the claim (pending -> deciding) happened.
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        Index(
            "one_active_alliance_request_per_user",
            "guild_id",
            "user_id",
            unique=True,
            sqlite_where=text("status IN ('pending', 'deciding')"),
            postgresql_where=text("status IN ('pending', 'deciding')"),
        ),
    )


class PanelMessage(Base):
    """Where a module's public panel (e.g. the "Open Application" button) was posted."""

    __tablename__ = "panel_messages"

    guild_id: Mapped[int] = mapped_column(BigInteger, _guild_fk(), primary_key=True)
    module: Mapped[str] = mapped_column(String(32), primary_key=True)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)


class AuditLog(Base):
    """Who changed what, when. ``changes`` is {"field": [old, new], ...}."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, _guild_fk())
    actor_id: Mapped[int | None] = mapped_column(BigInteger)
    actor_name: Mapped[str] = mapped_column(String(100))
    source: Mapped[str] = mapped_column(String(16))  # dashboard | bot | migration | system
    action: Mapped[str] = mapped_column(String(64))
    module: Mapped[str | None] = mapped_column(String(32))
    changes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (Index("ix_audit_log_guild_created", "guild_id", "created_at"),)


class GuildIssue(Base):
    """A problem the bot hit with nobody to tell (e.g. it couldn't give a role
    when someone joined). Shown as a warning on the dashboard."""

    __tablename__ = "guild_issues"

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, _guild_fk())
    key: Mapped[str] = mapped_column(String(64))  # e.g. "tickets.pending_role"
    message: Mapped[str] = mapped_column(Text)
    first_seen: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    count: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (UniqueConstraint("guild_id", "key"),)


class WebSession(Base):
    """A logged-in dashboard user. The browser cookie holds a random token; we
    store only a hash of it, so a leaked database can't be used to log in."""

    __tablename__ = "web_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str] = mapped_column(String(100))
    avatar: Mapped[str | None] = mapped_column(String(64))
    # TODO: OAuth access tokens are stored plaintext so the dashboard can refresh
    # the guild list. A DB leak lets an attacker call Discord as that user for
    # identify/guilds until the token expires. Consider encrypting at rest.
    access_token: Mapped[str] = mapped_column(Text)
    token_expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    # The user's server list from Discord, cached because that endpoint is heavily rate-limited.
    guilds_cache: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    guilds_fetched_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


# bot_meta keys. The values are stored in the database, so never change them.
# (The heartbeat's key lives with its code, in core/health.py.)
COMMANDS_HASH_KEY = "commands_hash"
# Set by the KOC migration: guild IDs whose old per-guild slash commands (registered
# by KOC Gatekeeper) the bot should remove, so they don't show up twice.
CLEAR_GUILD_COMMANDS_KEY = "clear_guild_commands"


class BotMeta(Base):
    """Small key/value store for the bot itself (e.g. hash of registered slash commands)."""

    __tablename__ = "bot_meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
