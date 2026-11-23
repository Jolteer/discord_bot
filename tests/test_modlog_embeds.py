"""Event -> embed formatting: what a log post shows, and that it always fits Discord's limits."""

from __future__ import annotations

from datetime import datetime, timezone

import discord
import pytest

from joltbot.core.limits import EMBED_DESCRIPTION_MAX, EMBED_FIELD_VALUE_MAX
from joltbot.modules.modlog.constants import (
    EVENT_BAN,
    EVENT_BULK_DELETE,
    EVENT_CHANNEL_DELETE,
    EVENT_KICK,
    EVENT_MEMBER_JOIN,
    EVENT_MESSAGE_DELETE,
    EVENT_MESSAGE_EDIT,
    EVENT_NICKNAME,
    EVENT_ROLE_UPDATE,
    EVENT_SERVER_UPDATE,
    EVENT_TIMEOUT_ADD,
    EVENTS,
    TARGET_CHANNEL,
    TARGET_OTHER,
    TARGET_ROLE,
)
from joltbot.modules.modlog.embeds import NOT_IN_MEMORY, SHORTENED_FOOTER, build_embed
from joltbot.modules.modlog.events import LogEvent

from .modlog_helpers import CHAT, MOD_ID, field

GUILD, USER = 1, 7
WHEN = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)


def event(key: str, **fields) -> LogEvent:
    return LogEvent(GUILD, key, **{"target_id": USER, "target_name": "alice", "created_at": WHEN, **fields})


def test_title_colour_and_timestamp_come_from_the_event():
    embed = build_embed(event(EVENT_BAN))
    assert embed.title == "Member banned"
    assert embed.colour == discord.Colour.dark_red()
    assert embed.timestamp == WHEN
    assert build_embed(event(EVENT_MEMBER_JOIN)).colour == discord.Colour.green()
    assert build_embed(event(EVENT_KICK)).colour == discord.Colour.orange()
    assert build_embed(event(EVENT_MESSAGE_EDIT)).colour == discord.Colour.blue()


def test_every_event_type_builds_an_embed():
    for key in EVENTS:
        assert build_embed(event(key)).title


def test_target_and_actor_show_a_mention_the_name_and_the_raw_id():
    embed = build_embed(event(EVENT_BAN, actor_id=MOD_ID, actor_name="mod_bob", reason="spam"))
    assert field(embed, "Member") == f"<@{USER}> · alice · `{USER}`"
    assert field(embed, "By") == f"<@{MOD_ID}> · mod\\_bob · `{MOD_ID}`"  # markdown in names is escaped
    assert field(embed, "Reason") == "spam"


def test_roles_and_channels_are_mentioned_their_own_way():
    role = build_embed(event(EVENT_ROLE_UPDATE, target_id=5, target_name="Staff", target_kind=TARGET_ROLE))
    assert field(role, "Role") == "<@&5> · Staff · `5`"
    channel = build_embed(event(EVENT_CHANNEL_DELETE, target_id=6, target_name="old", target_kind=TARGET_CHANNEL))
    assert field(channel, "Channel") == "<#6> · old · `6`"


def test_an_event_about_the_server_itself_has_no_target_field():
    embed = build_embed(LogEvent(GUILD, EVENT_SERVER_UPDATE, target_kind=TARGET_OTHER, created_at=WHEN))
    assert [f.name for f in embed.fields] == []


def test_an_unknown_actor_is_said_in_words():
    embed = build_embed(event(EVENT_NICKNAME, actor_note="unknown (missing View Audit Log)"))
    assert field(embed, "By") == "unknown (missing View Audit Log)"
    assert field(build_embed(event(EVENT_MEMBER_JOIN)), "By") is None  # nobody "does" a join


def test_changes_show_old_and_new_and_lists_show_only_the_new_side():
    log_event = event(EVENT_NICKNAME)
    log_event.add_change("Nickname", "none", "Al")
    log_event.add_change("Roles added", "", "@Staff, @Helper")
    embed = build_embed(log_event)
    assert field(embed, "Nickname") == "none → Al"
    assert field(embed, "Roles added") == "@Staff, @Helper"


def test_a_time_is_shown_as_a_discord_timestamp():
    log_event = event(EVENT_TIMEOUT_ADD)
    log_event.add_info("Duration", "1 hour")
    log_event.add_time("Until", WHEN)
    embed = build_embed(log_event)
    assert field(embed, "Duration") == "1 hour"
    assert field(embed, "Until") == f"<t:{int(WHEN.timestamp())}:F> (<t:{int(WHEN.timestamp())}:R>)"


def test_a_deleted_message_shows_text_author_channel_attachments_and_who_deleted_it():
    log_event = event(EVENT_MESSAGE_DELETE, channel_id=CHAT, actor_note="author or unknown")
    log_event.extra.update(
        target_label="Author",
        actor_label="Deleted by",
        channel_name="general",
        content="the secret",
        cached=True,
        attachments=[{"filename": "cat.png", "url": "https://cdn.discordapp.com/attachments/1/2/cat.png"}],
    )
    embed = build_embed(log_event)
    assert embed.description == "the secret"
    assert field(embed, "Author") == f"<@{USER}> · alice · `{USER}`"
    assert field(embed, "Channel") == f"<#{CHAT}> · general · `{CHAT}`"
    assert field(embed, "Deleted by") == "author or unknown"
    assert field(embed, "Attachments") == "[cat.png](https://cdn.discordapp.com/attachments/1/2/cat.png)"


def test_a_deleted_message_that_was_not_in_memory_says_so():
    log_event = LogEvent(GUILD, EVENT_MESSAGE_DELETE, channel_id=CHAT, created_at=WHEN)
    log_event.extra["cached"] = False
    embed = build_embed(log_event)
    assert embed.description == NOT_IN_MEMORY
    assert field(embed, "Author") is None


def test_an_edit_shows_before_after_and_a_jump_link():
    log_event = event(EVENT_MESSAGE_EDIT, channel_id=CHAT)
    log_event.extra.update(before="helo", after="hello", jump_url="https://discord.com/channels/1/600/9001")
    embed = build_embed(log_event)
    assert embed.description == "[Jump to message](https://discord.com/channels/1/600/9001)"
    assert (field(embed, "Before"), field(embed, "After")) == ("helo", "hello")

    log_event.extra["before"] = None  # edited, but the old text wasn't in memory
    assert field(build_embed(log_event), "Before") == NOT_IN_MEMORY


def test_a_bulk_delete_lists_the_cached_messages_under_the_count():
    log_event = LogEvent(GUILD, EVENT_BULK_DELETE, channel_id=CHAT, created_at=WHEN)
    log_event.extra.update(
        count=12,
        messages=[{"author": "alice", "content": "one\ntwo"}, {"author": "bob", "content": ""}],
    )
    description = build_embed(log_event).description
    assert description.startswith("**12** messages deleted. The 2 oldest still in memory:")
    assert "**alice**: one two" in description  # one line per message
    assert "**bob**: *no text*" in description

    log_event.extra["messages"] = []
    assert "isn't available" in build_embed(log_event).description


@pytest.mark.parametrize("size", [5_000, 50_000])
def test_long_text_is_cut_to_fit_every_limit(size):
    long = "x" * size
    deleted = event(EVENT_MESSAGE_DELETE, reason=long, actor_note="author or unknown")
    deleted.extra.update(content=long, cached=True)
    edited = event(EVENT_MESSAGE_EDIT)
    edited.extra.update(before=long, after=long)
    bulk = LogEvent(GUILD, EVENT_BULK_DELETE, created_at=WHEN)
    bulk.extra.update(count=100, messages=[{"author": "a" * 200, "content": long} for _ in range(20)])
    changed = event(EVENT_ROLE_UPDATE, target_name=long)
    for i in range(40):
        changed.add_change(f"Field {i}", long, long)
        changed.add_info(f"Info {i}", long)

    for log_event in (deleted, edited, bulk, changed):
        embed = build_embed(log_event)
        assert len(embed) <= 6000
        assert len(embed.description or "") <= EMBED_DESCRIPTION_MAX
        assert len(embed.fields) <= 25
        assert all(len(f.value) <= EMBED_FIELD_VALUE_MAX for f in embed.fields)


def test_an_embed_that_had_to_lose_fields_says_so():
    changed = event(EVENT_ROLE_UPDATE)
    for i in range(10):
        changed.add_change(f"Field {i}", "o" * 1000, "n" * 1000)
    embed = build_embed(changed)
    assert len(embed) <= 6000
    assert embed.footer.text == SHORTENED_FOOTER
    assert build_embed(event(EVENT_ROLE_UPDATE)).footer.text is None


def test_more_than_ten_changes_are_counted_not_listed():
    changed = event(EVENT_ROLE_UPDATE)
    for i in range(13):
        changed.add_change(f"Field {i}", "a", "b")
    assert field(build_embed(changed), "More changes").startswith("+3 more")


def test_the_stored_details_are_json_safe_and_keep_the_names():
    log_event = event(EVENT_BAN, actor_id=MOD_ID, actor_name="mod", reason="spam")
    log_event.add_change("Nickname", "a", "b")
    details = log_event.details()
    assert details["target_name"] == "alice" and details["actor_name"] == "mod"
    assert details["changes"] == [{"field": "Nickname", "old": "a", "new": "b"}]
    assert "actor_note" not in details
