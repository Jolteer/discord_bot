"""Login, per-server permission and CSRF checks for the dashboard.

The golden rule: the browser is never trusted. Every page or form that
touches a server goes through ``guild_access``, which re-checks on the server
that the logged-in user may use the dashboard for THAT server. The server ID
only ever comes from the URL path and is checked; IDs inside form fields are
ignored.

Two ways in, both checked with Discord on every request (with short caches):
- Manage Server, Administrator or owning the server ("manager").
- Holding one of the server's dashboard access roles. These people can change
  settings but not who has access (``manager_access``).
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request

from ..core.config import AppConfig
from ..core.db import Database
from ..core.discord_rest import DiscordClient, DiscordError, DiscordUnauthorized
from ..core.guild_settings import Actor, load_module
from ..core.models import Guild, WebSession
from ..core.permissions import can_manage_guild, is_administrator
from ..modules.general.settings import MODULE as GENERAL
from .sessions import delete_session, load_session, user_guilds

SESSION_COOKIE = "joltbot_session"
SECURE_SESSION_COOKIE = "__Host-joltbot_session"  # __Host- = HTTPS only, this site only, whole site
STATE_COOKIE = "joltbot_oauth_state"


class LoginRequired(Exception):
    """Sends the visitor to the login page."""


@dataclass(frozen=True)
class UserGuild:
    """One server from the logged-in user's Discord server list, and what they may do there."""

    id: int
    name: str
    icon: str | None
    owner: bool
    can_manage: bool  # owner, Administrator or Manage Server: a "manager" of this server
    is_admin: bool  # owner or Administrator: may also make JoltBot give out powerful roles

    @property
    def icon_url(self) -> str | None:
        """Address of the server's icon on Discord's CDN, or ``None`` if it has none."""
        return f"https://cdn.discordapp.com/icons/{self.id}/{self.icon}.png?size=64" if self.icon else None


def parse_user_guilds(raw: list[dict[str, Any]]) -> list[UserGuild]:
    """Turn Discord's ``/users/@me/guilds`` answer into ``UserGuild`` objects.

    Args:
        raw: The list as Discord sent it (also what the session caches).

    Returns:
        One ``UserGuild`` per server, with the permission flags worked out.
    """
    return [
        UserGuild(
            int(g["id"]),
            g.get("name", "?"),
            g.get("icon"),
            bool(g.get("owner")),
            can_manage_guild(g.get("permissions"), owner=bool(g.get("owner"))),
            is_administrator(g.get("permissions"), owner=bool(g.get("owner"))),
        )
        for g in raw
    ]


# --------------------------------------------------------------------------- app state


def get_config(request: Request) -> AppConfig:
    """The process settings, put on the app at start-up."""
    return request.app.state.config


def get_db(request: Request) -> Database:
    """The dashboard's database."""
    return request.app.state.db


def get_discord(request: Request) -> DiscordClient:
    """The dashboard's Discord REST client (a stand-in in tests and in the site preview)."""
    return request.app.state.discord


def session_cookie_name(config: AppConfig) -> str:
    """Which session cookie to use: the ``__Host-`` one whenever the site is served over HTTPS."""
    return SECURE_SESSION_COOKIE if config.uses_https else SESSION_COOKIE


# --------------------------------------------------------------------------- login


async def optional_session(request: Request) -> WebSession | None:
    """Load the session from the cookie if present (memoized on ``request.state``).

    Args:
        request: Incoming HTTP request.

    Returns:
        ``WebSession`` or ``None`` if anonymous / expired.
    """
    if hasattr(request.state, "web_session"):
        return request.state.web_session
    config = get_config(request)
    ws = await load_session(get_db(request), config.session_secret, request.cookies.get(session_cookie_name(config)))
    request.state.web_session = ws
    return ws


async def require_session(request: Request) -> WebSession:
    """Require a logged-in dashboard session.

    Args:
        request: Incoming HTTP request.

    Returns:
        Active ``WebSession``.

    Raises:
        LoginRequired: No valid session cookie.
    """
    ws = await optional_session(request)
    if ws is None:
        raise LoginRequired()
    return ws


async def current_user_guilds(request: Request, ws: WebSession) -> list[UserGuild]:
    """The user's servers, re-checked with Discord at least every 5 minutes.

    Args:
        request: Incoming HTTP request.
        ws: Logged-in session.

    Returns:
        Parsed ``UserGuild`` list with ``can_manage`` flags.

    Raises:
        LoginRequired: Discord token revoked (session deleted).
        HTTPException: 503 if Discord is unreachable and no cache exists.
    """
    try:
        raw = await user_guilds(get_db(request), get_discord(request), ws)
    except DiscordUnauthorized:
        await delete_session(get_db(request), ws.id)  # their Discord login was revoked
        raise LoginRequired() from None
    except DiscordError:
        raise HTTPException(503, "Discord isn't responding right now. Please try again in a minute.") from None
    return parse_user_guilds(raw)


# --------------------------------------------------------------------------- per-server access


@dataclass(frozen=True)
class GuildAccess:
    """Proof that the current user may use this guild's dashboard. Only guild_access() creates it."""

    session: WebSession
    guild: Guild
    user_guild: UserGuild
    # True: Manage Server / Administrator / owner. False: let in by a dashboard access role.
    is_manager: bool

    @property
    def guild_id(self) -> int:
        """The server's ID. It comes from the checked URL, never from a form."""
        return self.guild.id

    @property
    def actor(self) -> Actor:
        """The logged-in user, in the form the audit log records."""
        return Actor(user_id=self.session.user_id, name=self.session.username, source="dashboard")


ACCESS_DENIED = (
    "You need Manage Server permission in that server, or a role its admins gave dashboard access, "
    "to manage JoltBot there."
)


async def role_access(request: Request, ws: WebSession, guild_id: int) -> Guild | None:
    """The guild, if this user holds one of its dashboard access roles right now.

    Args:
        request: Incoming HTTP request.
        ws: Logged-in session.
        guild_id: A server the user is a member of.

    Returns:
        The ``Guild`` row, or ``None`` if JoltBot isn't there, the server has no
        access roles, or the user holds none of them.

    Raises:
        DiscordError: The user's roles couldn't be fetched (never guessed).
    """
    async with get_db(request).session() as session:
        guild = await session.get(Guild, guild_id)
        if guild is None or not guild.active:
            return None
        allowed = set((await load_module(session, guild_id, GENERAL)).config.dashboard_role_ids)
    if not allowed:
        return None
    # Asked with the bot token, after the database session is closed.
    held = await get_discord(request).member_role_ids(guild_id, ws.user_id)
    return guild if held & allowed else None


async def guild_access(request: Request, guild_id: int) -> GuildAccess:
    """FastAPI dependency for every ``/servers/{guild_id}/...`` route.

    1. Must be logged in.
    2. Must be a member of that server, according to Discord. Anyone else gets
       the same 403 without our database being read, so outsiders can't find
       out whether JoltBot is in a server.
    3. Owner, Administrator or Manage Server: allowed, as a manager, if JoltBot
       is in that server (``guilds.active``).
    4. Otherwise they must hold one of the server's dashboard access roles
       right now (``role_access``).

    Args:
        request: Incoming HTTP request.
        guild_id: Guild id from the URL path only.

    Returns:
        ``GuildAccess`` proof object for the route handler.

    Raises:
        LoginRequired: Anonymous visitor.
        HTTPException: 403 if not allowed; 404 if a manager's server has no bot;
            503 if a member's roles can't be checked with Discord.
    """
    ws = await require_session(request)
    match = next((g for g in await current_user_guilds(request, ws) if g.id == guild_id), None)
    if match is None:
        raise HTTPException(403, ACCESS_DENIED)
    if match.can_manage:
        async with get_db(request).session() as session:
            guild = await session.get(Guild, guild_id)
        if guild is None or not guild.active:
            raise HTTPException(404, "JoltBot isn't in that server. Invite it first from the server list.")
        return GuildAccess(session=ws, guild=guild, user_guild=match, is_manager=True)
    try:
        guild = await role_access(request, ws, guild_id)
    except DiscordError:
        # Fail closed: without Discord we can't know they still hold the role.
        raise HTTPException(
            503, "Discord isn't responding right now, so your roles couldn't be checked. Try again in a minute."
        ) from None
    if guild is None:
        raise HTTPException(403, ACCESS_DENIED)
    return GuildAccess(session=ws, guild=guild, user_guild=match, is_manager=False)


RequireGuild = Annotated[GuildAccess, Depends(guild_access)]
RequireSession = Annotated[WebSession, Depends(require_session)]


async def manager_access(access: RequireGuild) -> GuildAccess:
    """Dependency for pages only a manager may use: deciding who has dashboard access.

    Args:
        access: The result of ``guild_access`` for this request.

    Returns:
        The same ``GuildAccess``, now known to be a manager's.

    Raises:
        HTTPException: 403 for someone let in by a dashboard access role.
    """
    if not access.is_manager:
        raise HTTPException(403, "Only members with Manage Server can change who has dashboard access.")
    return access


RequireManager = Annotated[GuildAccess, Depends(manager_access)]


# --------------------------------------------------------------------------- CSRF


async def verify_csrf(request: Request) -> None:
    """Reject POSTs that do not prove they came from our own page.

    1. The request must carry the session's CSRF token, either as a hidden form
       field (``csrf_token``) or an ``X-CSRF-Token`` header (sent by htmx).
    2. If the browser sent an Origin header, it must match ``PUBLIC_BASE_URL``.

    Args:
        request: Incoming mutating request.

    Raises:
        HTTPException: 403 on Origin mismatch or bad/missing CSRF token.
        LoginRequired: No session (token cannot be verified).
    """
    config = get_config(request)
    origin = request.headers.get("origin")
    if origin and origin != _origin_of(config.public_base_url):
        raise HTTPException(403, "This request came from another website, so it was blocked.")
    ws = await require_session(request)
    sent = request.headers.get("x-csrf-token")
    if sent is None:
        form = await request.form()  # Starlette caches this, so the route can read it again
        sent = form.get("csrf_token")
    if not isinstance(sent, str) or not hmac.compare_digest(sent, ws.csrf_token):
        raise HTTPException(403, "Your form expired or came from somewhere else. Reload the page and try again.")


def _origin_of(url: str) -> str:
    """Scheme and host of a URL: the form in which browsers send the Origin header."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


CsrfProtected = Depends(verify_csrf)
