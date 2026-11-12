"""Slash-command registration and the replies people get when a command fails.

Uses a real JoltBot object that never connects: Discord's sync call is mocked.
"""

from unittest.mock import AsyncMock

import discord
import pytest
from discord import app_commands

from joltbot.bot.client import CLEAR_GUILD_COMMANDS_KEY, COMMANDS_HASH_KEY, JoltBot
from joltbot.core.config import AppConfig
from joltbot.core.models import BotMeta

from .fakes import fake_interaction, http_error

GONE_GUILD, OLD_GUILD, FLAKY_GUILD = 31, 32, 33


@pytest.fixture
async def bot(db):
    """A JoltBot on the test database. ``bot.synced`` lists every sync call: None = global, else a guild ID."""
    bot = JoltBot(AppConfig(_env_file=None, database_url=str(db.engine.url)))
    bot.synced = []
    bot.sync_errors = {}  # guild ID (or None for global) -> error to raise

    async def sync(*, guild=None):
        key = guild.id if guild is not None else None
        bot.synced.append(key)
        if key in bot.sync_errors:
            raise bot.sync_errors[key]
        return []

    bot.tree.sync = AsyncMock(side_effect=sync)
    yield bot
    await bot.db.dispose()


async def _meta(db, key):
    async with db.session() as session:
        row = await session.get(BotMeta, key)
    return row.value if row is not None else None


async def _set_clear_list(db, value):
    async with db.session() as session, session.begin():
        await session.merge(BotMeta(key=CLEAR_GUILD_COMMANDS_KEY, value=value))


async def test_commands_are_registered_once_and_then_left_alone(db, bot):
    await bot.sync_commands()
    assert bot.synced == [None]
    assert await _meta(db, COMMANDS_HASH_KEY)

    await bot.sync_commands()  # e.g. the next restart: nothing changed, so Discord isn't called
    assert bot.synced == [None]


async def test_old_per_server_commands_are_cleared_once(db, bot):
    await _set_clear_list(db, str(OLD_GUILD))
    await bot.sync_commands()
    assert bot.synced == [None, OLD_GUILD]
    assert await _meta(db, CLEAR_GUILD_COMMANDS_KEY) is None

    await bot.sync_commands()
    assert bot.synced == [None, OLD_GUILD]


@pytest.mark.parametrize("refusal", [http_error(discord.Forbidden, 403), http_error(discord.NotFound, 404)])
async def test_a_server_the_bot_left_cannot_block_registration(db, bot, refusal):
    await _set_clear_list(db, f"{GONE_GUILD},{OLD_GUILD},not-a-number")
    bot.sync_errors[GONE_GUILD] = refusal

    await bot.sync_commands()  # must not raise: retrying can't fix a server we're not in

    assert bot.synced == [None, GONE_GUILD, OLD_GUILD]  # global first, and the good server still cleared
    assert await _meta(db, COMMANDS_HASH_KEY)
    assert await _meta(db, CLEAR_GUILD_COMMANDS_KEY) is None  # the dead ID is dropped for good


async def test_a_discord_hiccup_while_clearing_is_retried_for_that_server_only(db, bot):
    await _set_clear_list(db, f"{OLD_GUILD},{FLAKY_GUILD}")
    bot.sync_errors[FLAKY_GUILD] = http_error(discord.HTTPException, 503)

    with pytest.raises(discord.HTTPException):  # the caller's retry loop tries again later
        await bot.sync_commands()
    assert await _meta(db, COMMANDS_HASH_KEY)  # global commands are registered regardless
    assert await _meta(db, CLEAR_GUILD_COMMANDS_KEY) == str(FLAKY_GUILD)

    del bot.sync_errors[FLAKY_GUILD]
    await bot.sync_commands()
    assert bot.synced == [None, OLD_GUILD, FLAKY_GUILD, FLAKY_GUILD]  # no second global sync, no repeat for OLD_GUILD
    assert await _meta(db, CLEAR_GUILD_COMMANDS_KEY) is None


async def test_failed_global_registration_is_not_recorded_as_done(db, bot):
    bot.sync_errors[None] = http_error(discord.HTTPException, 503)
    with pytest.raises(discord.HTTPException):
        await bot.sync_commands()
    assert await _meta(db, COMMANDS_HASH_KEY) is None


@pytest.mark.parametrize(
    ("error", "reply"),
    [
        (
            app_commands.BotMissingPermissions(["manage_roles", "kick_members"]),
            "I'm missing permissions: Manage Roles, Kick Members.",
        ),
        (app_commands.CheckFailure("You need Manage Server (or a staff role) to use this."), None),
        (app_commands.CheckFailure(), "You can't use this command."),
        (
            app_commands.AppCommandError("boom"),
            "Something went wrong running that command. Please try again in a moment.",
        ),
    ],
)
async def test_failed_commands_get_a_private_reply(bot, error, reply):
    interaction = fake_interaction(None, None)
    await bot.tree.on_error(interaction, error)
    interaction.response.send_message.assert_awaited_once_with(reply or str(error), ephemeral=True)
