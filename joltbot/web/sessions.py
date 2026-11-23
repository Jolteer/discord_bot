"""Server-side login sessions.

The browser's cookie holds a long random token. The database stores only an
HMAC of it (keyed with SESSION_SECRET), so someone who reads the database
still can't log in as anyone. Logging out deletes the row, which kills the
session everywhere immediately.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
from datetime import timedelta
from typing import Any

from sqlalchemy import delete

from ..core.db import Database, rows_changed
from ..core.discord_rest import DiscordClient, DiscordError, DiscordUnauthorized
from ..core.models import WebSession
from ..core.types import utcnow

log = logging.getLogger(__name__)

SESSION_LIFETIME = timedelta(days=7)
PURGE_EVERY_SECONDS = 3600  # how often expired sessions are deleted while the dashboard runs
# How long we trust the user's cached server list (and their permissions in it).
# If someone loses Manage Server, they keep dashboard access for at most this long.
GUILD_LIST_MAX_AGE = timedelta(minutes=5)
# If Discord can't be asked (an outage), the cached list is still used up to this age.
# After that, server pages answer "Discord isn't responding" until it can be refreshed.
GUILD_LIST_OUTAGE_LIMIT = timedelta(minutes=30)
# A real cookie token is 43 characters. Anything much longer isn't ours, so it is
# refused before it is hashed or looked up.
MAX_TOKEN_LENGTH = 200


def hash_token(secret: str, raw_token: str) -> str:
    """HMAC-SHA256 the cookie token for storage as ``web_sessions.id``.

    Args:
        secret: ``SESSION_SECRET``.
        raw_token: Opaque value placed in the browser cookie.

    Returns:
        Hex digest used as the primary key (DB leak ≠ login).
    """
    return hmac.new(secret.encode(), raw_token.encode(), hashlib.sha256).hexdigest()


async def create_session(
    db: Database, secret: str, *, user: dict[str, Any], access_token: str, expires_in: int, guilds: list[dict[str, Any]]
) -> str:
    """Store a new session and return the raw token for the cookie.

    Session lifetime is ``min(7 days, Discord token expires_in)`` because there
    is no refresh-token flow.

    Args:
        db: Shared database.
        secret: ``SESSION_SECRET`` for hashing the cookie token.
        user: Discord ``/users/@me`` payload.
        access_token: Discord OAuth access token (stored plaintext for guild list refresh).
        expires_in: Seconds until the Discord token expires.
        guilds: Initial guild list from Discord.

    Returns:
        Raw cookie token (never store this in the DB).
    """
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


async def purge_expired(db: Database) -> int:
    """Delete every session whose time is up.

    An expired session can no longer log anyone in, but its row still holds a
    Discord access token until it is deleted.

    Args:
        db: Shared database.

    Returns:
        How many sessions were deleted.
    """
    async with db.session() as session, session.begin():
        result = await session.execute(delete(WebSession).where(WebSession.expires_at < utcnow()))
    return rows_changed(result)


async def purge_expired_forever(db: Database) -> None:
    """Delete expired sessions when the dashboard starts, then once an hour. Cancel the task to stop it.

    Logging in also removes them (``create_session``), but on a server where
    people log in rarely that could leave expired rows lying for weeks.

    Args:
        db: Shared database.
    """
    while True:
        try:
            removed = await purge_expired(db)
            if removed:
                log.info("Removed %d expired login session(s)", removed)
        except Exception:
            # e.g. the database is busy. Nothing is lost: the next round tries again.
            log.warning("Couldn't remove expired login sessions", exc_info=True)
        await asyncio.sleep(PURGE_EVERY_SECONDS)


async def load_session(db: Database, secret: str, raw_token: str | None) -> WebSession | None:
    """Resolve a cookie token to a non-expired ``WebSession`` row.

    Args:
        db: Shared database.
        secret: ``SESSION_SECRET``.
        raw_token: Cookie value, or None if missing.

    Returns:
        Session row, or ``None`` if missing/expired/invalid.
    """
    if not raw_token or len(raw_token) > MAX_TOKEN_LENGTH:
        return None
    async with db.session() as session:
        row = await session.get(WebSession, hash_token(secret, raw_token))
    if row is None or row.expires_at <= utcnow():
        return None
    return row


async def delete_session(db: Database, session_id: str) -> None:
    """Delete a session row (logout / revoked Discord login).

    Args:
        db: Shared database.
        session_id: Hashed session id (``WebSession.id``).
    """
    async with db.session() as session, session.begin():
        await session.execute(delete(WebSession).where(WebSession.id == session_id))


async def user_guilds(db: Database, discord: DiscordClient, ws: WebSession) -> list[dict[str, Any]]:
    """Return the user's Discord guild list, refreshing when the cache is stale.

    Args:
        db: Shared database (updates ``guilds_cache`` on success).
        discord: REST client used with the stored access token.
        ws: Current web session.

    Returns:
        Raw Discord guild objects (dicts).

    Raises:
        DiscordUnauthorized: Discord login was revoked (caller should delete session).
        DiscordError: Discord failed and there is no cached list recent enough
            (``GUILD_LIST_OUTAGE_LIMIT``) to fall back on.
    """
    fresh_enough = ws.guilds_fetched_at and utcnow() - ws.guilds_fetched_at < GUILD_LIST_MAX_AGE
    if fresh_enough and ws.guilds_cache is not None:
        return ws.guilds_cache
    try:
        guilds = await discord.get_user_guilds(ws.access_token)
    except DiscordUnauthorized:
        raise
    except DiscordError:
        # Discord is having trouble. Keep admins working from the list we have, but
        # not forever: their permissions can't be re-checked while it lasts.
        usable = ws.guilds_fetched_at and utcnow() - ws.guilds_fetched_at < GUILD_LIST_OUTAGE_LIMIT
        if usable and ws.guilds_cache is not None:
            return ws.guilds_cache
        raise
    async with db.session() as session, session.begin():
        row = await session.get(WebSession, ws.id)
        if row is not None:
            row.guilds_cache = guilds
            row.guilds_fetched_at = utcnow()
    ws.guilds_cache, ws.guilds_fetched_at = guilds, utcnow()
    return guilds
