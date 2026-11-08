"""Login, per-server permission and CSRF checks for the dashboard.

The golden rule: the browser is never trusted. Every page or form that
touches a server goes through ``guild_access``, which re-checks on the server
that the logged-in user can manage THAT server. The server ID only ever comes
from the URL path and is checked; IDs inside form fields are ignored.
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
from ..core.guild_settings import Actor
from ..core.models import Guild, WebSession
from ..core.permissions import can_manage_guild
from .sessions import delete_session, load_session, user_guilds

SESSION_COOKIE = "joltbot_session"
SECURE_SESSION_COOKIE = "__Host-joltbot_session"  # __Host- = HTTPS only, this site only, whole site
STATE_COOKIE = "joltbot_oauth_state"


class LoginRequired(Exception):
    """Sends the visitor to the login page."""


@dataclass(frozen=True)
class UserGuild:
    id: int
    name: str
    icon: str | None
    owner: bool
    can_manage: bool

    @property
    def icon_url(self) -> str | None:
        return f"https://cdn.discordapp.com/icons/{self.id}/{self.icon}.png?size=64" if self.icon else None


def parse_user_guilds(raw: list[dict[str, Any]]) -> list[UserGuild]:
    return [
        UserGuild(
            int(g["id"]),
            g.get("name", "?"),
            g.get("icon"),
            bool(g.get("owner")),
            can_manage_guild(g.get("permissions"), owner=bool(g.get("owner"))),
        )
        for g in raw
    ]


# --------------------------------------------------------------------------- app state


def get_config(request: Request) -> AppConfig:
    return request.app.state.config


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_discord(request: Request) -> DiscordClient:
    return request.app.state.discord


def session_cookie_name(config: AppConfig) -> str:
    return SECURE_SESSION_COOKIE if config.uses_https else SESSION_COOKIE


# --------------------------------------------------------------------------- login


async def optional_session(request: Request) -> WebSession | None:
    if hasattr(request.state, "web_session"):
        return request.state.web_session
    config = get_config(request)
    ws = await load_session(get_db(request), config.session_secret, request.cookies.get(session_cookie_name(config)))
    request.state.web_session = ws
    return ws


async def require_session(request: Request) -> WebSession:
    ws = await optional_session(request)
    if ws is None:
        raise LoginRequired()
    return ws


async def current_user_guilds(request: Request, ws: WebSession) -> list[UserGuild]:
    """The user's servers, re-checked with Discord at least every 5 minutes."""
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
    """Proof that the current user may manage this guild. Only guild_access() creates it."""

    session: WebSession
    guild: Guild
    user_guild: UserGuild

    @property
    def guild_id(self) -> int:
        return self.guild.id

    @property
    def actor(self) -> Actor:
        return Actor(user_id=self.session.user_id, name=self.session.username, source="dashboard")


async def guild_access(request: Request, guild_id: int) -> GuildAccess:
    """FastAPI dependency for every /servers/{guild_id}/... route.

    1. Must be logged in.
    2. Discord must say they're the owner, an Administrator or have Manage Server there.
       (Checked BEFORE looking at our database, so non-admins can't even find out
       whether JoltBot is in a server.)
    3. JoltBot must be in that server.
    """
    ws = await require_session(request)
    match = next((g for g in await current_user_guilds(request, ws) if g.id == guild_id), None)
    if match is None or not match.can_manage:
        raise HTTPException(403, "You need Manage Server permission in that server to manage JoltBot there.")
    async with get_db(request).session() as session:
        guild = await session.get(Guild, guild_id)
    if guild is None or not guild.active:
        raise HTTPException(404, "JoltBot isn't in that server. Invite it first from the server list.")
    return GuildAccess(session=ws, guild=guild, user_guild=match)


RequireGuild = Annotated[GuildAccess, Depends(guild_access)]
RequireSession = Annotated[WebSession, Depends(require_session)]


# --------------------------------------------------------------------------- CSRF


async def verify_csrf(request: Request) -> None:
    """Every POST must prove it came from our own page.

    1. The request must carry the session's CSRF token, either as a hidden form
       field (csrf_token) or an X-CSRF-Token header (sent by htmx). Another site
       can make your browser POST here, but it can't read this token.
    2. If the browser sent an Origin header, it must be our own site.
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
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


CsrfProtected = Depends(verify_csrf)
