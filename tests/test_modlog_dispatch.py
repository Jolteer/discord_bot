"""The per-channel posting queue: order, batching, overflow, and channels JoltBot can't post in."""

from __future__ import annotations

import discord
import pytest

from joltbot.modules.modlog import dispatch
from joltbot.modules.modlog.constants import CATEGORY_MEMBERS, CATEGORY_MESSAGES, channel_issue_key
from joltbot.modules.modlog.dispatch import Dispatcher

from .conftest import GUILD_A
from .fakes import FakeBot, FakeGuild, fake_text_channel, http_error

LOG, OTHER_LOG = 501, 502


@pytest.fixture
def guild():
    guild = FakeGuild(GUILD_A)
    fake_text_channel(guild, LOG, "log")
    fake_text_channel(guild, OTHER_LOG, "other-log")
    return guild


@pytest.fixture
async def dispatcher(guild):
    dispatcher = Dispatcher(FakeBot(None, [guild]), pause=0)  # type: ignore[arg-type]
    yield dispatcher
    await dispatcher.close()


def embed(text: str = "x") -> discord.Embed:
    return discord.Embed(title=text)


def messages(guild, channel_id: int = LOG) -> list[list[str]]:
    """The embed titles of each message sent to a channel."""
    calls = guild.get_channel(channel_id).send.await_args_list
    return [[e.title for e in call.kwargs["embeds"]] for call in calls if "embeds" in call.kwargs]


async def test_embeds_arrive_in_order_and_cannot_ping_anyone(guild, dispatcher):
    for n in range(3):
        dispatcher.enqueue(GUILD_A, LOG, CATEGORY_MEMBERS, embed(str(n)))
    await dispatcher.drain()
    assert [title for message in messages(guild) for title in message] == ["0", "1", "2"]
    mentions = guild.get_channel(LOG).send.await_args.kwargs["allowed_mentions"]
    assert not (mentions.everyone or mentions.users or mentions.roles)


async def test_a_burst_is_sent_as_few_messages_of_at_most_ten_embeds(guild, dispatcher):
    for n in range(25):
        dispatcher.enqueue(GUILD_A, LOG, CATEGORY_MEMBERS, embed(str(n)))
    await dispatcher.drain()
    assert [len(message) for message in messages(guild)] == [10, 10, 5]
    assert [title for message in messages(guild) for title in message] == [str(n) for n in range(25)]


async def test_a_message_never_carries_more_than_6000_characters_of_embeds(guild, dispatcher):
    for n in range(4):
        dispatcher.enqueue(GUILD_A, LOG, CATEGORY_MEMBERS, discord.Embed(title=str(n), description="d" * 2500))
    await dispatcher.drain()
    assert messages(guild) == [["0", "1"], ["2", "3"]]


async def test_each_channel_has_its_own_queue(guild, dispatcher):
    dispatcher.enqueue(GUILD_A, LOG, CATEGORY_MEMBERS, embed("a"))
    dispatcher.enqueue(GUILD_A, OTHER_LOG, CATEGORY_MESSAGES, embed("b"))
    await dispatcher.drain()
    assert messages(guild, LOG) == [["a"]]
    assert messages(guild, OTHER_LOG) == [["b"]]


async def test_a_full_queue_drops_new_posts_and_says_how_many(guild, dispatcher, monkeypatch):
    monkeypatch.setattr(dispatch, "QUEUE_MAX", 5)
    for n in range(8):
        dispatcher.enqueue(GUILD_A, LOG, CATEGORY_MEMBERS, embed(str(n)))  # never blocks, never raises
    await dispatcher.drain()
    assert messages(guild) == [["0", "1", "2", "3", "4"]]
    notice = guild.get_channel(LOG).send.await_args_list[-1].args[0]
    assert notice.startswith("⚠️ 3 log posts skipped")


async def test_a_channel_joltbot_cannot_post_in_becomes_one_dashboard_issue(guild, dispatcher):
    channel = guild.get_channel(LOG)
    channel.send.side_effect = http_error(discord.Forbidden, 403)
    dispatcher.enqueue(GUILD_A, LOG, CATEGORY_MEMBERS, embed("a"))
    await dispatcher.drain()
    dispatcher.enqueue(GUILD_A, LOG, CATEGORY_MEMBERS, embed("b"))
    await dispatcher.drain()
    assert dispatcher.bot.issues == [(GUILD_A, channel_issue_key(CATEGORY_MEMBERS))]  # reported once, not per post

    channel.send.side_effect = None  # permissions fixed
    dispatcher.enqueue(GUILD_A, LOG, CATEGORY_MEMBERS, embed("c"))
    await dispatcher.drain()
    assert dispatcher.bot.resolved == [(GUILD_A, channel_issue_key(CATEGORY_MEMBERS))]


async def test_a_deleted_log_channel_becomes_a_dashboard_issue(guild, dispatcher):
    guild.remove_channel(LOG)
    dispatcher.enqueue(GUILD_A, LOG, CATEGORY_MESSAGES, embed("a"))
    await dispatcher.drain()
    assert dispatcher.bot.issues == [(GUILD_A, channel_issue_key(CATEGORY_MESSAGES))]


async def test_a_discord_error_drops_that_batch_and_the_queue_keeps_going(guild, dispatcher):
    channel = guild.get_channel(LOG)
    channel.send.side_effect = http_error(discord.HTTPException, 500)
    dispatcher.enqueue(GUILD_A, LOG, CATEGORY_MEMBERS, embed("lost"))
    await dispatcher.drain()
    channel.send.side_effect = None
    dispatcher.enqueue(GUILD_A, LOG, CATEGORY_MEMBERS, embed("kept"))
    await dispatcher.drain()
    assert messages(guild)[-1] == ["kept"]
    assert dispatcher.bot.issues == []
