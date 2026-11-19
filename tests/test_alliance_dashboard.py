"""The dashboard's Alliances page: saving the settings, and the checks that guard them.

Discord is replaced by FakeDiscord (from test_web_access), so these run offline.
"""

import html

import pytest
from sqlalchemy import select

from joltbot.core.discord_rest import CATEGORY, TEXT_CHANNEL, Channel, GuildData, Role
from joltbot.core.guild_settings import load_module
from joltbot.core.models import AuditLog
from joltbot.core.permissions import ADMINISTRATOR, MANAGE_GUILD
from joltbot.web.app import create_app

from .conftest import GUILD_A, GUILD_B
from .test_web_access import (
    ALICE,
    CAT,
    DAVE,
    KICK_MEMBERS,
    ROLE,
    TEXT,
    FakeDiscord,
    client_for,
    csrf_of,
    guild_entry,
    login,
    make_config,
    ticket_form,
)

BOT_ROLE, KING, ONE_R5, ONE_R4, TWO_R5 = 5399, 5320, 5301, 5302, 5303
MODERATOR, TWO, ONE, INTEGRATION = 5330, 5311, 5310, 5340
ONE_REQUESTS, TWO_REQUESTS, LOG, PANEL = 6301, 6302, 6303, 6304
URL = f"/servers/{GUILD_A}/alliances"


@pytest.fixture
def fake():
    """Server A as a kingdom server: leadership roles, two alliance roles, and their channels."""
    discord = FakeDiscord()
    discord.data[GUILD_A] = GuildData(
        guild_id=GUILD_A,
        roles=[
            Role(BOT_ROLE, "JoltBot", 10, ADMINISTRATOR, True, 0),
            Role(KING, "King", 9, KICK_MEMBERS, False, 0),
            Role(ONE_R5, "Top 1 R5", 8, 0, False, 0),
            Role(ONE_R4, "Top 1 R4", 7, 0, False, 0),
            Role(TWO_R5, "Top 2 R5", 6, 0, False, 0),
            Role(MODERATOR, "Moderator", 5, KICK_MEMBERS, False, 0),
            Role(TWO, "Defenders of Chaos", 4, 0, False, 0),
            Role(ONE, "Kings Of Chaos", 3, 0, False, 0),
            Role(INTEGRATION, "Some Bot", 2, 0, True, 0),
            Role(ROLE[GUILD_A], "Member", 1, 0, False, 0),
        ],
        channels=[
            Channel(ONE_REQUESTS, "top1-requests", TEXT_CHANNEL, 0, None),
            Channel(TWO_REQUESTS, "top2-requests", TEXT_CHANNEL, 1, None),
            Channel(LOG, "alliance-log", TEXT_CHANNEL, 2, None),
            Channel(PANEL, "choose-your-alliance", TEXT_CHANNEL, 3, None),
            Channel(TEXT[GUILD_A], "general", TEXT_CHANNEL, 4, None),
            Channel(CAT[GUILD_A], "Tickets", CATEGORY, 5, None),
        ],
        everyone_permissions=0,
        bot_role_ids=[BOT_ROLE],
    )
    return discord


@pytest.fixture
async def app(db, two_guilds, fake):
    return create_app(make_config(), db, fake, check_schema=False)


@pytest.fixture
async def alice(app, fake):
    """Manage Server in A; an ordinary member of B."""
    async with client_for(app) as client:
        yield await login(client, fake, ALICE, [guild_entry(GUILD_A, MANAGE_GUILD), guild_entry(GUILD_B, 0)])


@pytest.fixture
async def dave(app, fake):
    """An Administrator in A."""
    async with client_for(app) as client:
        yield await login(client, fake, DAVE, [guild_entry(GUILD_A, ADMINISTRATOR)])


def form(csrf, version=1, extra=None, **fields):
    """A complete, valid Alliances form with two alliances."""
    values = {
        "csrf_token": csrf,
        "version": str(version),
        "enabled": "on",
        "a_name_0": "Kings Of Chaos",
        "a_role_0": str(ONE),
        "a_reviewers_0": [str(ONE_R5), str(ONE_R4)],
        "a_channel_0": str(ONE_REQUESTS),
        "a_name_1": "Defenders of Chaos",
        "a_role_1": str(TWO),
        "a_reviewers_1": str(TWO_R5),
        "a_channel_1": str(TWO_REQUESTS),
        "override_role_ids": str(KING),
        "panel_channel_id": str(PANEL),
        "log_channel_id": str(LOG),
        "reject_cooldown_hours": "24",
        "panel_title": "Choose your alliance",
        "panel_description": "Pick one in {server}.",
    }
    values.update(fields)
    values.update(extra or {})
    return values


async def state(db, guild_id=GUILD_A):
    async with db.session() as session:
        return await load_module(session, guild_id, "alliances")


async def post(client, **fields):
    return await client.post(URL, data=form(await csrf_of(client), **fields))


# ------------------------------------------------------------------ the page


async def test_the_page_is_linked_and_only_opens_for_this_servers_dashboard_users(alice):
    page = await alice.get(URL)
    assert page.status_code == 200 and f'href="{URL}"' in page.text  # in the sidebar
    assert 'name="a_name_0" value=""' in page.text  # an empty block to add the first alliance
    assert (await alice.get(f"/servers/{GUILD_B}/alliances")).status_code == 403


async def test_saving_stores_the_alliances_and_is_audited(db, alice):
    r = await post(alice)
    assert r.status_code == 303 and r.headers["location"] == f"{URL}?saved=1"

    saved = await state(db)
    one, two = saved.config.alliances
    assert saved.enabled is True
    assert (one.name, one.member_role_id, one.reviewer_role_ids, one.request_channel_id) == (
        "Kings Of Chaos",
        ONE,
        [ONE_R5, ONE_R4],
        ONE_REQUESTS,
    )
    assert (two.name, two.reviewer_role_ids) == ("Defenders of Chaos", [TWO_R5])
    assert saved.config.override_role_ids == [KING]
    assert (saved.config.panel_channel_id, saved.config.log_channel_id) == (PANEL, LOG)
    assert saved.config.reject_cooldown_hours == 24

    async with db.session() as session:
        entry = (await session.execute(select(AuditLog).where(AuditLog.module == "alliances"))).scalar_one()
    assert (entry.actor_id, entry.source) == (int(ALICE["id"]), "dashboard")

    page = (await alice.get(URL)).text
    assert 'name="a_name_1" value="Defenders of Chaos"' in page
    assert f'name="a_reviewers_0" value="{ONE_R4}" checked' in page
    assert 'name="a_name_2" value=""' in page  # and an empty block for the next one


async def test_clearing_an_alliances_name_removes_it(db, alice):
    assert (await post(alice)).status_code == 303
    assert (await post(alice, version=2, a_name_0="")).status_code == 303
    assert [a.name for a in (await state(db)).config.alliances] == ["Defenders of Chaos"]


async def test_the_cooldown_can_be_changed(db, alice):
    assert (await post(alice, reject_cooldown_hours="6")).status_code == 303
    assert (await state(db)).config.reject_cooldown_hours == 6
    r = await post(alice, version=2, reject_cooldown_hours="-3")
    assert r.status_code == 400 and "Wait after a rejection" in html.unescape(r.text)


async def test_it_cannot_be_switched_on_without_an_alliance(db, alice):
    r = await post(alice, a_name_0="", a_name_1="")
    assert r.status_code == 400 and "Add at least one alliance" in html.unescape(r.text)
    assert (await state(db)).enabled is False


async def test_an_incomplete_alliance_says_what_is_missing_and_keeps_what_was_typed(db, alice):
    r = await post(alice, a_reviewers_0=[], a_channel_1="")
    page = html.unescape(r.text)
    assert r.status_code == 400
    assert "Alliance 1: pick at least one reviewer role" in page and "Alliance 2: pick its request channel" in page
    assert 'name="a_name_0" value="Kings Of Chaos"' in r.text
    assert (await state(db)).version == 1  # nothing saved


async def test_a_member_role_cannot_also_decide_requests(db, alice):
    r = await post(alice, a_reviewers_1=str(ONE))
    assert r.status_code == 400
    assert "member role of Kings Of Chaos can't also be a reviewer or override role" in html.unescape(r.text)


async def test_an_old_form_does_not_overwrite_a_newer_save(db, alice):
    assert (await post(alice)).status_code == 303
    r = await post(alice, panel_title="Old tab")
    assert r.status_code == 400 and "Someone else saved" in r.text
    assert (await state(db)).config.panel_title == "Choose your alliance"


async def test_names_are_html_escaped(db, alice):
    evil = "<script>alert(1)</script>"
    assert (await post(alice, a_name_0=evil)).status_code == 303
    page = (await alice.get(URL)).text
    assert evil not in page and "&lt;script&gt;" in page


# ------------------------------------------------------------------ IDs must belong to this server


@pytest.mark.parametrize(
    ("field", "foreign"),
    [
        ("a_role_0", ROLE[GUILD_B]),
        ("a_reviewers_0", ROLE[GUILD_B]),
        ("a_channel_0", TEXT[GUILD_B]),
        ("override_role_ids", ROLE[GUILD_B]),
        ("panel_channel_id", TEXT[GUILD_B]),
        ("log_channel_id", TEXT[GUILD_B]),
    ],
)
async def test_ids_from_another_server_are_refused(db, alice, field, foreign):
    """Otherwise A's join requests could be posted in B, or decided by a role from B."""
    r = await post(alice, **{field: str(foreign)})
    assert r.status_code == 400 and "isn't in this server" in html.unescape(r.text)
    assert (await state(db)).version == 1


async def test_a_role_managed_by_an_integration_cannot_be_a_member_role(db, alice):
    r = await post(alice, a_role_0=str(INTEGRATION))
    assert r.status_code == 400 and "managed by an integration" in html.unescape(r.text)


# ------------------------------------------------------------------ roles with powerful permissions


async def test_manage_server_alone_cannot_make_the_bot_give_out_a_powerful_role(db, alice):
    """Otherwise someone who can't assign roles in Discord could get one through an alliance request."""
    r = await post(alice, a_role_0=str(MODERATOR))
    page = html.unescape(r.text)
    assert r.status_code == 400
    assert "Moderator has powerful permissions (Kick Members)" in page
    assert "Only the server owner or an Administrator" in page
    assert (await state(db)).version == 1


async def test_an_administrator_can_and_the_page_keeps_warning_about_it(db, dave, alice):
    assert (await post(dave, a_role_0=str(MODERATOR))).status_code == 303
    assert "Moderator, which has powerful permissions" in html.unescape((await dave.get(URL)).text)
    # ...and it doesn't lock other managers out of the page afterwards.
    assert (await post(alice, version=2, a_role_0=str(MODERATOR), reject_cooldown_hours="1")).status_code == 303


async def test_powerful_roles_may_review_without_special_rights(db, alice):
    # The King can kick, but JoltBot never gives the King role to anyone: it only reads it.
    assert (await post(alice)).status_code == 303
    assert "powerful permissions" not in (await alice.get(URL)).text


async def test_an_alliance_role_cannot_be_made_a_dashboard_access_role(db, alice):
    """Everyone an R4 accepts would otherwise be let into the dashboard."""
    assert (await post(alice)).status_code == 303
    r = await alice.post(
        f"/servers/{GUILD_A}/access",
        data={"csrf_token": await csrf_of(alice), "version": "1", "dashboard_role_ids": str(ONE)},
    )
    page = html.unescape(r.text)
    assert r.status_code == 400
    assert "JoltBot gives out Kings Of Chaos (Alliances: Member role of Kings Of Chaos)" in page


# ------------------------------------------------------------------ warnings


async def test_the_page_warns_when_tickets_also_hand_out_an_alliance_role(db, alice):
    """Ticket staff could then give that alliance's access without its leadership."""
    assert (await post(alice)).status_code == 303
    assert "is also given out by Tickets" not in html.unescape((await alice.get(URL)).text)

    csrf = await csrf_of(alice)
    tickets = ticket_form(GUILD_A, csrf, 1, extra={"extra_label_0": "KoC", "extra_role_0": str(ONE)})
    assert (await alice.post(f"/servers/{GUILD_A}/tickets", data=tickets)).status_code == 303

    page = html.unescape((await alice.get(URL)).text)
    assert "Kings Of Chaos is also given out by Tickets & verification (Extra role 'KoC')" in page


async def test_the_page_warns_about_a_member_role_above_the_bot(db, alice, fake):
    assert (await post(alice)).status_code == 303
    data = fake.data[GUILD_A]
    data.roles[0] = Role(BOT_ROLE, "JoltBot", 3, ADMINISTRATOR, True, 0)  # now level with Kings Of Chaos
    page = html.unescape((await alice.get(URL)).text)
    assert "JoltBot's role must be above Defenders of Chaos, Kings Of Chaos" in page
