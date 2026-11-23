"""The mod log end to end, offline: a Discord event in, one saved entry and one log post out.

Covers the two promises that are easy to break: one change gives exactly one
entry (never two), and an actor is only named when the audit log leaves no doubt.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import discord
import pytest

from joltbot.core.types import utcnow
from joltbot.modules.modlog.constants import (
    EVENT_BAN,
    EVENT_BULK_DELETE,
    EVENT_CHANNEL_UPDATE,
    EVENT_KICK,
    EVENT_MEMBER_JOIN,
    EVENT_MEMBER_LEAVE,
    EVENT_MEMBER_ROLES,
    EVENT_MESSAGE_DELETE,
    EVENT_MESSAGE_EDIT,
    EVENT_NICKNAME,
    EVENT_ROLE_UPDATE,
    EVENT_TIMEOUT_ADD,
    EVENT_TIMEOUT_REMOVE,
    EVENT_VOICE_JOIN,
    EVENT_VOICE_LEAVE,
    EVENT_VOICE_MOVE,
    EVENT_WEBHOOK,
    ISSUE_VIEW_AUDIT_LOG,
)

from .conftest import GUILD_A, GUILD_B
from .fakes import FakeRole, fake_member
from .modlog_helpers import (
    CHAT,
    MEMBERS_LOG,
    MESSAGES_LOG,
    MOD_ID,
    MODERATION_LOG,
    SERVER_LOG,
    Diff,
    Server,
    enable_modlog,
    field,
    message,
)

Action = discord.AuditLogAction
USER = 7


@pytest.fixture
async def server(db, two_guilds):
    await enable_modlog(db, GUILD_A)
    server = Server(db, GUILD_A)
    fake_member(server.guild, MOD_ID, name="mod")
    yield server
    await server.manager.close()


def member_pair(server: Server, **changed):
    """The same member before and after an update; ``changed`` is set on the "after" one."""
    before = fake_member(server.guild, USER, name="alice", register=False)
    after = fake_member(server.guild, USER, name="alice")
    for name, value in changed.items():
        setattr(after, name, value)
    return before, after


def delete_payload(cached=None, *, message_id: int = 9001, channel_id: int = CHAT):
    return SimpleNamespace(guild_id=GUILD_A, channel_id=channel_id, message_id=message_id, cached_message=cached)


def message_delete_entry(author_id: int = USER, *, channel_id: int = CHAT, count: int = 1) -> dict:
    return {"target_id": author_id, "extra": SimpleNamespace(channel=SimpleNamespace(id=channel_id), count=count)}


# --------------------------------------------------------------------------- one change, one entry


async def test_a_nickname_change_is_one_entry_with_the_actor_from_the_audit_log(server):
    before, after = member_pair(server, nick="Al")
    await server.audit(Action.member_update, target_id=USER, after=Diff(nick="Al"), reason="asked nicely")
    await server.cog.on_member_update(before, after)

    (entry,) = await server.entries()  # exactly one, although both the audit log and the gateway reported it
    assert (entry.event_type, entry.category) == (EVENT_NICKNAME, "members")
    assert (entry.target_id, entry.actor_id, entry.reason) == (USER, MOD_ID, "asked nicely")
    assert entry.details["changes"] == [{"field": "Nickname", "old": "none", "new": "Al"}]
    (embed,) = await server.posted(MEMBERS_LOG)
    assert field(embed, "By") == f"<@{MOD_ID}> · mod · `{MOD_ID}`"
    assert field(embed, "Nickname") == "none → Al"


async def test_an_audit_entry_alone_creates_nothing_for_gateway_owned_events(server):
    await server.audit(Action.member_update, target_id=USER, after=Diff(nick="Al"))
    await server.audit(Action.member_role_update, target_id=USER, after=Diff(roles=[]))
    await server.audit(Action.ban, target_id=USER)
    await server.audit(Action.role_update, target_id=5, after=Diff(name="x"))
    await server.audit(Action.message_delete, **message_delete_entry())
    assert await server.entries() == []


async def test_the_audit_entry_may_arrive_after_the_gateway_event(server):
    before, after = member_pair(server, nick="Al")

    async def audit_arrives_during_the_wait():
        await server.audit(Action.member_update, target_id=USER, after=Diff(nick="Al"))

    server.manager.settle = audit_arrives_during_the_wait
    await server.cog.on_member_update(before, after)
    (entry,) = await server.entries()
    assert entry.actor_id == MOD_ID


async def test_a_timeout_entry_is_not_mistaken_for_a_nickname_entry(server):
    # Both are "member_update" in the audit log; only the changed attribute tells them apart.
    before, after = member_pair(server, nick="Al")
    await server.audit(Action.member_update, target_id=USER, after=Diff(timed_out_until=utcnow()))
    await server.cog.on_member_update(before, after)
    (entry,) = await server.entries()
    assert entry.actor_id is None and entry.details["actor_note"] == "unknown"


async def test_roles_added_and_removed_are_one_entry(server):
    staff, helper = FakeRole(70, "Staff"), FakeRole(71, "Helper")
    before, after = member_pair(server)
    before.roles, after.roles = [helper], [staff]
    await server.audit(Action.member_role_update, target_id=USER, after=Diff(roles=[staff]))
    await server.cog.on_member_update(before, after)
    (entry,) = await server.entries()
    assert (entry.event_type, entry.actor_id) == (EVENT_MEMBER_ROLES, MOD_ID)
    assert entry.details["changes"] == [
        {"field": "Roles added", "old": "", "new": "@Staff"},
        {"field": "Roles removed", "old": "", "new": "@Helper"},
    ]


async def test_a_timeout_and_its_removal(server):
    until = utcnow() + timedelta(hours=1)
    before, after = member_pair(server, timed_out_until=until)
    await server.audit(Action.member_update, target_id=USER, after=Diff(timed_out_until=until), reason="cool off")
    await server.cog.on_member_update(before, after)
    await server.cog.on_member_update(after, before)  # a moderator removes it early

    applied, removed = await server.entries()
    assert (applied.event_type, applied.category, applied.reason) == (EVENT_TIMEOUT_ADD, "moderation", "cool off")
    assert {"name": "Duration", "value": "1 hour"} in applied.details["info"]
    assert removed.event_type == EVENT_TIMEOUT_REMOVE
    assert len(await server.posted(MODERATION_LOG)) == 2


async def test_a_member_update_with_nothing_we_track_is_ignored(server):
    before, after = member_pair(server)
    await server.cog.on_member_update(before, after)
    assert await server.entries() == []


# --------------------------------------------------------------------------- joins, leaves, kicks, bans


async def test_a_join_shows_the_account_age_and_flags_a_new_account(server):
    old = fake_member(server.guild, USER, name="alice")
    new = fake_member(server.guild, 8, name="fresh")
    new.created_at = utcnow() - timedelta(days=2)
    await server.cog.on_member_join(old)
    await server.cog.on_member_join(new)

    first, second = await server.entries()
    assert first.event_type == second.event_type == EVENT_MEMBER_JOIN
    assert "new_account" not in first.details
    assert second.details["new_account"] is True
    plain, flagged = await server.posted(MEMBERS_LOG)
    assert plain.title == "Member joined" and field(plain, "Account created").startswith("<t:")
    assert flagged.title == "Member joined (new account)"
    assert field(flagged, "⚠️ New account") == "Created 2 days ago"


async def test_someone_leaving_is_a_leave(server):
    member = fake_member(server.guild, USER, name="alice", roles=[FakeRole(70, "Staff")])
    await server.cog.on_raw_member_remove(SimpleNamespace(guild_id=GUILD_A, user=member))
    (entry,) = await server.entries()
    assert entry.event_type == EVENT_MEMBER_LEAVE
    assert {"name": "Roles", "value": "@Staff"} in entry.details["info"]


async def test_a_kick_is_one_moderation_entry_and_no_leave(server):
    member = fake_member(server.guild, USER, name="alice")
    await server.audit(Action.kick, target_id=USER, reason="spam")
    await server.cog.on_raw_member_remove(SimpleNamespace(guild_id=GUILD_A, user=member))
    (entry,) = await server.entries()
    assert (entry.event_type, entry.actor_id, entry.reason) == (EVENT_KICK, MOD_ID, "spam")
    assert await server.posted(MEMBERS_LOG) == []


async def test_a_ban_is_one_moderation_entry_and_no_leave(server):
    member = fake_member(server.guild, USER, name="alice")
    await server.audit(Action.ban, target_id=USER, reason="raid")
    await server.cog.on_member_ban(server.guild, member)
    await server.cog.on_raw_member_remove(SimpleNamespace(guild_id=GUILD_A, user=member))
    (entry,) = await server.entries()
    assert (entry.event_type, entry.actor_id, entry.reason) == (EVENT_BAN, MOD_ID, "raid")


async def test_a_kick_shows_as_a_leave_when_kicks_are_not_being_logged(server):
    await enable_modlog(server.db, GUILD_A, events={EVENT_KICK: False})
    member = fake_member(server.guild, USER, name="alice")
    await server.audit(Action.kick, target_id=USER)
    await server.cog.on_raw_member_remove(SimpleNamespace(guild_id=GUILD_A, user=member))
    assert [e.event_type for e in await server.entries()] == [EVENT_MEMBER_LEAVE]


# --------------------------------------------------------------------------- missing View Audit Log


async def test_without_view_audit_log_the_event_is_still_logged_with_an_unknown_actor(db, two_guilds):
    await enable_modlog(db, GUILD_A)
    server = Server(db, GUILD_A, view_audit_log=False)
    before, after = member_pair(server, nick="Al")
    await server.cog.on_member_update(before, after)
    await server.cog.on_member_update(after, before)

    first, _second = await server.entries()
    assert first.actor_id is None
    assert first.details["actor_note"] == "unknown (missing View Audit Log)"
    embed = (await server.posted(MEMBERS_LOG))[0]
    assert field(embed, "By") == "unknown (missing View Audit Log)"
    assert server.bot.issues == [(GUILD_A, ISSUE_VIEW_AUDIT_LOG)]  # one dashboard warning, not one per event

    server.guild.me.guild_permissions = discord.Permissions(view_audit_log=True)  # an admin grants it
    await server.cog.on_member_update(before, after)
    assert (GUILD_A, ISSUE_VIEW_AUDIT_LOG) in server.bot.resolved
    assert (await server.entries())[-1].details["actor_note"] == "unknown"
    await server.manager.close()


async def test_without_view_audit_log_a_webhook_change_is_noted_from_the_gateway(db, two_guilds):
    await enable_modlog(db, GUILD_A)
    server = Server(db, GUILD_A, view_audit_log=False)
    await server.cog.on_webhooks_update(server.chat)
    (entry,) = await server.entries()
    assert (entry.event_type, entry.channel_id) == (EVENT_WEBHOOK, CHAT)
    assert entry.details["actor_note"] == "unknown (missing View Audit Log)"
    await server.manager.close()


async def test_with_view_audit_log_a_webhook_is_logged_once_from_the_audit_entry(server):
    await server.cog.on_webhooks_update(server.chat)  # the gateway event stays silent here
    await server.audit(
        Action.webhook_create, target_id=77, after=Diff(name="GitHub", channel=SimpleNamespace(id=CHAT, name="general"))
    )
    (entry,) = await server.entries()
    assert (entry.event_type, entry.actor_id, entry.channel_id) == (EVENT_WEBHOOK, MOD_ID, CHAT)
    (embed,) = await server.posted(SERVER_LOG)
    assert embed.title == "Webhook created" and field(embed, "Webhook") == "GitHub · `77`"


# --------------------------------------------------------------------------- deleted by


async def test_a_message_deleted_by_a_moderator_names_them(server):
    author = fake_member(server.guild, USER, name="alice")
    await server.audit(Action.message_delete, **message_delete_entry())
    await server.cog.on_raw_message_delete(delete_payload(message(author, "the secret")))

    (entry,) = await server.entries()
    assert (entry.event_type, entry.target_id, entry.actor_id, entry.channel_id) == (
        EVENT_MESSAGE_DELETE,
        USER,
        MOD_ID,
        CHAT,
    )
    assert entry.details["content"] == "the secret"
    (embed,) = await server.posted(MESSAGES_LOG)
    assert embed.description == "the secret"
    assert field(embed, "Deleted by") == f"<@{MOD_ID}> · mod · `{MOD_ID}`"


async def test_a_self_deleted_message_says_author_or_unknown(server):
    author = fake_member(server.guild, USER, name="alice")
    await server.cog.on_raw_message_delete(delete_payload(message(author)))
    (entry,) = await server.entries()
    assert entry.actor_id is None and entry.details["actor_note"] == "author or unknown"
    (embed,) = await server.posted(MESSAGES_LOG)
    assert field(embed, "Deleted by") == "author or unknown"


async def test_an_audit_entry_for_another_channel_or_author_is_not_used(server):
    author = fake_member(server.guild, USER, name="alice")
    await server.audit(Action.message_delete, **message_delete_entry(channel_id=601))
    await server.audit(Action.message_delete, **message_delete_entry(author_id=8))
    await server.cog.on_raw_message_delete(delete_payload(message(author)))
    (entry,) = await server.entries()
    assert entry.actor_id is None


async def test_an_old_audit_entry_is_not_used_for_a_new_delete(server):
    author = fake_member(server.guild, USER, name="alice")
    await server.audit(Action.message_delete, **message_delete_entry())
    server.clock.advance(20)
    await server.cog.on_raw_message_delete(delete_payload(message(author)))
    (entry,) = await server.entries()
    assert entry.actor_id is None


async def test_two_deletes_and_one_audit_entry_name_nobody(server):
    author = fake_member(server.guild, USER, name="alice")
    await server.audit(Action.message_delete, **message_delete_entry())
    # Another delete of alice's messages at the same moment.
    server.manager.audit.observe("message_delete", GUILD_A, channel_id=CHAT, user_id=USER)
    await server.cog.on_raw_message_delete(delete_payload(message(author)))
    (entry,) = await server.entries()
    assert entry.actor_id is None and entry.details["actor_note"] == "author or unknown"


async def test_a_deleted_message_that_was_not_in_memory_is_still_logged(server):
    await server.cog.on_raw_message_delete(delete_payload(None))
    (entry,) = await server.entries()
    assert entry.target_id is None and entry.details["cached"] is False
    assert "content" not in entry.details

    # With a single matching audit entry, both the author and the deleter are known after all.
    fake_member(server.guild, USER, name="alice")
    server.clock.advance(60)
    await server.audit(Action.message_delete, **message_delete_entry())
    await server.cog.on_raw_message_delete(delete_payload(None, message_id=9002))
    entry = (await server.entries())[-1]
    assert (entry.target_id, entry.actor_id, entry.details["target_name"]) == (USER, MOD_ID, "alice")


async def test_attachments_are_kept_as_names_and_links(server):
    author = fake_member(server.guild, USER, name="alice")
    deleted = message(author, "")
    deleted.attachments = [SimpleNamespace(filename="cat.png", url="https://cdn.discordapp.com/a/cat.png")]
    await server.cog.on_raw_message_delete(delete_payload(deleted))
    (entry,) = await server.entries()
    assert entry.details["attachments"] == [{"filename": "cat.png", "url": "https://cdn.discordapp.com/a/cat.png"}]


# --------------------------------------------------------------------------- message rules


async def test_bot_messages_and_messages_in_log_channels_are_not_logged(server):
    bot_author = fake_member(server.guild, 50, name="musicbot", bot=True)
    human = fake_member(server.guild, USER, name="alice")
    await server.cog.on_raw_message_delete(delete_payload(message(bot_author)))
    await server.cog.on_raw_message_delete(delete_payload(message(human), channel_id=MESSAGES_LOG))
    assert await server.entries() == []
    assert await server.posted(MESSAGES_LOG) == []


async def test_ignored_users_roles_and_channels_are_not_logged(server):
    await enable_modlog(server.db, GUILD_A, ignored_user_ids=[USER], ignored_role_ids=[70], ignored_channel_ids=[601])
    ignored_user = fake_member(server.guild, USER, name="alice")
    ignored_role = fake_member(server.guild, 8, name="bob", roles=[FakeRole(70, "Quiet")])
    someone = fake_member(server.guild, 9, name="carol")
    await server.cog.on_raw_message_delete(delete_payload(message(ignored_user)))
    await server.cog.on_raw_message_delete(delete_payload(message(ignored_role)))
    await server.cog.on_raw_message_delete(delete_payload(message(someone), channel_id=601))
    assert await server.entries() == []
    await server.cog.on_raw_message_delete(delete_payload(message(someone)))
    assert len(await server.entries()) == 1


async def test_an_edit_keeps_before_and_after_and_a_link(server):
    author = fake_member(server.guild, USER, name="alice")
    before, after = message(author, "helo"), message(author, "hello")
    payload = SimpleNamespace(guild_id=GUILD_A, channel_id=CHAT, message_id=9001, cached_message=before, message=after)
    await server.cog.on_raw_message_edit(payload)
    (entry,) = await server.entries()
    assert entry.event_type == EVENT_MESSAGE_EDIT and entry.actor_id is None
    assert (entry.details["before"], entry.details["after"]) == ("helo", "hello")
    assert entry.details["jump_url"] == after.jump_url
    (embed,) = await server.posted(MESSAGES_LOG)
    assert field(embed, "By") is None  # only the author can edit a message


async def test_an_update_that_does_not_change_the_text_is_not_an_edit(server):
    author = fake_member(server.guild, USER, name="alice")
    same = SimpleNamespace(
        guild_id=GUILD_A,
        channel_id=CHAT,
        message_id=9001,
        cached_message=message(author, "a link"),
        message=message(author, "a link"),  # Discord added the link preview
    )
    old_pin = SimpleNamespace(
        guild_id=GUILD_A, channel_id=CHAT, message_id=9002, cached_message=None, message=message(author, "pinned")
    )
    await server.cog.on_raw_message_edit(same)
    await server.cog.on_raw_message_edit(old_pin)
    assert await server.entries() == []


async def test_an_edit_of_a_message_not_in_memory_is_logged_without_the_old_text(server):
    author = fake_member(server.guild, USER, name="alice")
    after = message(author, "new text")
    after.edited_at = utcnow()
    payload = SimpleNamespace(guild_id=GUILD_A, channel_id=CHAT, message_id=9001, cached_message=None, message=after)
    await server.cog.on_raw_message_edit(payload)
    (entry,) = await server.entries()
    assert entry.details["before"] is None and entry.details["after"] == "new text"


async def test_a_purge_is_one_entry_with_the_count_the_actor_and_the_cached_messages(server):
    alice = fake_member(server.guild, USER, name="alice")
    cached = [message(alice, f"spam {n}", message_id=9000 + n) for n in (3, 1, 2)]
    await server.audit(Action.message_bulk_delete, target_id=CHAT, extra=SimpleNamespace(count=30))
    await server.cog.on_raw_bulk_message_delete(
        SimpleNamespace(guild_id=GUILD_A, channel_id=CHAT, message_ids=set(range(30)), cached_messages=cached)
    )
    (entry,) = await server.entries()
    assert (entry.event_type, entry.actor_id, entry.channel_id) == (EVENT_BULK_DELETE, MOD_ID, CHAT)
    assert entry.details["count"] == 30
    assert [m["content"] for m in entry.details["messages"]] == ["spam 1", "spam 2", "spam 3"]  # oldest first
    assert entry.details["messages"][0]["author_id"] == str(USER)  # IDs inside JSON are strings


# --------------------------------------------------------------------------- server events


def role(**changed):
    values = {
        "id": 70,
        "name": "Staff",
        "colour": discord.Colour(0x112233),
        "hoist": False,
        "mentionable": False,
        "permissions": discord.Permissions(send_messages=True),
        "position": 3,
    }
    return SimpleNamespace(**{**values, **changed})


async def test_a_role_change_lists_what_changed_and_who_did_it(server):
    before = role(guild=server.guild)
    after = role(
        guild=server.guild, name="Moderators", permissions=discord.Permissions(send_messages=True, kick_members=True)
    )
    await server.audit(Action.role_update, target_id=70, after=Diff(name="Moderators"))
    await server.cog.on_guild_role_update(before, after)
    (entry,) = await server.entries()
    assert (entry.event_type, entry.target_id, entry.actor_id) == (EVENT_ROLE_UPDATE, 70, MOD_ID)
    assert entry.details["changes"] == [
        {"field": "Name", "old": "Staff", "new": "Moderators"},
        {"field": "Permissions granted", "old": "", "new": "Kick Members"},
    ]
    (embed,) = await server.posted(SERVER_LOG)
    assert field(embed, "Role") == "<@&70> · Moderators · `70`"


async def test_a_role_that_only_moved_is_not_logged(server):
    await server.cog.on_guild_role_update(role(guild=server.guild), role(guild=server.guild, position=9))
    assert await server.entries() == []


async def test_a_channel_change_includes_topic_slowmode_and_permission_overrides(server):
    everyone = FakeRole(GUILD_A, "@everyone")
    common = {"id": CHAT, "guild": server.guild, "name": "general", "nsfw": False, "category": None}
    before = SimpleNamespace(**common, topic=None, slowmode_delay=0, overwrites={})
    after = SimpleNamespace(
        **common,
        topic="Be kind",
        slowmode_delay=30,
        overwrites={everyone: discord.PermissionOverwrite(send_messages=False)},
    )
    await server.audit(Action.overwrite_create, target_id=CHAT)
    await server.cog.on_guild_channel_update(before, after)
    (entry,) = await server.entries()
    assert (entry.event_type, entry.actor_id) == (EVENT_CHANNEL_UPDATE, MOD_ID)
    assert entry.details["changes"] == [
        {"field": "Topic", "old": "none", "new": "Be kind"},
        {"field": "Slowmode", "old": "off", "new": "30s"},
        {"field": "Permissions added for member @everyone", "old": "", "new": "deny: Send Messages"},
    ]


# --------------------------------------------------------------------------- voice


def voice(channel_id: int | None):
    channel = SimpleNamespace(id=channel_id, name=f"voice-{channel_id}") if channel_id else None
    return SimpleNamespace(channel=channel)


async def test_voice_is_off_by_default(server):
    member = fake_member(server.guild, USER, name="alice")
    await server.cog.on_voice_state_update(member, voice(None), voice(700))
    assert await server.entries() == []


async def test_voice_joins_leaves_and_moves_go_to_the_members_channel_when_switched_on(server):
    await enable_modlog(
        server.db, GUILD_A, events={EVENT_VOICE_JOIN: True, EVENT_VOICE_LEAVE: True, EVENT_VOICE_MOVE: True}
    )
    member = fake_member(server.guild, USER, name="alice")
    await server.cog.on_voice_state_update(member, voice(None), voice(700))
    await server.cog.on_voice_state_update(member, voice(700), voice(700))  # muted: not logged
    await server.audit(Action.member_move, extra=SimpleNamespace(channel=SimpleNamespace(id=701), count=1))
    await server.cog.on_voice_state_update(member, voice(700), voice(701))
    server.clock.advance(60)
    await server.cog.on_voice_state_update(member, voice(701), voice(None))

    joined, moved, left = await server.entries()
    assert [e.event_type for e in (joined, moved, left)] == [EVENT_VOICE_JOIN, EVENT_VOICE_MOVE, EVENT_VOICE_LEAVE]
    assert moved.actor_id == MOD_ID  # one move, one audit entry: no doubt
    assert left.actor_id is None and "actor_note" not in left.details  # they most likely just left
    assert len(await server.posted(MEMBERS_LOG)) == 3


# --------------------------------------------------------------------------- per-server settings


async def test_a_category_without_a_channel_records_nothing_at_all(server):
    await enable_modlog(server.db, GUILD_A, messages_channel_id=None)
    author = fake_member(server.guild, USER, name="alice")
    await server.cog.on_raw_message_delete(delete_payload(message(author)))
    assert await server.entries() == []  # not even in the history
    await server.cog.on_member_join(author)
    assert len(await server.entries()) == 1


async def test_a_server_with_logging_off_records_nothing(db, two_guilds):
    await enable_modlog(db, GUILD_A)  # on in A, never switched on in B
    server_b = Server(db, GUILD_B)
    member = fake_member(server_b.guild, USER, name="alice")
    await server_b.cog.on_member_join(member)
    await server_b.cog.on_member_ban(server_b.guild, member)
    assert await server_b.entries() == []
    assert await server_b.posted(MEMBERS_LOG) == []
    await server_b.manager.close()
