"""Tests for TicketSettings validation, snowflake JSON, and setup_problems."""

import pytest
from pydantic import ValidationError

from joltbot.modules.tickets.settings import FormQuestion, TicketSettings


def test_defaults_are_valid_and_need_setup():
    settings = TicketSettings()
    assert len(settings.form_questions) >= 1
    assert settings.setup_problems()  # nothing picked yet, so it can't be switched on


def test_ids_accept_strings_and_are_saved_as_strings():
    settings = TicketSettings.model_validate(
        {"staff_role_ids": ["900000000000000011"], "log_channel_id": "900000000000000034"}
    )
    assert settings.staff_role_ids == [900000000000000011]
    dumped = settings.model_dump(mode="json")
    # Strings in JSON so a browser can't round the number to a different ID.
    assert dumped["staff_role_ids"] == ["900000000000000011"]
    assert dumped["log_channel_id"] == "900000000000000034"


def test_empty_form_value_means_no_channel():
    assert TicketSettings.model_validate({"log_channel_id": ""}).log_channel_id is None


@pytest.mark.parametrize("bad", ["abc", "-5", "1.5", True, "0x10"])
def test_bad_ids_are_rejected(bad):
    with pytest.raises(ValidationError):
        TicketSettings.model_validate({"staff_role_ids": [bad]})


def test_at_most_five_questions():
    questions = [FormQuestion(label=f"Q{i}") for i in range(6)]
    with pytest.raises(ValidationError):
        TicketSettings(form_questions=questions)


def test_pending_role_cannot_be_an_accept_role():
    with pytest.raises(ValidationError):
        TicketSettings(pending_role_id=5, accept_role_ids=[5])


def test_duplicate_role_ids_are_dropped():
    assert TicketSettings(staff_role_ids=[1, 2, 1]).staff_role_ids == [1, 2]


def test_ping_falls_back_to_staff_roles():
    assert TicketSettings(staff_role_ids=[7]).effective_ping_role_ids == [7]
    assert TicketSettings(staff_role_ids=[7], ping_role_ids=[8]).effective_ping_role_ids == [8]
    assert TicketSettings(staff_role_ids=[7], ping_enabled=False).effective_ping_role_ids == []


def test_fully_configured_has_no_problems():
    settings = TicketSettings(
        panel_channel_id=1, ticket_category_id=2, log_channel_id=3, accept_role_ids=[4], staff_role_ids=[5]
    )
    assert settings.setup_problems() == []
