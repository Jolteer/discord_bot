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

log = logging.getLogger(__name__)

API = "https://discord.com/api/v10"
AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
OAUTH_SCOPES = "identify guilds"
GUILD_CACHE_SECONDS = 60

# Discord channel types we care about
TEXT_CHANNEL, CATEGORY, ANNOUNCEMENT_CHANNEL = 0, 4, 5


class DiscordError(Exception):
    """Discord returned an error or couldn't be reached."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class DiscordUnauthorized(DiscordError):
    """The access token is no longer valid (expired or revoked): log in again."""


@dataclass(frozen=True)
class Role:
    id: int
    name: str
    position: int
    permissions: int
    managed: bool
    color: int


@dataclass(frozen=True)
class Channel:
    id: int
    name: str
    type: int
    position: int
    parent_id: int | None


@dataclass(frozen=True)
class GuildData:
    """Everything the dashboard needs about one server, fetched with the bot token."""

    guild_id: int
    roles: list[Role]  # highest first, without @everyone
    channels: list[Channel]
    everyone_permissions: int
    bot_role_ids: list[int]
    fetched_at: float = field(default_factory=time.time)

    @property
    def role_ids(self) -> set[int]:
        return {r.id for r in self.roles}

    def text_channels(self) -> list[Channel]:
        return [c for c in self.channels if c.type in (TEXT_CHANNEL, ANNOUNCEMENT_CHANNEL)]

    def categories(self) -> list[Channel]:
        return [c for c in self.channels if c.type == CATEGORY]

    def bot_top_position(self) -> int:
        positions = [r.position for r in self.roles if r.id in self.bot_role_ids]
        return max(positions, default=0)

    def bot_permissions(self) -> int:
        """JoltBot's server-wide permissions: @everyone's plus those of each of its roles."""
        bits = self.everyone_permissions
        for role in self.roles:
            if role.id in self.bot_role_ids:
                bits |= role.permissions
        return bits


class DiscordClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        bot_token: str,
        redirect_uri: str,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.bot_token = bot_token
        self.redirect_uri = redirect_uri
        self.http = http or httpx.AsyncClient(timeout=10, headers={"User-Agent": "JoltBot dashboard"})
        self._cache: dict[int, GuildData] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._bot_user_id: int | None = None

    async def aclose(self) -> None:
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
            if response.status_code == 429 and attempt == 0:
                retry_after = float(response.json().get("retry_after", 1))
                if retry_after <= 5:
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
        return await self._request("GET", "/users/@me", auth=f"Bearer {access_token}")

    async def get_user_guilds(self, access_token: str) -> list[dict[str, Any]]:
        return await self._request("GET", "/users/@me/guilds", auth=f"Bearer {access_token}")

    # ------------------------------------------------------------------ bot token

    async def bot_user_id(self) -> int:
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

            everyone = next((r for r in raw_roles if int(r["id"]) == guild_id), None)
            roles = sorted(
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
            channels = sorted(
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
            data = GuildData(
                guild_id,
                roles,
                channels,
                int(everyone["permissions"]) if everyone else 0,
                [int(r) for r in member.get("roles", [])],
            )
            self._cache[guild_id] = data
            return data

    def forget_guild(self, guild_id: int) -> None:
        self._cache.pop(guild_id, None)
