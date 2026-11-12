"""Tests for TicketSettings validation, snowflake JSON, and setup_problems."""

import pytest
from pydantic import ValidationError

from joltbot.modules.tickets.settings import ChoiceOption, ChoiceQuestion, FormQuestion, TicketSettings


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


# ------------------------------------------------------------------ dropdown questions

TROOPS = {
    "label": "Troop type",
    "options": [
        {"label": "Infantry", "role_id": "900000000000000041"},
        {"label": "Cavalry", "role_id": "900000000000000042"},
        {"label": "Not sure yet"},
    ],
}


def test_servers_start_without_dropdown_questions():
    assert TicketSettings().choice_questions == []


def test_a_dropdown_option_can_give_a_role_and_the_id_is_saved_as_a_string():
    settings = TicketSettings.model_validate({"choice_questions": [TROOPS]})
    (question,) = settings.choice_questions
    assert question.required is True
    assert [(o.label, o.role_id) for o in question.options] == [
        ("Infantry", 900000000000000041),
        ("Cavalry", 900000000000000042),
        ("Not sure yet", None),  # an option doesn't have to give a role
    ]
    dumped = settings.model_dump(mode="json")["choice_questions"][0]["options"]
    assert dumped[0]["role_id"] == "900000000000000041" and dumped[2]["role_id"] is None


def test_an_empty_role_picker_means_no_role():
    question = ChoiceQuestion.model_validate({"label": "Q", "options": [{"label": "A", "role_id": ""}, {"label": "B"}]})
    assert question.options[0].role_id is None


def test_a_dropdown_question_needs_at_least_two_options():
    with pytest.raises(ValidationError):
        ChoiceQuestion(label="Troop type", options=[ChoiceOption(label="Infantry")])


def test_options_of_one_question_need_different_labels():
    with pytest.raises(ValidationError, match="different label"):
        ChoiceQuestion(label="Troop type", options=[ChoiceOption(label="Infantry"), ChoiceOption(label="infantry")])


def test_at_most_four_dropdown_questions():
    # A Discord message has 5 rows, and one of them holds the Continue button.
    question = ChoiceQuestion(label="Q", options=[ChoiceOption(label="A"), ChoiceOption(label="B")])
    TicketSettings(choice_questions=[question] * 4)
    with pytest.raises(ValidationError):
        TicketSettings(choice_questions=[question] * 5)


def test_the_pending_role_cannot_be_given_by_a_dropdown_option():
    with pytest.raises(ValidationError, match="pending role"):
        TicketSettings.model_validate({"pending_role_id": "900000000000000041", "choice_questions": [TROOPS]})


def test_assignable_roles_lists_every_role_joltbot_gives():
    settings = TicketSettings.model_validate(
        {
            "staff_role_ids": [1],  # staff and ping roles are never given by JoltBot
            "ping_role_ids": [2],
            "accept_role_ids": [10],
            "pending_role_id": 11,
            "extra_role_choices": [{"label": "Team A", "role_id": 12}],
            "choice_questions": [TROOPS],
        }
    )
    assert [(r.group, r.description, r.role_id) for r in settings.assignable_roles()] == [
        ("Roles given on accept", "Role given on accept", 10),
        ("Pending role", "Pending role", 11),
        ("Extra role choices", "Extra role 'Team A'", 12),
        ("Dropdown questions", "Dropdown role 'Troop type: Infantry'", 900000000000000041),
        ("Dropdown questions", "Dropdown role 'Troop type: Cavalry'", 900000000000000042),
    ]
    assert settings.choice_role_ids == {900000000000000041, 900000000000000042}
