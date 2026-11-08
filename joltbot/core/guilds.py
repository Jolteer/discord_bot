"""Which servers JoltBot is in: join, leave, startup sync and the 30-day purge."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..modules.registry import MODULES
from .models import Guild, ModuleSetting
from .types import utcnow

RETENTION = timedelta(days=30)


@dataclass(frozen=True)
class GuildInfo:
    id: int
    name: str
    icon: str | None = None


async def ensure_guild(session: AsyncSession, info: GuildInfo) -> Guild:
    """Create or reactivate a guild. New guilds get a settings row per module,
    switched off, so an admin has to set each module up before it does anything."""
    now = utcnow()
    guild = await session.get(Guild, info.id)
    if guild is None:
        guild = Guild(id=info.id, name=info.name[:100], icon=info.icon, active=True, joined_at=now)
        session.add(guild)
    else:
        if not guild.active:  # re-invited within 30 days: keep the old settings
            guild.active = True
            guild.left_at = None
            guild.joined_at = now
        guild.name = info.name[:100]
        guild.icon = info.icon

    existing = set(
        (await session.execute(select(ModuleSetting.module).where(ModuleSetting.guild_id == info.id))).scalars()
    )
    for name in MODULES:
        if name not in existing:
            session.add(ModuleSetting(guild_id=info.id, module=name, enabled=False, config={}, version=1))
    await session.flush()
    return guild


async def mark_guild_left(session: AsyncSession, guild_id: int) -> None:
    """JoltBot was removed. Keep the data for 30 days in case it's re-invited."""
    guild = await session.get(Guild, guild_id)
    if guild is not None and guild.active:
        guild.active = False
        guild.left_at = utcnow()
        await session.flush()


async def sync_guilds(
    session: AsyncSession, present: list[GuildInfo], unavailable_ids: set[int] = frozenset()
) -> tuple[list[int], list[int]]:
    """Make the guilds table match the servers the bot is actually in.

    ``unavailable_ids`` are servers Discord reports as temporarily unavailable
    (an outage on their side). The bot is still in them, so they must NOT be
    marked as left. Returns (ids that are new or rejoined, ids marked as left).
    """
    present_ids = {g.id for g in present}
    active_before = set((await session.execute(select(Guild.id).where(Guild.active.is_(True)))).scalars())

    for info in present:
        await ensure_guild(session, info)

    left = sorted(active_before - present_ids - set(unavailable_ids))
    for guild_id in left:
        await mark_guild_left(session, guild_id)
    joined = sorted(present_ids - active_before)
    return joined, left


async def purge_left_guilds(session: AsyncSession, now: datetime | None = None) -> list[int]:
    """Delete guilds that left more than 30 days ago. Their settings, tickets,
    audit log, etc. go with them (ON DELETE CASCADE)."""
    cutoff = (now or utcnow()) - RETENTION
    ids = list(
        (
            await session.execute(
                select(Guild.id).where(Guild.active.is_(False), Guild.left_at.is_not(None), Guild.left_at < cutoff)
            )
        ).scalars()
    )
    if ids:
        await session.execute(delete(Guild).where(Guild.id.in_(ids)))
    return ids


async def is_active_guild(session: AsyncSession, guild_id: int) -> bool:
    guild = await session.get(Guild, guild_id)
    return guild is not None and guild.active
