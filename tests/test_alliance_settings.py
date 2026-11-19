"""Alliance settings: what the model accepts, and who the permission check lets through."""

import pytest
from pydantic import ValidationError

from joltbot.modules.alliances.checks import can_manage_requests, can_review
from joltbot.modules.alliances.settings import DEFAULT_REJECT_COOLDOWN_HOURS, AllianceSettings

from .conftest import GUILD_A
from .fakes import FakeGuild, FakeRole, fake_member

ONE_R5, ONE_R4, TWO_R5, ONE, TWO, KING = 70, 71, 72, 80, 81, 90


def _alliance(name="Kings Of Chaos", member=ONE, reviewers=(ONE_R5, ONE_R4), channel=60):
    return {
        "name": name,
        "member_role_id": member,
        "reviewer_role_ids": list(reviewers),
        "request_channel_id": channel,
    }


def _settings(**changes):
    values = dict(
        alliances=[_alliance(), _alliance("Defenders of Chaos", TWO, (TWO_R5,), 61)],
        override_role_ids=[KING],
        panel_channel_id=63,
        log_channel_id=62,
    )
    return AllianceSettings(**{**values, **changes})


def _member(*role_ids, **permissions):
    guild = FakeGuild(GUILD_A)
    return fake_member(guild, 7, roles=[FakeRole(role_id) for role_id in role_ids], **permissions)


# ------------------------------------------------------------------ the permission check


def test_the_right_alliances_leadership_may_review():
    settings = _settings()
    one, two = settings.alliances
    assert can_review(_member(ONE_R5), one, settings) is True
    assert can_review(_member(ONE_R4), one, settings) is True
    assert can_review(_member(TWO_R5), two, settings) is True


def test_the_wrong_alliances_leadership_may_not():
    settings = _settings()
    one, two = settings.alliances
    assert can_review(_member(TWO_R5), one, settings) is False
    assert can_review(_member(ONE_R5, ONE_R4), two, settings) is False


def test_an_override_role_may_review_every_alliance():
    settings = _settings()
    assert all(can_review(_member(KING), alliance, settings) for alliance in settings.alliances)


def test_nothing_else_lets_someone_review():
    settings = _settings()
    one = settings.alliances[0]
    assert can_review(_member(ONE), one, settings) is False  # a plain member of the alliance
    assert can_review(_member(), one, settings) is False
    assert can_review(_member(administrator=True, manage_guild=True), one, settings) is False
    assert can_review(object(), one, settings) is False  # not a member of the server at all


def test_who_may_run_the_admin_commands():
    settings = _settings()
    assert can_manage_requests(_member(manage_guild=True), settings) is True
    assert can_manage_requests(_member(KING), settings) is True
    assert can_manage_requests(_member(ONE_R5), settings) is False


# ------------------------------------------------------------------ the settings model


def test_a_server_that_never_set_it_up_loads_with_defaults():
    settings = AllianceSettings()
    assert settings.alliances == [] and settings.reject_cooldown_hours == DEFAULT_REJECT_COOLDOWN_HOURS == 24
    assert len(settings.setup_problems()) == 3  # an alliance, the panel channel, the log channel
    assert _settings().setup_problems() == []


def test_ids_are_stored_as_text_and_read_back_as_numbers():
    stored = _settings().model_dump(mode="json")
    assert stored["alliances"][0]["member_role_id"] == str(ONE)
    assert AllianceSettings.model_validate(stored) == _settings()


def test_an_alliance_is_found_by_its_member_role():
    settings = _settings()
    assert settings.alliance_for(TWO).name == "Defenders of Chaos"
    assert settings.alliance_for(12345) is None
    assert settings.member_role_ids == {ONE, TWO}


def test_every_member_role_is_listed_as_a_role_the_bot_gives_out():
    # This list is what the dashboard's "powerful role" and "dashboard access" rules read.
    assert [(r.description, r.role_id) for r in _settings().assignable_roles()] == [
        ("Member role of Kings Of Chaos", ONE),
        ("Member role of Defenders of Chaos", TWO),
    ]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"alliances": [_alliance(), _alliance("Other", ONE, (TWO_R5,))]}, "different member role"),
        ({"alliances": [_alliance(), _alliance("kings of chaos", TWO, (TWO_R5,))]}, "different name"),
        ({"alliances": [_alliance(reviewers=())]}, "reviewer"),
        # Otherwise every member of the alliance could approve the next one.
        ({"alliances": [_alliance(reviewers=(ONE,))]}, "can't also be a reviewer"),
        ({"alliances": [_alliance(), _alliance("Other", TWO, (ONE,))]}, "can't also be a reviewer"),
        ({"override_role_ids": [TWO]}, "can't also be a reviewer or override role"),
        ({"reject_cooldown_hours": -1}, "greater than or equal to 0"),
    ],
)
def test_settings_that_would_not_work_are_refused(changes, message):
    with pytest.raises(ValidationError, match=message):
        _settings(**changes)
