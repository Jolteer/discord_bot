"""Small stand-ins for discord.py objects, so the ticket flow can be tested offline.

Members and channels are ``MagicMock(spec=...)`` objects: they pass the
``isinstance`` checks in the bot code, and their async methods (``send``,
``add_roles``, ``delete`` ...) record every call. Where the bot reads state
back (a member's roles, which channels exist), the fakes keep real state.

Only what the ticket module touches is modelled. Add to it as tests need more.
"""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord

from joltbot.core.guild_settings import Actor, load_module, save_module
from joltbot.modules.tickets.settings import TicketSettings

BOT_USER_ID = 9
BOT_ROLE_POSITION = 100  # roles at or above this can't be managed by the fake bot
TEST_ACTOR = Actor(user_id=None, name="tests", source="system")


def http_error(kind: type[discord.HTTPException] = discord.HTTPException, status: int = 500) -> discord.HTTPException:
    """Build a discord.py HTTP error (``Forbidden``, ``NotFound`` ...) without a real response."""
    return kind(SimpleNamespace(status=status, reason="fake"), "fake error")


@dataclass(eq=False)  # eq=False keeps identity hashing, so roles work as dict keys
class FakeRole:
    id: int
    name: str = "role"
    position: int = 1
    managed: bool = False
    mentionable: bool = True

    @property
    def mention(self) -> str:
        return f"<@&{self.id}>"

    def __ge__(self, other: FakeRole) -> bool:
        return self.position >= other.position

    def __lt__(self, other: FakeRole) -> bool:
        return self.position < other.position


class FakeGuild:
    """A server. ``channels`` and ``members`` may be fakes or bare IDs (bare IDs just "exist")."""

    def __init__(self, guild_id: int, channels: Any = (), members: Any = (), *, name: str = "Test Server") -> None:
        self.id = guild_id
        self.name = name
        self.unavailable = False
        self.icon = None
        self.owner_id = 1
        self.filesize_limit = 10 * 1024 * 1024
        self.default_role = FakeRole(guild_id, "@everyone", position=0)
        self._roles: dict[int, FakeRole] = {}
        self._channels: dict[int, Any] = {}
        self._members: dict[int, Any] = {}
        self._uncached_members: dict[int, Any] = {}
        self.emojis: list[Any] = []  # the server's own emojis: objects with id, name and animated
        for channel in channels:
            self.add_channel(channel)
        for member in members:
            self.add_member(member)
        self.created_channels: list[Any] = []
        self.create_error: Exception | None = None  # raised by create_text_channel when set
        self._next_channel_id = 7000
        self.bot_role = self.add_role(FakeRole(guild_id + 1, "JoltBot", position=BOT_ROLE_POSITION, managed=True))
        self.me = fake_member(self, BOT_USER_ID, name="JoltBot", roles=[self.bot_role], bot=True, register=False)
        self.me.guild_permissions = discord.Permissions(
            manage_roles=True, manage_channels=True, kick_members=True, view_channel=True, send_messages=True
        )

    # --- building the server -------------------------------------------------

    def add_role(self, role: FakeRole) -> FakeRole:
        self._roles[role.id] = role
        return role

    def add_channel(self, channel: Any) -> Any:
        self._channels[channel if isinstance(channel, int) else channel.id] = (
            object() if isinstance(channel, int) else channel
        )
        return channel

    def remove_channel(self, channel_id: int) -> None:
        self._channels.pop(channel_id, None)

    def add_member(self, member: Any, *, cached: bool = True) -> Any:
        """``cached=False``: still in the server, but missing from the bot's member cache."""
        key = member if isinstance(member, int) else member.id
        value = object() if isinstance(member, int) else member
        (self._members if cached else self._uncached_members)[key] = value
        return member

    def remove_member(self, user_id: int) -> None:
        self._members.pop(user_id, None)
        self._uncached_members.pop(user_id, None)

    # --- what the bot calls --------------------------------------------------

    @property
    def members(self) -> list[Any]:
        """The cached members, like ``discord.Guild.members`` (the bot itself is left out)."""
        return list(self._members.values())

    def get_role(self, role_id: int) -> FakeRole | None:
        return self._roles.get(role_id)

    def get_channel(self, channel_id: int) -> Any:
        return self._channels.get(channel_id)

    def get_member(self, user_id: int) -> Any:
        return self._members.get(user_id)

    async def fetch_member(self, user_id: int) -> Any:
        member = self._members.get(user_id) or self._uncached_members.get(user_id)
        if member is None:
            raise http_error(discord.NotFound, 404)
        return member

    async def create_text_channel(self, *, name: str, **options: Any) -> Any:
        if self.create_error is not None:
            raise self.create_error
        self._next_channel_id += 1
        channel = fake_text_channel(self, self._next_channel_id, name)
        channel.category = options.get("category")
        channel.overwrites = options.get("overwrites") or {}
        channel.topic = options.get("topic")
        self.created_channels.append(channel)
        return channel


def fake_member(
    guild: FakeGuild,
    user_id: int,
    *,
    name: str = "member",
    roles: Any = (),
    administrator: bool = False,
    manage_guild: bool = False,
    bot: bool = False,
    register: bool = True,
) -> Any:
    """A server member whose roles really change when the bot adds or removes them."""
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.name = name
    member.display_name = name
    member.mention = f"<@{user_id}>"
    member.bot = bot
    member.guild = guild
    member.roles = list(roles)
    member.created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    member.joined_at = datetime(2024, 6, 1, tzinfo=timezone.utc)
    member.display_avatar.url = "https://cdn.discordapp.com/embed/avatars/0.png"
    member.guild_permissions = discord.Permissions(administrator=administrator, manage_guild=manage_guild)
    member.__str__.return_value = name

    def refresh_top_role() -> None:
        member.top_role = max(member.roles, key=lambda r: r.position, default=guild.default_role)

    async def add_roles(*new: FakeRole, reason: str | None = None) -> None:
        member.roles.extend(r for r in new if r not in member.roles)
        refresh_top_role()

    async def remove_roles(*old: FakeRole, reason: str | None = None) -> None:
        member.roles[:] = [r for r in member.roles if r not in old]
        refresh_top_role()

    async def kick(reason: str | None = None) -> None:
        guild.remove_member(user_id)

    async def edit(*, roles: Any = None, reason: str | None = None) -> None:
        # Like Discord: the member's whole role list is replaced in one call.
        if roles is not None:
            member.roles[:] = list(roles)
            refresh_top_role()

    refresh_top_role()
    member.add_roles = AsyncMock(side_effect=add_roles)
    member.remove_roles = AsyncMock(side_effect=remove_roles)
    member.edit = AsyncMock(side_effect=edit)
    member.kick = AsyncMock(side_effect=kick)
    member.send = AsyncMock()
    if register:
        guild.add_member(member)
    return member


async def _iterate(items: list[Any]):
    for item in items:
        yield item


def fake_text_channel(guild: FakeGuild, channel_id: int, name: str = "channel", messages: Any = ()) -> Any:
    """A text channel. ``channel.messages`` is what ``history()`` returns; ``send`` records calls."""
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.name = name
    channel.mention = f"<#{channel_id}>"
    channel.guild = guild
    channel.messages = list(messages)
    channel.__str__.return_value = name
    channel.send = AsyncMock()
    channel.history = lambda **_: _iterate(list(channel.messages))
    channel.overwrites_for = MagicMock(side_effect=lambda _target: discord.PermissionOverwrite())
    channel.set_permissions = AsyncMock()

    async def delete(reason: str | None = None) -> None:
        guild.remove_channel(channel_id)

    channel.delete = AsyncMock(side_effect=delete)
    guild.add_channel(channel)
    return channel


def record_messages(channel: Any, first_id: int = 9000) -> Any:
    """Make ``channel.send`` return real message stand-ins, kept in ``channel.sent``.

    Each has an ``id`` and remembers its ``content``, ``embed`` and ``view``.
    Editing one, directly or through ``channel.get_partial_message(id)``,
    changes what it remembers, so a test can read back what a message looks
    like now. Returns the channel.
    """
    channel.sent = []

    async def send(content: str | None = None, **options: Any) -> Any:
        message = SimpleNamespace(
            id=first_id + len(channel.sent),
            channel=channel,
            content=content,
            embed=options.get("embed"),
            view=options.get("view"),
            pin=AsyncMock(),
            delete=AsyncMock(),
        )

        async def edit(**changes: Any) -> None:
            for key, value in changes.items():
                setattr(message, key, value)

        message.edit = AsyncMock(side_effect=edit)
        channel.sent.append(message)
        return message

    def partial(message_id: int) -> Any:
        found = next((m for m in channel.sent if m.id == message_id), None)
        if found is not None:
            return found
        missing = SimpleNamespace(id=message_id)
        missing.edit = AsyncMock(side_effect=http_error(discord.NotFound, 404))
        missing.delete = AsyncMock(side_effect=http_error(discord.NotFound, 404))
        return missing

    channel.send = AsyncMock(side_effect=send)
    channel.get_partial_message = MagicMock(side_effect=partial)
    return channel


def fake_category(guild: FakeGuild, channel_id: int, name: str = "Tickets") -> Any:
    category = MagicMock(spec=discord.CategoryChannel)
    category.id = channel_id
    category.name = name
    category.channels = []
    guild.add_channel(category)
    return category


def fake_attachment(filename: str, data: bytes = b"file-bytes") -> Any:
    return SimpleNamespace(
        filename=filename,
        content_type="image/png",
        size=len(data),
        url=f"https://cdn.discordapp.com/attachments/1/2/{filename}",
        to_file=AsyncMock(side_effect=lambda: discord.File(io.BytesIO(data), filename=filename)),
    )


def fake_message(author: Any, content: str = "", attachments: Any = ()) -> Any:
    return SimpleNamespace(
        author=author,
        content=content,
        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        embeds=[],
        attachments=list(attachments),
        stickers=[],
    )


def fake_interaction(guild: FakeGuild | None, user: Any, channel: Any = None) -> Any:
    """A button click or form submit. Replies are recorded on ``response`` and ``followup``."""
    interaction = MagicMock()
    interaction.guild = guild
    interaction.guild_id = guild.id if guild is not None else None
    interaction.user = user
    interaction.channel = channel
    interaction.channel_id = channel.id if channel is not None else None
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.original_response = AsyncMock()
    return interaction


class FakeBot:
    """The parts of ``JoltBot`` the ticket module uses. ``issues`` records ``report_issue`` calls."""

    def __init__(self, db: Any, guilds: Any) -> None:
        self.db = db
        self._guilds = {g.id: g for g in guilds}
        self.issues: list[tuple[int, str]] = []
        self.resolved: list[tuple[int, str]] = []

    @property
    def guilds(self) -> list[Any]:
        return list(self._guilds.values())

    def get_guild(self, guild_id: int) -> Any:
        return self._guilds.get(guild_id)

    async def report_issue(self, guild_id: int, key: str, message: str) -> None:
        self.issues.append((guild_id, key))

    async def resolve_issue(self, guild_id: int, key: str) -> None:
        self.resolved.append((guild_id, key))


async def settle(manager: Any) -> None:
    """Wait for the ticket manager's background work (finalize, delete) to finish."""
    while manager._tasks:
        await asyncio.gather(*list(manager._tasks))


async def enable_tickets(db: Any, guild_id: int, **settings: Any) -> TicketSettings:
    """Save ticket settings for a guild and switch the module on."""
    config = TicketSettings(**settings)
    async with db.session() as session, session.begin():
        current = await load_module(session, guild_id, "tickets")
        await save_module(
            session,
            guild_id,
            "tickets",
            enabled=True,
            config=config,
            expected_version=current.version,
            actor=TEST_ACTOR,
        )
    return config
