"""Problems the bot hit with nobody to tell (e.g. it couldn't give a role when
someone joined). The bot records them here; the dashboard shows them as
warnings until they're fixed. Each (guild, key) is one row, so a problem that
repeats 500 times is still one warning with a count."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Guild, GuildIssue
from .types import utcnow


async def record_issue(session: AsyncSession, guild_id: int, key: str, message: str) -> bool:
    """Upsert a guild issue by ``(guild_id, key)``, bumping ``count`` on repeats.

    Args:
        session: Open async session (caller commits).
        guild_id: Discord guild snowflake.
        key: Stable machine key (e.g. ``tickets.pending_role``).
        message: Latest human-readable text for the dashboard.

    Returns:
        True if this problem is new for the guild (no open issue with this key).
        False for a repeat, or when there is no guild row to attach it to. A
        problem counts as new again after it was cleared or dismissed.
    """
    if await session.get(Guild, guild_id) is None:
        return False  # guild row not created yet; nothing to attach the issue to
    existing = (
        await session.execute(select(GuildIssue).where(GuildIssue.guild_id == guild_id, GuildIssue.key == key))
    ).scalar_one_or_none()
    now = utcnow()
    if existing is None:
        session.add(GuildIssue(guild_id=guild_id, key=key, message=message, first_seen=now, last_seen=now, count=1))
    else:
        existing.message = message
        existing.last_seen = now
        existing.count += 1
    await session.flush()
    return existing is None


async def clear_issue(session: AsyncSession, guild_id: int, key: str) -> None:
    """Delete a guild issue if present.

    Args:
        session: Open async session (caller commits).
        guild_id: Discord guild snowflake.
        key: Issue key previously passed to ``record_issue``.
    """
    await session.execute(delete(GuildIssue).where(GuildIssue.guild_id == guild_id, GuildIssue.key == key))


async def list_issues(session: AsyncSession, guild_id: int) -> list[GuildIssue]:
    """List open issues for a guild, newest activity first.

    Args:
        session: Open async session.
        guild_id: Discord guild snowflake.

    Returns:
        ``GuildIssue`` rows for the overview page.
    """
    result = await session.execute(
        select(GuildIssue).where(GuildIssue.guild_id == guild_id).order_by(GuildIssue.last_seen.desc())
    )
    return list(result.scalars())
