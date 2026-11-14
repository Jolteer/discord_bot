"""Talks to Discord's REST API for the dashboard (no gateway connection).

Two kinds of calls:
- As the logged-in user (OAuth2 access token): who they are, which servers they're in.
- As the bot (bot token): a server's roles and channels, and JoltBot's own roles there.

Server roles/channels are cached for 60 seconds per guild so the dashboard
doesn't hit Discord's rate limits when people click around.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

from .permissions import DISCORD_AUTHORIZE_URL

log = logging.getLogger(__name__)

API = "https://discord.com/api/v10"
CDN = "https://cdn.discordapp.com"
AUTHORIZE_URL = DISCORD_AUTHORIZE_URL
OAUTH_SCOPES = "identify guilds"
# Discord's OAuth access tokens last a week; used when a token response doesn't say.
DEFAULT_TOKEN_LIFETIME_SECONDS = 604800
HTTP_TIMEOUT_SECONDS = 10
# A rate limit shorter than this is waited out and the call retried once; a longer one is an error.
MAX_RATE_LIMIT_WAIT_SECONDS = 5
EMOJI_PREVIEW_SIZE = 32  # pixels, for the small emoji image next to a dashboard field
GUILD_CACHE_SECONDS = 60
# How long one member's roles are trusted. Someone who loses a dashboard access role
# keeps dashboard access for at most this long.
MEMBER_CACHE_SECONDS = 60
MEMBER_CACHE_MAX = 1000  # entries; expired ones are dropped once the cache grows past this

# Discord channel types we care about
TEXT_CHANNEL, CATEGORY, ANNOUNCEMENT_CHANNEL = 0, 4, 5


class DiscordError(Exception):
    """Discord returned an error or couldn't be reached."""

    def __init__(self, message: str, status: int | None = None) -> None:
        """Keep the HTTP status next to the message.

        Args:
            message: What went wrong, safe to show to a dashboard user.
            status: The HTTP status Discord answered with, or ``None`` if it couldn't be reached.
        """
        super().__init__(message)
        self.status = status


class DiscordUnauthorized(DiscordError):
    """The access token is no longer valid (expired or revoked): log in again."""


@dataclass(frozen=True)
class Role:
    """A server role, as Discord's REST API describes it.

    Attributes:
        id: The role's Discord ID.
        name: Its name, without the leading ``@``.
        position: Its place in the role list; higher numbers are higher up.
        permissions: Its permission bitfield (see ``core/permissions.py``).
        managed: True for a role owned by a bot or integration; nobody can assign those.
        color: ``0xRRGGBB``, or 0 for a role with no colour of its own.
    """

    id: int
    name: str
    position: int
    permissions: int
    managed: bool
    color: int


@dataclass(frozen=True)
class Channel:
    """A server channel or category.

    Attributes:
        id: The channel's Discord ID.
        name: Its name, without the leading ``#``.
        type: Discord's channel type number (see the constants above).
        position: Its place in the channel list, within its category.
        parent_id: The category it sits in, or ``None``.
    """

    id: int
    name: str
    type: int
    position: int
    parent_id: int | None


@dataclass(frozen=True)
class Emoji:
    """One of a server's own (custom) emojis."""

    id: int
    name: str
    animated: bool

    @property
    def url(self) -> str:
        """Address of the emoji's image on Discord's CDN, small enough to sit beside a form field."""
        return f"{CDN}/emojis/{self.id}.{'gif' if self.animated else 'png'}?size={EMOJI_PREVIEW_SIZE}"


@dataclass(frozen=True)
class GuildData:
    """Everything the dashboard needs about one server, fetched with the bot token."""

    guild_id: int
    roles: list[Role]  # highest first, without @everyone
    channels: list[Channel]
    everyone_permissions: int
    bot_role_ids: list[int]
    fetched_at: float = field(default_factory=time.time)
    emojis: list[Emoji] = field(default_factory=list)  # the server's own emojis

    @property
    def role_ids(self) -> set[int]:
        """The ID of every role in the server (@everyone is not included)."""
        return {r.id for r in self.roles}

    def text_channels(self) -> list[Channel]:
        """Channels a message can be posted in: text and announcement channels."""
        return [c for c in self.channels if c.type in (TEXT_CHANNEL, ANNOUNCEMENT_CHANNEL)]

    def categories(self) -> list[Channel]:
        """The server's categories."""
        return [c for c in self.channels if c.type == CATEGORY]

    def bot_top_position(self) -> int:
        """Position of JoltBot's highest role. It can only give or remove roles below that."""
        positions = [r.position for r in self.roles if r.id in self.bot_role_ids]
        return max(positions, default=0)

    def bot_permissions(self) -> int:
        """JoltBot's server-wide permissions: @everyone's plus those of each of its roles."""
        bits = self.everyone_permissions
        for role in self.roles:
            if role.id in self.bot_role_ids:
                bits |= role.permissions
        return bits


def _parse_roles(raw_roles: list[dict[str, Any]], guild_id: int) -> list[Role]:
    """Discord's role objects as ``Role``s, highest first, without @everyone."""
    return sorted(
        (
            Role(
                int(r["id"]),
                r["name"],
                r["position"],
                int(r["permissions"]),
                r.get("managed", False),
                r.get("color", 0),
            )
            for r in raw_roles
            if int(r["id"]) != guild_id
        ),
        key=lambda r: r.position,
        reverse=True,
    )


def _parse_channels(raw_channels: list[dict[str, Any]]) -> list[Channel]:
    """Discord's channel objects as ``Channel``s, in the order the Discord app shows them."""
    return sorted(
        (
            Channel(
                int(c["id"]),
                c["name"],
                c["type"],
                c.get("position", 0),
                int(c["parent_id"]) if c.get("parent_id") else None,
            )
            for c in raw_channels
        ),
        key=lambda c: (c.position, c.id),
    )


def _parse_emojis(raw_emojis: list[dict[str, Any]]) -> list[Emoji]:
    """The server's usable emojis, by name. Ones Discord marks unavailable are left out."""
    return sorted(
        (
            Emoji(int(e["id"]), e["name"], bool(e.get("animated")))
            for e in raw_emojis
            if e.get("id") and e.get("name") and e.get("available", True)
        ),
        key=lambda e: e.name.lower(),
    )


class DiscordClient:
    """Discord REST client for the dashboard, with per-server and per-member caches.

    One instance lives for the whole dashboard process (``app.state.discord``).
    Its caches are in memory, which is one reason the dashboard runs as a single
    worker: a second worker would have its own, separately ageing copies.

    Args:
        client_id: The application's OAuth2 client ID.
        client_secret: Its OAuth2 client secret.
        bot_token: The bot token, for calls made as JoltBot.
        redirect_uri: Where Discord sends the browser back after login.
        http: A ready-made HTTP client; tests pass a fake one.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        bot_token: str,
        redirect_uri: str,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        """Store the credentials and start with empty caches."""
        self.client_id = client_id
        self.client_secret = client_secret
        self.bot_token = bot_token
        self.redirect_uri = redirect_uri
        self.http = http or httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, headers={"User-Agent": "JoltBot dashboard"})
        self._cache: dict[int, GuildData] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._bot_user_id: int | None = None
        # (guild ID, user ID) -> (time fetched, that member's role IDs)
        self._member_roles: dict[tuple[int, int], tuple[float, frozenset[int]]] = {}

    async def aclose(self) -> None:
        """Close the HTTP client. Called once, when the dashboard shuts down."""
        await self.http.aclose()

    # ------------------------------------------------------------------ low level

    async def _request(self, method: str, path: str, *, auth: str, **kwargs: Any) -> Any:
        """One API call. Waits and retries once on a short rate limit."""
        url = path if path.startswith("http") else f"{API}{path}"
        for attempt in range(2):
            try:
                response = await self.http.request(method, url, headers={"Authorization": auth}, **kwargs)
            except httpx.HTTPError as e:
                raise DiscordError(f"Couldn't reach Discord ({e.__class__.__name__}).") from e
            # 429 = rate limited. Discord says how long to wait; a short wait is sat out once.
            if response.status_code == 429 and attempt == 0:
                retry_after = float(response.json().get("retry_after", 1))
                if retry_after <= MAX_RATE_LIMIT_WAIT_SECONDS:
                    await asyncio.sleep(retry_after)
                    continue
            if response.status_code == 401:
                raise DiscordUnauthorized("Discord says this login is no longer valid.", 401)
            if response.status_code >= 400:
                raise DiscordError(f"Discord returned an error ({response.status_code}).", response.status_code)
            return response.json()
        raise DiscordError("Discord is rate limiting us; try again in a moment.", 429)

    # ------------------------------------------------------------------ OAuth2 (as the user)

    def authorize_url(self, state: str) -> str:
        """Build Discord's OAuth authorize URL for dashboard login.

        Args:
            state: One-time CSRF value also stored in the short-lived state cookie.

        Returns:
            Full authorize URL including ``identify guilds`` scopes.
        """
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": OAUTH_SCOPES,
            "state": state,
            # TODO: prompt=none skips the consent screen; first-time or revoked users
            # may get opaque Discord errors instead of a re-consent UI.
            "prompt": "none",  # don't ask again if they already approved JoltBot
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code(self, code: str) -> dict[str, Any]:
        """Swap the one-time ?code= from the callback for an access token."""
        try:
            response = await self.http.post(
                f"{API}/oauth2/token",
                data={"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri},
                auth=(self.client_id, self.client_secret),
            )
        except httpx.HTTPError as e:
            raise DiscordError("Couldn't reach Discord.") from e
        if response.status_code != 200:
            raise DiscordError(f"Discord rejected the login ({response.status_code}).", response.status_code)
        return response.json()

    async def get_user(self, access_token: str) -> dict[str, Any]:
        """Who the logged-in user is (``GET /users/@me`` with their access token)."""
        return await self._request("GET", "/users/@me", auth=f"Bearer {access_token}")

    async def get_user_guilds(self, access_token: str) -> list[dict[str, Any]]:
        """The servers the logged-in user is in, each with their permissions there."""
        return await self._request("GET", "/users/@me/guilds", auth=f"Bearer {access_token}")

    # ------------------------------------------------------------------ bot token

    async def bot_user_id(self) -> int:
        """JoltBot's own user ID, fetched once and then remembered."""
        if self._bot_user_id is None:
            me = await self._request("GET", "/users/@me", auth=f"Bot {self.bot_token}")
            self._bot_user_id = int(me["id"])
        return self._bot_user_id

    async def guild_data(self, guild_id: int, *, refresh: bool = False) -> GuildData:
        """Roles, channels and JoltBot's roles for one server; cached for 60 seconds."""
        lock = self._locks.setdefault(guild_id, asyncio.Lock())
        async with lock:  # two tabs loading at once -> only one set of requests
            cached = self._cache.get(guild_id)
            if cached and not refresh and time.time() - cached.fetched_at < GUILD_CACHE_SECONDS:
                return cached
            auth = f"Bot {self.bot_token}"
            raw_roles = await self._request("GET", f"/guilds/{guild_id}/roles", auth=auth)
            raw_channels = await self._request("GET", f"/guilds/{guild_id}/channels", auth=auth)
            member = await self._request("GET", f"/guilds/{guild_id}/members/{await self.bot_user_id()}", auth=auth)
            raw_emojis = await self._request("GET", f"/guilds/{guild_id}/emojis", auth=auth)

            # The @everyone role has the same ID as the server itself.
            everyone = next((r for r in raw_roles if int(r["id"]) == guild_id), None)
            data = GuildData(
                guild_id,
                _parse_roles(raw_roles, guild_id),
                _parse_channels(raw_channels),
                int(everyone["permissions"]) if everyone else 0,
                [int(r) for r in member.get("roles", [])],
                emojis=_parse_emojis(raw_emojis),
            )
            self._cache[guild_id] = data
            return data

    def forget_guild(self, guild_id: int) -> None:
        """Drop one server's cached roles and channels, so the next page load fetches them again."""
        self._cache.pop(guild_id, None)

    async def member_role_ids(self, guild_id: int, user_id: int) -> frozenset[int]:
        """The roles one member holds in a server right now; cached for 60 seconds.

        Asked with the bot token, so it needs no extra permission from the user
        and can't be faked by them. Used to decide whether someone without
        Manage Server may use a server's dashboard.

        Args:
            guild_id: Server to look in. JoltBot must be in it.
            user_id: The Discord user.

        Returns:
            Their role IDs. Empty if they are not in that server (or JoltBot isn't).

        Raises:
            DiscordError: Discord couldn't be asked. The caller must not guess.
        """
        key = (guild_id, user_id)
        now = time.time()
        cached = self._member_roles.get(key)
        if cached and now - cached[0] < MEMBER_CACHE_SECONDS:
            return cached[1]
        try:
            member = await self._request("GET", f"/guilds/{guild_id}/members/{user_id}", auth=f"Bot {self.bot_token}")
        except DiscordError as e:
            if e.status not in (403, 404):  # 404: not a member. 403: JoltBot is not in that server.
                raise
            roles: frozenset[int] = frozenset()
        else:
            roles = frozenset(int(r) for r in member.get("roles", []))
        if len(self._member_roles) >= MEMBER_CACHE_MAX:
            self._member_roles = {k: v for k, v in self._member_roles.items() if now - v[0] < MEMBER_CACHE_SECONDS}
        self._member_roles[key] = (now, roles)
        return roles
