"""/tickets diagnostics: the role checks cover every role JoltBot gives, whatever setting it comes from."""

from joltbot.modules.tickets.diagnostics import ERROR, OK, run_diagnostics
from joltbot.modules.tickets.settings import TicketSettings

from .conftest import GUILD_A
from .fakes import BOT_ROLE_POSITION, FakeGuild, FakeRole

ACCEPT_ROLE, EXTRA_ROLE, INFANTRY_ROLE, CAVALRY_ROLE = 51, 53, 54, 55


def _settings() -> TicketSettings:
    return TicketSettings.model_validate(
        {
            "accept_role_ids": [ACCEPT_ROLE],
            "extra_role_choices": [{"label": "Team A", "role_id": EXTRA_ROLE}],
            "choice_questions": [
                {
                    "label": "Troop type",
                    "options": [
                        {"label": "Infantry", "role_id": INFANTRY_ROLE},
                        {"label": "Cavalry", "role_id": CAVALRY_ROLE},
                    ],
                }
            ],
        }
    )


def _guild(**positions: int) -> FakeGuild:
    guild = FakeGuild(GUILD_A)
    for role_id, name in ((ACCEPT_ROLE, "Member"), (EXTRA_ROLE, "Team A"), (INFANTRY_ROLE, "Infantry")):
        guild.add_role(FakeRole(role_id, name, position=positions.get(name.lower().replace(" ", "_"), 10)))
    return guild


def _texts(findings, level):
    return [f.text for f in findings if f.level == level]


def test_role_order_is_ok_when_joltbot_is_above_every_role_it_gives():
    guild = _guild()
    guild.add_role(FakeRole(CAVALRY_ROLE, "Cavalry", position=10))
    findings = run_diagnostics(guild, _settings(), enabled=True)
    assert any(text.startswith("Role order OK") for text in _texts(findings, OK))


def test_a_dropdown_role_above_joltbot_is_an_error():
    guild = _guild(infantry=BOT_ROLE_POSITION + 1)
    guild.add_role(FakeRole(CAVALRY_ROLE, "Cavalry", position=10))
    errors = _texts(run_diagnostics(guild, _settings(), enabled=True), ERROR)
    assert any("NOT above **Infantry**" in text for text in errors)


def test_a_deleted_dropdown_role_is_an_error_that_names_the_question_and_option():
    guild = _guild()  # the Cavalry role does not exist
    errors = _texts(run_diagnostics(guild, _settings(), enabled=True), ERROR)
    assert any(text.startswith("Dropdown role 'Troop type: Cavalry'") and "no longer exists" in text for text in errors)
