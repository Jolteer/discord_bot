"""Matching gateway events to audit log entries: a name is only given when there is no doubt."""

from __future__ import annotations

from joltbot.modules.modlog.correlate import (
    AUDIT_TTL_SECONDS,
    SEEN_BAN,
    SEEN_MESSAGE_DELETE,
    SEEN_VOICE_LEAVE,
    SEEN_VOICE_MOVE,
    AuditRecord,
    RecentAudit,
)

from .modlog_helpers import CHAT, MOD_ID, FakeClock

GUILD, OTHER_GUILD, AUTHOR, OTHER_MOD = 1, 2, 7, 43


def buffer() -> tuple[RecentAudit, FakeClock]:
    clock = FakeClock()
    return RecentAudit(clock=clock), clock


def deleted(actor: int = MOD_ID, *, author: int = AUTHOR, channel: int = CHAT, count: int = 1) -> AuditRecord:
    return AuditRecord(GUILD, "message_delete", actor, target_id=author, channel_id=channel, count=count)


# --------------------------------------------------------------------------- find


def test_find_returns_the_entry_for_the_same_target_and_change():
    audit, clock = buffer()
    nick = AuditRecord(GUILD, "member_update", MOD_ID, target_id=AUTHOR, keys=frozenset({"nick"}), reason="rule 3")
    audit.add(nick)
    assert audit.find(GUILD, ("member_update",), since=clock() - 5, target_id=AUTHOR, key="nick") is nick
    # A timeout is also a member_update, but it changed a different attribute.
    assert audit.find(GUILD, ("member_update",), since=clock() - 5, target_id=AUTHOR, key="timed_out_until") is None
    assert audit.find(GUILD, ("member_update",), since=clock() - 5, target_id=8, key="nick") is None
    assert audit.find(OTHER_GUILD, ("member_update",), since=clock() - 5, target_id=AUTHOR) is None
    assert audit.find(GUILD, ("member_role_update",), since=clock() - 5, target_id=AUTHOR) is None


def test_find_names_nobody_when_two_different_people_fit():
    audit, clock = buffer()
    audit.add(AuditRecord(GUILD, "role_update", MOD_ID, target_id=5))
    audit.add(AuditRecord(GUILD, "role_update", OTHER_MOD, target_id=5))
    assert audit.find(GUILD, ("role_update",), since=clock() - 5, target_id=5) is None


def test_find_ignores_entries_from_before_the_window_and_forgets_old_ones():
    audit, clock = buffer()
    audit.add(AuditRecord(GUILD, "ban", MOD_ID, target_id=AUTHOR))
    clock.advance(10)
    assert audit.find(GUILD, ("ban",), since=clock() - 5, target_id=AUTHOR) is None
    clock.advance(AUDIT_TTL_SECONDS)
    audit.add(AuditRecord(GUILD, "ban", MOD_ID, target_id=99))  # adding is when old entries are dropped
    assert audit.find(GUILD, ("ban",), since=0, target_id=AUTHOR) is None


def test_was_seen_remembers_a_ban_for_that_member_only():
    audit, clock = buffer()
    audit.observe(SEEN_BAN, GUILD, user_id=AUTHOR)
    assert audit.was_seen(SEEN_BAN, GUILD, user_id=AUTHOR, since=clock() - 5)
    assert not audit.was_seen(SEEN_BAN, GUILD, user_id=8, since=clock() - 5)
    assert not audit.was_seen(SEEN_BAN, OTHER_GUILD, user_id=AUTHOR, since=clock() - 5)


# --------------------------------------------------------------------------- deleted by


def test_a_moderators_delete_is_matched_by_channel_and_author():
    audit, clock = buffer()
    audit.observe(SEEN_MESSAGE_DELETE, GUILD, channel_id=CHAT, user_id=AUTHOR)
    audit.add(deleted())
    record = audit.who_deleted(GUILD, CHAT, AUTHOR, since=clock() - 5)
    assert record is not None and record.actor_id == MOD_ID


def test_the_audit_entry_may_arrive_before_or_after_the_delete_event():
    audit, clock = buffer()
    audit.add(deleted())
    clock.advance(1)
    audit.observe(SEEN_MESSAGE_DELETE, GUILD, channel_id=CHAT, user_id=AUTHOR)
    assert audit.who_deleted(GUILD, CHAT, AUTHOR, since=clock() - 5) is not None


def test_a_self_delete_has_no_entry_so_nobody_is_named():
    audit, clock = buffer()
    audit.observe(SEEN_MESSAGE_DELETE, GUILD, channel_id=CHAT, user_id=AUTHOR)
    assert audit.who_deleted(GUILD, CHAT, AUTHOR, since=clock() - 5) is None


def test_an_entry_for_another_channel_or_author_is_not_used():
    audit, clock = buffer()
    audit.observe(SEEN_MESSAGE_DELETE, GUILD, channel_id=CHAT, user_id=AUTHOR)
    audit.add(deleted(channel=601))
    audit.add(deleted(author=8))
    assert audit.who_deleted(GUILD, CHAT, AUTHOR, since=clock() - 5) is None


def test_an_old_entry_is_not_used_for_a_new_delete():
    # Discord merges later deletes into the old entry without sending it again,
    # so an entry from a minute ago says nothing about a delete happening now.
    audit, clock = buffer()
    audit.add(deleted())
    clock.advance(20)
    audit.observe(SEEN_MESSAGE_DELETE, GUILD, channel_id=CHAT, user_id=AUTHOR)
    assert audit.who_deleted(GUILD, CHAT, AUTHOR, since=clock() - 5) is None


def test_two_deletes_but_one_entry_names_nobody():
    # One could be the moderator's and the other the author's own: there is no telling which.
    audit, clock = buffer()
    audit.observe(SEEN_MESSAGE_DELETE, GUILD, channel_id=CHAT, user_id=AUTHOR)
    audit.observe(SEEN_MESSAGE_DELETE, GUILD, channel_id=CHAT, user_id=AUTHOR)
    audit.add(deleted())
    assert audit.who_deleted(GUILD, CHAT, AUTHOR, since=clock() - 5) is None


def test_two_deletes_covered_by_the_entrys_count_are_both_named():
    audit, clock = buffer()
    audit.observe(SEEN_MESSAGE_DELETE, GUILD, channel_id=CHAT, user_id=AUTHOR)
    audit.observe(SEEN_MESSAGE_DELETE, GUILD, channel_id=CHAT, user_id=AUTHOR)
    audit.add(deleted(count=2))
    assert audit.who_deleted(GUILD, CHAT, AUTHOR, since=clock() - 5) is not None


def test_deletes_of_other_authors_messages_do_not_cast_doubt():
    audit, clock = buffer()
    audit.observe(SEEN_MESSAGE_DELETE, GUILD, channel_id=CHAT, user_id=AUTHOR)
    audit.observe(SEEN_MESSAGE_DELETE, GUILD, channel_id=CHAT, user_id=8)  # someone else's own delete
    audit.add(deleted())
    assert audit.who_deleted(GUILD, CHAT, AUTHOR, since=clock() - 5) is not None


def test_entries_from_two_moderators_name_nobody():
    audit, clock = buffer()
    audit.observe(SEEN_MESSAGE_DELETE, GUILD, channel_id=CHAT, user_id=AUTHOR)
    audit.add(deleted(MOD_ID))
    audit.add(deleted(OTHER_MOD))
    assert audit.who_deleted(GUILD, CHAT, AUTHOR, since=clock() - 5) is None


def test_an_uncached_message_gets_author_and_deleter_from_the_only_entry():
    audit, clock = buffer()
    audit.observe(SEEN_MESSAGE_DELETE, GUILD, channel_id=CHAT)  # author unknown
    audit.add(deleted())
    record = audit.who_deleted(GUILD, CHAT, None, since=clock() - 5)
    assert record is not None and (record.actor_id, record.target_id) == (MOD_ID, AUTHOR)


def test_an_uncached_message_stays_unnamed_when_more_than_one_delete_or_entry_fits():
    audit, clock = buffer()
    audit.observe(SEEN_MESSAGE_DELETE, GUILD, channel_id=CHAT)
    audit.observe(SEEN_MESSAGE_DELETE, GUILD, channel_id=CHAT)
    audit.add(deleted())
    assert audit.who_deleted(GUILD, CHAT, None, since=clock() - 5) is None

    audit, clock = buffer()
    audit.observe(SEEN_MESSAGE_DELETE, GUILD, channel_id=CHAT)
    audit.add(deleted(author=AUTHOR))
    audit.add(deleted(author=8))  # same moderator, two authors: whose message was this one?
    assert audit.who_deleted(GUILD, CHAT, None, since=clock() - 5) is None


# --------------------------------------------------------------------------- voice


def test_a_voice_move_is_named_only_when_one_move_and_one_entry_fit():
    audit, clock = buffer()
    audit.observe(SEEN_VOICE_MOVE, GUILD, channel_id=700, user_id=AUTHOR)
    assert audit.who_moved(GUILD, 700, since=clock() - 5) is None  # they moved themself
    audit.add(AuditRecord(GUILD, "member_move", MOD_ID, channel_id=700))
    assert audit.who_moved(GUILD, 700, since=clock() - 5).actor_id == MOD_ID
    assert audit.who_moved(GUILD, 701, since=clock() - 5) is None
    audit.observe(SEEN_VOICE_MOVE, GUILD, channel_id=700, user_id=8)
    assert audit.who_moved(GUILD, 700, since=clock() - 5) is None  # two moved in, one entry


def test_a_voice_disconnect_is_named_only_when_one_leave_and_one_entry_fit():
    audit, clock = buffer()
    audit.observe(SEEN_VOICE_LEAVE, GUILD, user_id=AUTHOR)
    assert audit.who_disconnected(GUILD, since=clock() - 5) is None
    audit.add(AuditRecord(GUILD, "member_disconnect", MOD_ID))
    assert audit.who_disconnected(GUILD, since=clock() - 5).actor_id == MOD_ID
    audit.observe(SEEN_VOICE_LEAVE, GUILD, user_id=8)
    assert audit.who_disconnected(GUILD, since=clock() - 5) is None
