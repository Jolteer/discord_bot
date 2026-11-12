"""The optional per-server notices channel: new problems are posted there once.

Uses a real JoltBot object that never connects; Discord objects are fakes
(tests/fakes.py). Picking the channel on the dashboard is tested in test_web_access.py.
"""

import discord
import pytest

from joltbot.bot.client import NOTICE_CHANNEL_ISSUE, JoltBot
from joltbot.core.config import AppConfig
from joltbot.core.guild_settings import load_module, save_module
from joltbot.core.issues import clear_issue, list_issues, record_issue
from joltbot.modules.general.settings import GeneralSettings

from .conftest import GUILD_A, GUILD_B
from .fakes import TEST_ACTOR, FakeGuild, fake_category, fake_text_channel, http_error

NOTICES = 70
PROBLEM = "I can't post in #ticket-logs. JoltBot needs **Send Messages** there."


async def _set_notice_channel(db, guild_id, channel_id):
    async with db.session() as session, session.begin():
        current = await load_module(session, guild_id, "general")
        await save_module(
            session,
            guild_id,
            "general",
            enabled=True,
            config=GeneralSettings(notice_channel_id=channel_id),
            expected_version=current.version,
            actor=TEST_ACTOR,
        )


async def _issue_keys(db, guild_id=GUILD_A):
    async with db.session() as session:
        return {issue.key for issue in await list_issues(session, guild_id)}


@pytest.fixture
async def world(db, two_guilds):
    """A JoltBot in one server that has a #bot-notices channel picked on the dashboard."""
    guild = FakeGuild(GUILD_A)
    channel = fake_text_channel(guild, NOTICES, "bot-notices")
    await _set_notice_channel(db, GUILD_A, NOTICES)
    bot = JoltBot(AppConfig(_env_file=None, database_url=str(db.engine.url)))
    bot.get_guild = lambda guild_id: guild if guild_id == GUILD_A else None
    yield bot, guild, channel
    await bot.db.dispose()


# ------------------------------------------------------------------ what "new" means


async def test_record_issue_says_whether_the_problem_is_new(db, two_guilds):
    async def record():
        async with db.session() as session, session.begin():
            return await record_issue(session, GUILD_A, "tickets.log_channel", PROBLEM)

    assert await record() is True
    assert await record() is False  # a repeat: counted, not new
    async with db.session() as session, session.begin():
        await clear_issue(session, GUILD_A, "tickets.log_channel")
    assert await record() is True  # fixed (or dismissed) and then it happened again


async def test_record_issue_for_an_unknown_server_is_not_new(db):
    async with db.session() as session, session.begin():
        assert await record_issue(session, 12345, "tickets.log_channel", PROBLEM) is False


# ------------------------------------------------------------------ the bot posts the notice


async def test_a_new_problem_is_posted_once(world, db):
    bot, _, channel = world
    await bot.report_issue(GUILD_A, "tickets.log_channel", PROBLEM)
    await bot.report_issue(GUILD_A, "tickets.log_channel", PROBLEM)
    await bot.report_issue(GUILD_A, "tickets.log_channel", PROBLEM)

    channel.send.assert_awaited_once()
    text = channel.send.await_args.args[0]
    assert PROBLEM in text and text.startswith("⚠️ **JoltBot needs attention**")
    mentions = channel.send.await_args.kwargs["allowed_mentions"]
    assert (mentions.everyone, mentions.users, mentions.roles) == (False, False, False)
    assert await _issue_keys(db) == {"tickets.log_channel"}  # still on the dashboard too


async def test_different_problems_each_get_a_notice(world):
    bot, _, channel = world
    await bot.report_issue(GUILD_A, "tickets.log_channel", PROBLEM)
    await bot.report_issue(GUILD_A, "tickets.category", "The ticket category no longer exists.")
    assert channel.send.await_count == 2


async def test_a_problem_that_comes_back_after_being_fixed_is_posted_again(world):
    bot, _, channel = world
    await bot.report_issue(GUILD_A, "tickets.log_channel", PROBLEM)
    await bot.resolve_issue(GUILD_A, "tickets.log_channel")
    await bot.report_issue(GUILD_A, "tickets.log_channel", PROBLEM)
    assert channel.send.await_count == 2


async def test_no_notice_when_no_channel_is_picked(world, db):
    bot, _, channel = world
    await _set_notice_channel(db, GUILD_A, None)
    await bot.report_issue(GUILD_A, "tickets.log_channel", PROBLEM)
    channel.send.assert_not_awaited()
    assert await _issue_keys(db) == {"tickets.log_channel"}


async def test_long_problems_fit_in_one_discord_message(world):
    bot, _, channel = world
    await bot.report_issue(GUILD_A, "tickets.log_channel", "x" * 5000)
    assert len(channel.send.await_args.args[0]) <= 2000


@pytest.mark.parametrize("failure", [http_error(discord.Forbidden, 403), http_error(discord.HTTPException, 500)])
async def test_a_notice_that_cannot_be_posted_never_breaks_the_caller(world, db, failure):
    bot, _, channel = world
    channel.send.side_effect = failure
    await bot.report_issue(GUILD_A, "tickets.log_channel", PROBLEM)  # must not raise
    assert "tickets.log_channel" in await _issue_keys(db)


async def test_admins_are_told_on_the_dashboard_when_the_notices_channel_is_unusable(world, db):
    bot, guild, channel = world
    channel.send.side_effect = http_error(discord.Forbidden, 403)
    await bot.report_issue(GUILD_A, "tickets.log_channel", PROBLEM)
    assert await _issue_keys(db) == {"tickets.log_channel", NOTICE_CHANNEL_ISSUE}
    assert channel.send.await_count == 1  # the "can't post notices" warning is not itself posted

    # Once posting works again, that warning clears itself.
    channel.send.side_effect = None
    await bot.report_issue(GUILD_A, "tickets.category", "The ticket category no longer exists.")
    assert await _issue_keys(db) == {"tickets.log_channel", "tickets.category"}


@pytest.mark.parametrize("replacement", ["deleted", "category"])
async def test_a_notices_channel_that_is_gone_is_reported_on_the_dashboard(world, db, replacement):
    bot, guild, channel = world
    guild.remove_channel(NOTICES)
    if replacement == "category":
        fake_category(guild, NOTICES)  # same ID, but not somewhere a message can go
    await bot.report_issue(GUILD_A, "tickets.log_channel", PROBLEM)
    assert await _issue_keys(db) == {"tickets.log_channel", NOTICE_CHANNEL_ISSUE}


async def test_notices_never_leak_to_another_server(world, db):
    bot, _, channel = world
    await bot.report_issue(GUILD_B, "tickets.log_channel", PROBLEM)  # B has no notices channel
    channel.send.assert_not_awaited()
    assert await _issue_keys(db, GUILD_B) == {"tickets.log_channel"}


def test_notice_channel_setting_defaults_to_off_and_is_stored_as_a_string():
    assert GeneralSettings().notice_channel_id is None
    assert GeneralSettings(notice_channel_id="").notice_channel_id is None
    picked = GeneralSettings(notice_channel_id="900000000000000031")
    assert picked.notice_channel_id == 900000000000000031
    assert picked.model_dump(mode="json")["notice_channel_id"] == "900000000000000031"
