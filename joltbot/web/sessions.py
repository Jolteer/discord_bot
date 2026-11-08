"""Server-side login sessions.

The browser's cookie holds a long random token. The database stores only an
HMAC of it (keyed with SESSION_SECRET), so someone who reads the database
still can't log in as anyone. Logging out deletes the row, which kills the
session everywhere immediately.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta
from typing import Any

from sqlalchemy import delete

from ..core.db import Database
from ..core.discord_rest import DiscordClient, DiscordError, DiscordUnauthorized
from ..core.models import WebSession
from ..core.types import utcnow

SESSION_LIFETIME = timedelta(days=7)
# How long we trust the user's cached server list (and their permissions in it).
# If someone loses Manage Server, they keep dashboard access for at most this long.
GUILD_LIST_MAX_AGE = timedelta(minutes=5)


def hash_token(secret: str, raw_token: str) -> str:
    return hmac.new(secret.encode(), raw_token.encode(), hashlib.sha256).hexdigest()


async def create_session(
    db: Database, secret: str, *, user: dict[str, Any], access_token: str, expires_in: int, guilds: list[dict[str, Any]]
) -> str:
    """Store a new session and return the raw token for the cookie."""
    raw = secrets.token_urlsafe(32)
    now = utcnow()
    async with db.session() as session, session.begin():
        await session.execute(delete(WebSession).where(WebSession.expires_at < now))  # tidy up old ones
        session.add(
            WebSession(
                id=hash_token(secret, raw),
                user_id=int(user["id"]),
                username=(user.get("global_name") or user.get("username") or "unknown")[:100],
                avatar=user.get("avatar"),
                access_token=access_token,
                token_expires_at=now + timedelta(seconds=expires_in),
                guilds_cache=guilds,
                guilds_fetched_at=now,
                csrf_token=secrets.token_urlsafe(32),
                created_at=now,
                expires_at=min(now + SESSION_LIFETIME, now + timedelta(seconds=expires_in)),
            )
        )
    return raw


async def load_session(db: Database, secret: str, raw_token: str | None) -> WebSession | None:
    if not raw_token or len(raw_token) > 200:
        return None
    async with db.session() as session:
        row = await session.get(WebSession, hash_token(secret, raw_token))
    if row is None or row.expires_at <= utcnow():
        return None
    return row


async def delete_session(db: Database, session_id: str) -> None:
    async with db.session() as session, session.begin():
        await session.execute(delete(WebSession).where(WebSession.id == session_id))


async def user_guilds(db: Database, discord: DiscordClient, ws: WebSession) -> list[dict[str, Any]]:
    """The user's servers from Discord, re-fetched when the cache is older than 5 minutes.

    Raises DiscordUnauthorized if their Discord login was revoked. If Discord
    is just slow or rate limiting, the cached list is used instead.
    """
    fresh_enough = ws.guilds_fetched_at and utcnow() - ws.guilds_fetched_at < GUILD_LIST_MAX_AGE
    if fresh_enough and ws.guilds_cache is not None:
        return ws.guilds_cache
    try:
        guilds = await discord.get_user_guilds(ws.access_token)
    except DiscordUnauthorized:
        raise
    except DiscordError:
        if ws.guilds_cache is not None:
            return ws.guilds_cache
        raise
    async with db.session() as session, session.begin():
        row = await session.get(WebSession, ws.id)
        if row is not None:
            row.guilds_cache = guilds
            row.guilds_fetched_at = utcnow()
    ws.guilds_cache, ws.guilds_fetched_at = guilds, utcnow()
    return guilds
