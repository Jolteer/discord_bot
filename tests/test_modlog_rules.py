"""The mod log's ignore rules: what is recorded and what is skipped."""

from __future__ import annotations

import pytest

from joltbot.modules.modlog.constants import (
    EVENT_AVATAR,
    EVENT_BAN,
    EVENT_MEMBER_JOIN,
    EVENT_MESSAGE_DELETE,
    EVENT_MESSAGE_EDIT,
    EVENT_ROLE_UPDATE,
    EVENT_VOICE_JOIN,
    EVENTS,
)
from joltbot.modules.modlog.events import Facts
from joltbot.modules.modlog.rules import should_log
from joltbot.modules.modlog.settings import ModLogSettings

from .modlog_helpers import CHAT, MEMBERS_LOG, MESSAGES_LOG, all_log_channels

USER, ROLE, CATEGORY = 7, 70, 650


def settings(**overrides) -> ModLogSettings:
    return ModLogSettings(**{**all_log_channels(), **overrides})


def a_message(event: str = EVENT_MESSAGE_DELETE, **facts) -> Facts:
    return Facts(event, **{"user_id": USER, "channel_ids": (CHAT,), **facts})


def test_an_ordinary_event_is_recorded():
    assert should_log(True, settings(), a_message())


def test_nothing_is_recorded_while_the_module_is_off():
    assert not should_log(False, settings(), a_message())


def test_an_event_switched_off_is_not_recorded_but_others_still_are():
    config = settings(events={EVENT_MESSAGE_DELETE: False})
    assert not should_log(True, config, a_message())
    assert should_log(True, config, a_message(EVENT_MESSAGE_EDIT))


@pytest.mark.parametrize("event", [EVENT_AVATAR, EVENT_VOICE_JOIN])
def test_spammy_events_are_off_until_switched_on(event):
    assert not EVENTS[event].default_on
    assert not should_log(True, settings(), Facts(event, user_id=USER))
    assert should_log(True, settings(events={event: True}), Facts(event, user_id=USER))


def test_a_category_without_a_channel_records_nothing():
    config = settings(messages_channel_id=None)
    assert not should_log(True, config, a_message())
    assert should_log(True, config, Facts(EVENT_MEMBER_JOIN, user_id=USER))  # other categories are unaffected


def test_voice_uses_the_members_channel_unless_it_has_its_own():
    on = {"events": {EVENT_VOICE_JOIN: True}}
    assert settings(**on).channel_for("voice") == MEMBERS_LOG
    assert settings(voice_channel_id=555, **on).channel_for("voice") == 555
    assert not should_log(True, settings(members_channel_id=None, **on), Facts(EVENT_VOICE_JOIN, user_id=USER))
    assert should_log(
        True, settings(members_channel_id=None, voice_channel_id=555, **on), Facts(EVENT_VOICE_JOIN, user_id=USER)
    )


def test_bots_are_ignored_by_default_and_logged_when_asked():
    assert not should_log(True, settings(), a_message(user_is_bot=True))
    assert should_log(True, settings(ignore_bots=False), a_message(user_is_bot=True))


def test_ignored_users_and_roles_are_skipped():
    assert not should_log(True, settings(ignored_user_ids=[USER]), a_message())
    assert not should_log(True, settings(ignored_role_ids=[ROLE]), a_message(user_role_ids=frozenset({ROLE, 71})))
    assert should_log(True, settings(ignored_user_ids=[8], ignored_role_ids=[72]), a_message())


def test_an_ignored_channel_also_covers_its_threads_and_a_category_its_channels():
    assert not should_log(True, settings(ignored_channel_ids=[CHAT]), a_message())
    thread_in_ignored_channel = a_message(channel_ids=(900, CHAT, CATEGORY))
    assert not should_log(True, settings(ignored_channel_ids=[CHAT]), thread_in_ignored_channel)
    assert not should_log(True, settings(ignored_channel_ids=[CATEGORY]), a_message(channel_ids=(CHAT, CATEGORY)))
    assert should_log(True, settings(ignored_channel_ids=[601]), a_message())


def test_ignored_channels_only_apply_to_message_events():
    assert should_log(True, settings(ignored_channel_ids=[CHAT]), Facts(EVENT_MEMBER_JOIN, user_id=USER))


@pytest.mark.parametrize("log_channel", [MEMBERS_LOG, MESSAGES_LOG])
def test_messages_in_a_log_channel_are_never_recorded(log_channel):
    # Otherwise deleting a log post would create a log post: a loop.
    assert not should_log(True, settings(ignore_bots=False), a_message(channel_ids=(log_channel,)))
    assert not should_log(True, settings(ignore_bots=False), a_message(EVENT_MESSAGE_EDIT, channel_ids=(log_channel,)))


def test_moderation_and_server_events_cannot_be_hidden_with_the_ignore_lists():
    config = settings(ignored_user_ids=[USER], ignored_role_ids=[ROLE])
    banned_bot = Facts(EVENT_BAN, user_id=USER, user_is_bot=True, user_role_ids=frozenset({ROLE}))
    assert should_log(True, config, banned_bot)
    assert should_log(True, config, Facts(EVENT_ROLE_UPDATE))


def test_an_unknown_event_is_not_recorded():
    assert not should_log(True, settings(), Facts("made_up", user_id=USER))


def test_toggles_for_events_this_version_does_not_know_are_dropped_on_load():
    config = ModLogSettings.model_validate({"events": {"made_up": True, EVENT_AVATAR: True}})
    assert config.events == {EVENT_AVATAR: True}


def test_logging_cannot_be_switched_on_without_a_channel():
    assert ModLogSettings().setup_problems() == ["Pick at least one log channel."]
    assert ModLogSettings(server_channel_id=5).setup_problems() == []


def test_retention_defaults_to_30_days_and_is_bounded():
    assert ModLogSettings().retention_days == 30
    for days in (0, 366):
        with pytest.raises(ValueError):
            ModLogSettings(retention_days=days)
