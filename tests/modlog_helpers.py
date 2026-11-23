"""Shared stand-ins for the mod log tests: a clock moved by hand, fake audit entries, a ready-made server."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import discord
from sqlalchemy import select

from joltbot.core.guild_settings import load_module, save_module
from joltbot.core.models import ModLogEntry
from joltbot.modules.modlog.cog import ModLog
from joltbot.modules.modlog.constants import MODULE
from joltbot.modules.modlog.service import ModLogManager
from joltbot.modules.modlog.settings import ModLogSettings

from .fakes import TEST_ACTOR, FakeBot, FakeGuild, fake_text_channel

MEMBERS_LOG, MESSAGES_LOG, MODERATION_LOG, SERVER_LOG = 501, 502, 503, 504
CHAT = 600  # an ordinary channel members talk in
MOD_ID = 42  # the moderator named by the fake audit entries


class FakeClock:
    """A clock that only moves when a test says so."""

    def __init__(self) -> None:
        self.time = 1000.0

    def __call__(self) -> float:
        return self.time

    def advance(self, seconds: float) -> None:
        self.time += seconds


class Diff:
    """Stands in for ``discord.AuditLogDiff``: attributes that can also be iterated as (name, value)."""

    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)

    def __iter__(self):
        yield from self.__dict__.items()


def audit_entry(
    guild: Any,
    action: discord.AuditLogAction,
    *,
    user_id: int = MOD_ID,
    target_id: int | None = None,
    extra: Any = None,
    before: Diff | None = None,
    after: Diff | None = None,
    reason: str | None = None,
) -> Any:
    """A fake audit log entry, with only the parts ``record_from_entry`` reads."""
    return SimpleNamespace(
        guild=guild,
        action=action,
        user_id=user_id,
        target=SimpleNamespace(id=target_id) if target_id is not None else None,
        extra=extra,
        before=before or Diff(),
        after=after or Diff(),
        reason=reason,
    )


def all_log_channels() -> dict[str, int]:
    """Settings with a log channel for every category (voice uses the members channel)."""
    return {
        "members_channel_id": MEMBERS_LOG,
        "messages_channel_id": MESSAGES_LOG,
        "moderation_channel_id": MODERATION_LOG,
        "server_channel_id": SERVER_LOG,
    }


async def enable_modlog(db: Any, guild_id: int, **settings: Any) -> ModLogSettings:
    """Save mod log settings for a guild and switch the module on. Defaults to a channel per category."""
    config = ModLogSettings(**{**all_log_channels(), **settings})
    async with db.session() as session, session.begin():
        current = await load_module(session, guild_id, MODULE)
        await save_module(
            session, guild_id, MODULE, enabled=True, config=config, expected_version=current.version, actor=TEST_ACTOR
        )
    return config


class Server:
    """One fake server with log channels, a bot and the mod log cog, ready to receive events."""

    def __init__(self, db: Any, guild_id: int, *, view_audit_log: bool = True) -> None:
        self.db = db
        self.guild = FakeGuild(guild_id)
        self.guild.me.guild_permissions = discord.Permissions(view_audit_log=view_audit_log, send_messages=True)
        self.logs = {
            channel_id: fake_text_channel(self.guild, channel_id, name)
            for channel_id, name in (
                (MEMBERS_LOG, "member-log"),
                (MESSAGES_LOG, "message-log"),
                (MODERATION_LOG, "mod-log"),
                (SERVER_LOG, "server-log"),
            )
        }
        self.chat = fake_text_channel(self.guild, CHAT, "general")
        self.chat.category_id = None
        self.bot = FakeBot(db, [self.guild])
        self.clock = FakeClock()
        self.manager = ModLogManager(self.bot, wait=0, clock=self.clock)  # type: ignore[arg-type]
        self.manager.dispatcher.pause = 0
        self.bot.modlog = self.manager
        self.cog = ModLog(self.bot)  # type: ignore[arg-type]

    async def entries(self) -> list[ModLogEntry]:
        """Every saved entry of this server, oldest first."""
        async with self.db.session() as session:
            result = await session.execute(
                select(ModLogEntry).where(ModLogEntry.guild_id == self.guild.id).order_by(ModLogEntry.id)
            )
            return list(result.scalars())

    async def posted(self, channel_id: int) -> list[discord.Embed]:
        """Every embed posted in a log channel so far, in order."""
        await self.manager.dispatcher.drain()
        embeds: list[discord.Embed] = []
        for call in self.logs[channel_id].send.await_args_list:
            embeds += call.kwargs.get("embeds", [])
        return embeds

    async def audit(self, action: discord.AuditLogAction, **fields: Any) -> None:
        """Deliver a fake audit log entry to the cog."""
        await self.cog.on_audit_log_entry_create(audit_entry(self.guild, action, **fields))


def message(author: Any, content: str = "hello", *, message_id: int = 9001, channel: Any = None, **extra: Any) -> Any:
    """A fake cached message."""
    return SimpleNamespace(
        id=message_id,
        author=author,
        content=content,
        channel=channel,
        attachments=[],
        stickers=[],
        embeds=[],
        edited_at=None,
        jump_url=f"https://discord.com/channels/1/{CHAT}/{message_id}",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        **extra,
    )


def field(embed: discord.Embed, name: str) -> str | None:
    """The value of an embed field by name, or ``None`` if the embed has no such field."""
    return next((f.value for f in embed.fields if f.name == name), None)
