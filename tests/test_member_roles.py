"""DiscordClient.member_role_ids: the lookup behind dashboard access roles.

Discord is replaced by an httpx mock transport, so these run offline.
"""

import httpx
import pytest

from joltbot.core import discord_rest
from joltbot.core.discord_rest import MEMBER_CACHE_SECONDS, DiscordClient, DiscordError

GUILD, USER = 111, 222


class Discord:
    """Answers GET /guilds/{id}/members/{user} and remembers what it was asked."""

    def __init__(self):
        self.requests = []
        self.status = 200
        self.roles = ["5", "7"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "no"})
        return httpx.Response(200, json={"user": {"id": str(USER)}, "roles": self.roles})


@pytest.fixture
def discord():
    return Discord()


@pytest.fixture
async def client(discord):
    http = httpx.AsyncClient(transport=httpx.MockTransport(discord))
    api = DiscordClient(client_id="1", client_secret="s", bot_token="bot-token", redirect_uri="http://x", http=http)
    yield api
    await api.aclose()


@pytest.fixture
def clock(monkeypatch):
    """A clock the test moves by hand."""
    now = [1000.0]
    monkeypatch.setattr(discord_rest.time, "time", lambda: now[0])
    return now


async def test_roles_are_asked_for_with_the_bot_token(client, discord):
    assert await client.member_role_ids(GUILD, USER) == {5, 7}
    (request,) = discord.requests
    assert request.url.path == f"/api/v10/guilds/{GUILD}/members/{USER}"
    assert request.headers["authorization"] == "Bot bot-token"


async def test_roles_are_cached_for_a_minute_and_then_asked_again(client, discord, clock):
    assert await client.member_role_ids(GUILD, USER) == {5, 7}
    discord.roles = ["5"]  # role 7 is taken away in Discord

    clock[0] += MEMBER_CACHE_SECONDS - 1
    assert await client.member_role_ids(GUILD, USER) == {5, 7}  # still the cached answer
    assert len(discord.requests) == 1

    clock[0] += 2
    assert await client.member_role_ids(GUILD, USER) == {5}  # the removal is seen within the minute
    assert len(discord.requests) == 2


async def test_each_member_and_server_is_cached_separately(client, discord):
    await client.member_role_ids(GUILD, USER)
    await client.member_role_ids(GUILD, USER + 1)
    await client.member_role_ids(GUILD + 1, USER)
    assert len(discord.requests) == 3


@pytest.mark.parametrize("status", [403, 404])
async def test_someone_who_is_not_in_the_server_has_no_roles(client, discord, status):
    discord.status = status
    assert await client.member_role_ids(GUILD, USER) == frozenset()


async def test_a_discord_failure_is_an_error_and_is_not_cached(client, discord):
    discord.status = 500
    with pytest.raises(DiscordError):
        await client.member_role_ids(GUILD, USER)
    discord.status = 200
    assert await client.member_role_ids(GUILD, USER) == {5, 7}
