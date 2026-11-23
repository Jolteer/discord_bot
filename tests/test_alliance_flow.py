"""Alliance join requests: apply, who may decide, accept, reject, and what gets logged.

Everything runs against fake Discord objects (tests/fakes.py) and a real SQLite
file, so no network or bot token is needed.
"""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord import app_commands
from sqlalchemy import update

from joltbot.bot.client import JoltBot
from joltbot.core.config import AppConfig
from joltbot.core.guild_settings import load_module, save_module
from joltbot.core.models import AllianceRequest
from joltbot.core.types import utcnow
from joltbot.modules.alliances import store
from joltbot.modules.alliances.cog import Alliances, _manage_check
from joltbot.modules.alliances.service import AllianceManager
from joltbot.modules.alliances.settings import AllianceSettings
from joltbot.modules.alliances.views import (
    ACCEPT_ID,
    CHOOSE_ID,
    NOT_SET_UP,
    REJECT_ID,
    AlliancePanelView,
    JoinRequestModal,
    RejectReasonModal,
    RequestDecisionView,
)

from .conftest import GUILD_A, GUILD_B
from .fakes import (
    BOT_ROLE_POSITION,
    TEST_ACTOR,
    FakeBot,
    FakeGuild,
    FakeRole,
    fake_interaction,
    fake_member,
    fake_text_channel,
    http_error,
    meet_at,
    record_messages,
)

ONE_R5, ONE_R4, TWO_R5, TWO_R4 = 70, 71, 72, 73  # the reviewer roles
ONE, TWO = 80, 81  # the alliances' member roles
KING, FRIENDS = 90, 95
ONE_REQUESTS, TWO_REQUESTS, LOG_CHANNEL, PANEL_CHANNEL = 60, 61, 62, 63
R5_ONE_ID, R4_ONE_ID, R5_TWO_ID, KING_ID, APPLICANT_ID, OUTSIDER_ID = 3, 4, 5, 6, 7, 8


def _settings(**changes):
    values = dict(
        alliances=[
            {
                "name": "Kings Of Chaos",
                "member_role_id": ONE,
                "reviewer_role_ids": [ONE_R5, ONE_R4],
                "request_channel_id": ONE_REQUESTS,
            },
            {
                "name": "Defenders of Chaos",
                "member_role_id": TWO,
                "reviewer_role_ids": [TWO_R5, TWO_R4],
                "request_channel_id": TWO_REQUESTS,
            },
        ],
        override_role_ids=[KING],
        panel_channel_id=PANEL_CHANNEL,
        log_channel_id=LOG_CHANNEL,
    )
    return AllianceSettings(**{**values, **changes})


async def _save(db, guild_id=GUILD_A, *, enabled=True, **changes):
    async with db.session() as session, session.begin():
        current = await load_module(session, guild_id, "alliances")
        await save_module(
            session,
            guild_id,
            "alliances",
            enabled=enabled,
            config=_settings(**changes),
            expected_version=current.version,
            actor=TEST_ACTOR,
        )


@pytest.fixture
async def world(db, two_guilds):
    """One configured server: two alliances with their leadership, a King, and an applicant."""
    guild = FakeGuild(GUILD_A)
    roles = SimpleNamespace(
        one_r5=guild.add_role(FakeRole(ONE_R5, "Top 1 R5", position=41)),
        one_r4=guild.add_role(FakeRole(ONE_R4, "Top 1 R4", position=40)),
        two_r5=guild.add_role(FakeRole(TWO_R5, "Top 2 R5", position=39)),
        one=guild.add_role(FakeRole(ONE, "Kings Of Chaos", position=10)),
        two=guild.add_role(FakeRole(TWO, "Defenders of Chaos", position=11)),
        king=guild.add_role(FakeRole(KING, "King", position=60)),
        friends=guild.add_role(FakeRole(FRIENDS, "Friends", position=3)),
    )
    channels = SimpleNamespace(
        one=record_messages(fake_text_channel(guild, ONE_REQUESTS, "top1-requests")),
        two=record_messages(fake_text_channel(guild, TWO_REQUESTS, "top2-requests"), first_id=9500),
        log=record_messages(fake_text_channel(guild, LOG_CHANNEL, "alliance-log"), first_id=9800),
        panel=record_messages(fake_text_channel(guild, PANEL_CHANNEL, "choose-your-alliance"), first_id=9900),
    )
    await _save(db)
    bot = FakeBot(db, [guild])
    bot.alliances = AllianceManager(bot)
    return SimpleNamespace(
        db=db,
        bot=bot,
        manager=bot.alliances,
        guild=guild,
        roles=roles,
        channels=channels,
        r5_one=fake_member(guild, R5_ONE_ID, name="r5-one", roles=[roles.one_r5, roles.one]),
        r4_one=fake_member(guild, R4_ONE_ID, name="r4-one", roles=[roles.one_r4, roles.one]),
        r5_two=fake_member(guild, R5_TWO_ID, name="r5-two", roles=[roles.two_r5, roles.two]),
        king=fake_member(guild, KING_ID, name="king", roles=[roles.king]),
        applicant=fake_member(guild, APPLICANT_ID, name="alice", roles=[roles.friends]),
    )


async def _apply(world, member=None, alliance=ONE, name="Lord Alice", governor="12345678"):
    """The member submits the form; returns (request row or None, the text they were shown)."""
    member = member or world.applicant
    text = await world.manager.create_request(member, alliance, name, governor)
    return await store.get_active_by_user(world.db, GUILD_A, member.id), text


async def _reload(world, request):
    return await store.get(world.db, request.id)


def _fields(embed):
    return {field.name: field.value for field in embed.fields}


def _buttons_disabled(message):
    return [item.disabled for item in message.view.children]


# ------------------------------------------------------------------ asking to join


async def test_a_request_is_saved_and_posted_in_that_alliances_channel(world):
    request, text = await _apply(world)

    assert text.startswith("✅") and "Kings Of Chaos" in text
    assert (request.status, request.alliance_role_id, request.alliance_name) == ("pending", ONE, "Kings Of Chaos")
    assert (request.ingame_name, request.governor_id) == ("Lord Alice", "12345678")

    (message,) = world.channels.one.sent
    assert world.channels.two.sent == []  # the other alliance's leadership never sees it
    assert (request.channel_id, request.message_id) == (ONE_REQUESTS, message.id)
    assert isinstance(message.view, RequestDecisionView) and _buttons_disabled(message) == [False, False]
    fields = _fields(message.embed)
    assert f"<@{APPLICANT_ID}>" in fields["Member"]
    assert (fields["In-game name"], fields["Governor ID"]) == ("Lord Alice", "12345678")
    assert fields["Account created"].startswith("<t:") and fields["Joined server"].startswith("<t:")


async def test_a_member_can_only_have_one_pending_request(world):
    first, _ = await _apply(world)
    again, text = await _apply(world, alliance=TWO)

    assert again.id == first.id  # still the first one; no second row
    assert "already have a pending request" in text and "Kings Of Chaos" in text
    assert len(world.channels.one.sent) == 1 and world.channels.two.sent == []


async def test_the_database_itself_refuses_a_second_pending_request(world):
    # Two form submits at the same moment both pass the check above; the unique index decides.
    details = dict(
        guild_id=GUILD_A,
        user_id=APPLICANT_ID,
        username="alice",
        alliance_name="Kings Of Chaos",
        ingame_name="Lord Alice",
        governor_id="1",
        member_joined_at=None,
    )
    assert await store.create(world.db, alliance_role_id=ONE, **details) is not None
    assert await store.create(world.db, alliance_role_id=TWO, **details) is None


async def test_a_member_of_the_alliance_is_told_so(world):
    world.applicant.roles.append(world.roles.one)
    request, text = await _apply(world)
    assert request is None and "already a member of **Kings Of Chaos**" in text
    assert world.channels.one.sent == []


async def test_a_request_that_cannot_be_posted_is_not_left_pending(world):
    world.channels.one.send.side_effect = http_error(discord.Forbidden, 403)
    request, text = await _apply(world)
    assert request is None and text.startswith("❌")
    assert (GUILD_A, "alliances.request_channel") in world.bot.issues
    # Nothing blocks the member from trying again once it is fixed.
    assert await store.get_active_by_user(world.db, GUILD_A, APPLICANT_ID) is None


# ------------------------------------------------------------------ who may decide


@pytest.mark.parametrize("reviewer", ["r5_one", "r4_one"])
async def test_the_alliances_own_r5_and_r4_can_accept(world, reviewer):
    request, _ = await _apply(world)
    text = await world.manager.accept(world.guild, getattr(world, reviewer), request)
    assert text.startswith("✅")
    assert (await _reload(world, request)).status == "accepted"
    assert world.roles.one in world.applicant.roles


async def test_another_alliances_leadership_cannot_decide(world):
    request, _ = await _apply(world)

    for decide in (world.manager.accept, lambda *a: world.manager.reject(*a, "no")):
        text = await decide(world.guild, world.r5_two, request)
        assert "Only **Kings Of Chaos** leadership" in text

    assert (await _reload(world, request)).status == "pending"
    assert world.roles.one not in world.applicant.roles
    world.applicant.send.assert_not_awaited()


async def test_an_override_role_can_decide_for_any_alliance(world):
    request, _ = await _apply(world, alliance=TWO)
    text = await world.manager.accept(world.guild, world.king, request)
    assert text.startswith("✅")
    request = await _reload(world, request)
    assert (request.status, request.decided_by) == ("accepted", KING_ID)
    assert world.roles.two in world.applicant.roles


async def test_seeing_the_channel_or_being_an_administrator_is_not_enough(world):
    request, _ = await _apply(world)
    admin = fake_member(world.guild, OUTSIDER_ID, name="admin", administrator=True, manage_guild=True)
    text = await world.manager.accept(world.guild, admin, request)
    assert "Only **Kings Of Chaos** leadership" in text
    assert (await _reload(world, request)).status == "pending"


async def test_losing_the_reviewer_role_takes_effect_on_the_next_click(world):
    request, _ = await _apply(world)
    await world.r4_one.remove_roles(world.roles.one_r4)
    assert "Only **Kings Of Chaos** leadership" in await world.manager.accept(world.guild, world.r4_one, request)


async def test_nobody_can_decide_their_own_request(world):
    # An R4 of alliance one, and the King, each ask to join alliance two... and hold a role that could decide it.
    await world.king.add_roles(world.roles.two_r5)
    request, _ = await _apply(world, member=world.king, alliance=TWO)
    for decide in (world.manager.accept, lambda *a: world.manager.reject(*a, "")):
        assert "can't decide your own request" in await decide(world.guild, world.king, request)
    assert (await _reload(world, request)).status == "pending"
    assert world.roles.two not in world.king.roles


async def test_a_request_from_another_server_is_never_matched(world):
    request, _ = await _apply(world)
    elsewhere = FakeGuild(GUILD_B)
    stranger = fake_member(elsewhere, KING_ID, roles=[FakeRole(KING, "King", position=60)])
    text = await world.manager.accept(elsewhere, stranger, request)
    assert (await _reload(world, request)).status == "pending" and not text.startswith("✅")


# ------------------------------------------------------------------ accepting


async def test_accept_gives_the_role_marks_the_request_and_tells_everyone(world):
    request, _ = await _apply(world)
    await world.manager.accept(world.guild, world.r5_one, request)

    request = await _reload(world, request)
    assert (request.status, request.decided_by) == ("accepted", R5_ONE_ID) and request.decided_at is not None
    assert world.applicant.roles == [world.roles.friends, world.roles.one]  # other roles are untouched

    message = world.channels.one.sent[0]
    assert _buttons_disabled(message) == [True, True]
    decision = _fields(message.embed)["Decision"]
    assert f"Accepted by <@{R5_ONE_ID}>" in decision and "<t:" in decision

    dm = world.applicant.send.await_args.kwargs["embed"].description
    assert "accepted" in dm and "Kings Of Chaos" in dm

    (log,) = world.channels.log.sent
    assert "Accepted" in log.embed.title and f"<@{R5_ONE_ID}>" in _fields(log.embed)["Decided by"]
    assert "Notes" not in _fields(log.embed)


async def test_switching_keeps_the_old_role_until_the_new_alliance_accepts(world):
    world.applicant.roles.append(world.roles.one)
    request, text = await _apply(world, alliance=TWO)
    assert text.startswith("✅") and request.alliance_role_id == TWO
    assert world.roles.one in world.applicant.roles  # nothing changes while the request waits

    await world.manager.reject(world.guild, world.r5_two, request, "Not on our roster")
    assert world.roles.one in world.applicant.roles and world.roles.two not in world.applicant.roles


async def test_accepting_a_switch_swaps_the_roles_in_one_step(world):
    world.applicant.roles.append(world.roles.one)
    request, _ = await _apply(world, alliance=TWO)

    await world.manager.accept(world.guild, world.r5_two, request)

    assert world.applicant.roles == [world.roles.friends, world.roles.two]
    # One call that sets the whole list: there is no moment with both roles, or with neither.
    world.applicant.edit.assert_awaited_once()
    world.applicant.add_roles.assert_not_awaited()
    world.applicant.remove_roles.assert_not_awaited()


async def test_accept_is_refused_when_the_bot_cannot_give_the_role(world):
    world.roles.one.position = BOT_ROLE_POSITION + 1
    request, _ = await _apply(world)
    text = await world.manager.accept(world.guild, world.r5_one, request)
    assert "above Kings Of Chaos" in text
    assert (await _reload(world, request)).status == "pending"


async def test_a_blocked_role_change_explains_the_fix_and_reopens_the_request(world):
    request, _ = await _apply(world)
    world.applicant.edit.side_effect = http_error(discord.Forbidden, 403)

    text = await world.manager.accept(world.guild, world.r5_one, request)

    assert "Manage Roles" in text and "above" in text
    request = await _reload(world, request)
    assert (request.status, request.decided_by) == ("pending", None)  # leadership can try again
    assert _buttons_disabled(world.channels.one.sent[0]) == [False, False]
    world.applicant.send.assert_not_awaited()


async def test_a_dropped_connection_during_accept_reopens_the_request(world):
    request, _ = await _apply(world)
    world.applicant.edit.side_effect = OSError("connection reset")
    text = await world.manager.accept(world.guild, world.r5_one, request)
    assert "try again" in text and (await _reload(world, request)).status == "pending"


async def test_the_second_reviewer_is_told_who_handled_it(world):
    request, _ = await _apply(world)
    # Both loaded the request while it was pending, then clicked.
    first = await world.manager.accept(world.guild, world.r5_one, request)
    second = await world.manager.reject(world.guild, world.r4_one, request, "too late")

    assert first.startswith("✅") and second == f"Already handled by <@{R5_ONE_ID}>."
    request = await _reload(world, request)
    assert (request.status, request.reason) == ("accepted", None)
    assert world.applicant.send.await_count == 1 and len(world.channels.log.sent) == 1


async def test_a_closed_dm_is_noted_in_the_log_and_nothing_else_changes(world):
    world.applicant.send.side_effect = http_error(discord.Forbidden, 403)
    request, _ = await _apply(world)

    text = await world.manager.accept(world.guild, world.r5_one, request)

    assert text.startswith("✅") and world.roles.one in world.applicant.roles
    (log,) = world.channels.log.sent
    assert "DM" in _fields(log.embed)["Notes"]


async def test_accepting_someone_who_left_closes_the_request(world):
    request, _ = await _apply(world)
    world.guild.remove_member(APPLICANT_ID)

    text = await world.manager.accept(world.guild, world.r5_one, request)

    assert "left the server" in text
    assert (await _reload(world, request)).status == "closed"
    assert _buttons_disabled(world.channels.one.sent[0]) == [True, True]
    assert "left the server" in _fields(world.channels.one.sent[0].embed)["Decision"]
    assert len(world.channels.log.sent) == 1


async def test_a_member_missing_from_the_cache_is_still_found(world):
    request, _ = await _apply(world)
    world.guild.remove_member(APPLICANT_ID)
    world.guild.add_member(world.applicant, cached=False)
    assert (await world.manager.accept(world.guild, world.r5_one, request)).startswith("✅")
    assert world.roles.one in world.applicant.roles


async def test_an_alliance_removed_from_the_settings_cannot_be_accepted(world):
    request, _ = await _apply(world)
    await _save(world.db, alliances=_settings().model_dump()["alliances"][1:])
    text = await world.manager.accept(world.guild, world.king, request)
    assert "no longer set up" in text and world.roles.one not in world.applicant.roles


# ------------------------------------------------------------------ rejecting and the cooldown


async def test_reject_tells_the_member_why_and_changes_no_roles(world):
    request, _ = await _apply(world)
    text = await world.manager.reject(world.guild, world.r4_one, request, "Not on the roster")

    assert text.startswith("❌")
    request = await _reload(world, request)
    assert (request.status, request.decided_by, request.reason) == ("rejected", R4_ONE_ID, "Not on the roster")
    assert world.applicant.roles == [world.roles.friends]
    world.applicant.edit.assert_not_awaited()

    message = world.channels.one.sent[0]
    assert _buttons_disabled(message) == [True, True]
    assert f"Rejected by <@{R4_ONE_ID}>" in _fields(message.embed)["Decision"]
    assert _fields(message.embed)["Reason"] == "Not on the roster"
    assert "Not on the roster" in world.applicant.send.await_args.kwargs["embed"].description
    assert "Rejected" in world.channels.log.sent[0].embed.title


async def test_the_reason_is_optional(world):
    request, _ = await _apply(world)
    await world.manager.reject(world.guild, world.r4_one, request, "   ")
    assert (await _reload(world, request)).reason is None
    assert "Reason" not in _fields(world.channels.one.sent[0].embed)
    assert "No reason was given" in world.applicant.send.await_args.kwargs["embed"].description


async def test_rejecting_someone_who_left_closes_the_request(world):
    request, _ = await _apply(world)
    world.guild.remove_member(APPLICANT_ID)
    assert "left the server" in await world.manager.reject(world.guild, world.r4_one, request, "x")
    assert (await _reload(world, request)).status == "closed"


async def test_a_rejected_member_waits_before_asking_the_same_alliance_again(world):
    request, _ = await _apply(world)
    await world.manager.reject(world.guild, world.r4_one, request, "")

    again, text = await _apply(world)
    assert again is None and "You can ask again <t:" in text
    assert len(world.channels.one.sent) == 1

    # 24 hours later (the default) the same request goes through.
    async with world.db.session() as session, session.begin():
        await session.execute(update(AllianceRequest).values(decided_at=utcnow() - timedelta(hours=24, minutes=1)))
    again, text = await _apply(world)
    assert again is not None and text.startswith("✅")


async def test_a_rejection_does_not_block_a_different_alliance(world):
    # Picking the wrong alliance by mistake must not cost a day.
    request, _ = await _apply(world)
    await world.manager.reject(world.guild, world.r4_one, request, "Wrong alliance")
    other, text = await _apply(world, alliance=TWO)
    assert other is not None and text.startswith("✅")


async def test_the_cooldown_length_is_a_setting(world):
    await _save(world.db, reject_cooldown_hours=0)
    request, _ = await _apply(world)
    await world.manager.reject(world.guild, world.r4_one, request, "")
    again, text = await _apply(world)
    assert again is not None and text.startswith("✅")


# ------------------------------------------------------------------ leaving, cancelling, restarts


async def test_a_pending_request_closes_when_the_member_leaves(world):
    request, _ = await _apply(world)
    world.guild.remove_member(APPLICANT_ID)

    await world.manager.handle_member_left(GUILD_A, APPLICANT_ID)

    assert (await _reload(world, request)).status == "closed"
    assert _buttons_disabled(world.channels.one.sent[0]) == [True, True]
    assert "left the server" in world.channels.log.sent[0].embed.title.lower()


async def test_an_admin_can_cancel_a_request_and_the_member_can_ask_again(world):
    request, _ = await _apply(world)
    text = await world.manager.cancel(world.guild, world.king, request)

    assert text.startswith("✅")
    request = await _reload(world, request)
    assert (request.status, request.decided_by) == ("cancelled", KING_ID)
    assert _buttons_disabled(world.channels.one.sent[0]) == [True, True]
    assert len(world.channels.log.sent) == 1
    again, text = await _apply(world)  # a cancellation carries no cooldown
    assert again is not None and again.id != request.id


async def test_a_decision_cut_short_by_a_restart_can_be_made_again(world):
    request, _ = await _apply(world)
    assert await store.claim(world.db, request.id, decided_by=R5_ONE_ID)  # ...and then the bot stopped

    assert await store.release_stuck(world.db) == 1

    request = await _reload(world, request)
    assert (request.status, request.decided_by) == ("pending", None)
    assert (await world.manager.accept(world.guild, world.r4_one, request)).startswith("✅")


# ------------------------------------------------------------------ the panel, the form and the buttons


async def test_the_views_are_persistent_with_fixed_ids():
    bot = SimpleNamespace()
    panel, decision = AlliancePanelView(bot), RequestDecisionView(bot)
    assert panel.timeout is None and decision.timeout is None
    assert panel.is_persistent() and decision.is_persistent()
    assert [item.custom_id for item in panel.children] == [CHOOSE_ID] == ["joltbot:alliances:choose"]
    assert [item.custom_id for item in decision.children] == [ACCEPT_ID, REJECT_ID]
    assert (ACCEPT_ID, REJECT_ID) == ("joltbot:alliances:accept", "joltbot:alliances:reject")


async def test_the_panel_lists_the_alliances_from_the_settings(world):
    view = AlliancePanelView(world.bot, _settings().alliances)
    (select,) = view.children
    assert [(o.label, o.value) for o in select.options] == [
        ("Kings Of Chaos", str(ONE)),
        ("Defenders of Chaos", str(TWO)),
    ]


def _pick(world, member, alliance):
    click = fake_interaction(world.guild, member)
    click.data = {"values": [str(alliance)]}
    click.message = world.channels.panel.sent[0] if world.channels.panel.sent else SimpleNamespace(edit=AsyncMock())
    return click


async def test_picking_an_alliance_opens_the_form(world):
    click = _pick(world, world.applicant, ONE)
    await AlliancePanelView(world.bot).choose(click)
    modal = click.response.send_modal.await_args.args[0]
    assert isinstance(modal, JoinRequestModal) and modal.alliance_role_id == ONE
    assert modal.children == [modal.ingame_name, modal.governor_id]


async def test_picking_your_own_alliance_stops_before_the_form(world):
    world.applicant.roles.append(world.roles.one)
    click = _pick(world, world.applicant, ONE)
    await AlliancePanelView(world.bot).choose(click)
    click.response.send_modal.assert_not_awaited()
    assert "already a member of **Kings Of Chaos**" in click.response.send_message.await_args.args[0]
    assert click.response.send_message.await_args.kwargs == {"ephemeral": True}


async def test_picking_with_a_request_pending_names_that_alliance(world):
    await _apply(world)
    click = _pick(world, world.applicant, TWO)
    await AlliancePanelView(world.bot).choose(click)
    click.response.send_modal.assert_not_awaited()
    assert "pending request with **Kings Of Chaos**" in click.response.send_message.await_args.args[0]


async def test_an_option_from_an_old_panel_is_refused(world):
    click = _pick(world, world.applicant, 12345)
    await AlliancePanelView(world.bot).choose(click)
    click.response.send_modal.assert_not_awaited()
    assert "no longer" in click.response.send_message.await_args.args[0]


async def test_the_panel_does_nothing_where_the_module_is_off(world):
    other = FakeGuild(GUILD_B)
    click = fake_interaction(other, fake_member(other, APPLICANT_ID))
    click.data = {"values": [str(ONE)]}
    await AlliancePanelView(world.bot).choose(click)
    click.response.send_message.assert_awaited_once_with(NOT_SET_UP, ephemeral=True)


async def test_the_menu_is_reset_after_a_pick_so_the_same_alliance_can_be_picked_again(world, cog):
    # Discord keeps showing the picked option, and picking the shown option again does nothing.
    await cog.panel.callback(cog, fake_interaction(world.guild, world.king))
    panel = world.channels.panel.sent[0]
    await AlliancePanelView(world.bot).choose(_pick(world, world.applicant, ONE))
    panel.edit.assert_awaited_once()
    assert isinstance(panel.view, AlliancePanelView)


async def _submit(world, name, governor):
    modal = JoinRequestModal(world.bot, ONE, "Kings Of Chaos")
    submit = fake_interaction(world.guild, world.applicant)
    modal.ingame_name._refresh_state(submit, {"value": name})
    modal.governor_id._refresh_state(submit, {"value": governor})
    await modal.on_submit(submit)
    return submit


async def test_submitting_the_form_sends_the_request_and_confirms_privately(world):
    submit = await _submit(world, "  Lord Alice ", " 1234 5678 ")

    request = await store.get_active_by_user(world.db, GUILD_A, APPLICANT_ID)
    assert (request.ingame_name, request.governor_id) == ("Lord Alice", "12345678")
    submit.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert submit.followup.send.await_args.args[0].startswith("✅")
    assert submit.followup.send.await_args.kwargs == {"ephemeral": True}


async def test_a_governor_id_must_be_a_number(world):
    submit = await _submit(world, "Lord Alice", "abc")
    assert await store.get_active_by_user(world.db, GUILD_A, APPLICANT_ID) is None
    assert "Governor ID" in submit.response.send_message.await_args.args[0]


def _click(world, member, message):
    click = fake_interaction(world.guild, member, message.channel)
    click.message = message
    return click


async def test_the_accept_button_checks_the_clicker_itself(world):
    await _apply(world)
    message = world.channels.one.sent[0]
    view = RequestDecisionView(world.bot)

    wrong = _click(world, world.r5_two, message)
    await view.accept(wrong)
    wrong.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert "Only **Kings Of Chaos** leadership" in wrong.followup.send.await_args.args[0]
    assert world.roles.one not in world.applicant.roles

    right = _click(world, world.r5_one, message)
    await view.accept(right)
    assert right.followup.send.await_args.args[0].startswith("✅")
    assert world.roles.one in world.applicant.roles


async def test_the_reject_button_opens_the_reason_form_for_reviewers_only(world):
    request, _ = await _apply(world)
    message = world.channels.one.sent[0]
    view = RequestDecisionView(world.bot)

    wrong = _click(world, world.r5_two, message)
    await view.reject(wrong)
    wrong.response.send_modal.assert_not_awaited()
    assert "Only **Kings Of Chaos** leadership" in wrong.response.send_message.await_args.args[0]

    right = _click(world, world.r4_one, message)
    await view.reject(right)
    modal = right.response.send_modal.await_args.args[0]
    assert isinstance(modal, RejectReasonModal) and modal.reason.required is False

    # The form stays open for minutes; the reviewer is checked again when it comes back.
    await world.r4_one.remove_roles(world.roles.one_r4)
    submit = fake_interaction(world.guild, world.r4_one)
    modal.reason._refresh_state(submit, {"value": "No"})
    await modal.on_submit(submit)
    assert "Only **Kings Of Chaos** leadership" in submit.followup.send.await_args.args[0]
    assert (await _reload(world, request)).status == "pending"


async def test_the_reason_form_rejects_the_request(world):
    request, _ = await _apply(world)
    modal = RejectReasonModal(world.bot, request.id)
    submit = fake_interaction(world.guild, world.r4_one)
    modal.reason._refresh_state(submit, {"value": "Not on the roster"})
    await modal.on_submit(submit)
    submit.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert (await _reload(world, request)).reason == "Not on the roster"


async def test_buttons_on_a_decided_request_say_who_handled_it(world):
    request, _ = await _apply(world)
    await world.manager.accept(world.guild, world.r5_one, request)
    for press in ("accept", "reject"):
        click = _click(world, world.r4_one, world.channels.one.sent[0])
        await getattr(RequestDecisionView(world.bot), press)(click)
        click.response.send_message.assert_awaited_once_with(f"Already handled by <@{R5_ONE_ID}>.", ephemeral=True)


async def test_buttons_only_act_on_this_servers_request(world):
    await _apply(world)
    elsewhere = FakeGuild(GUILD_B)
    click = fake_interaction(elsewhere, world.king, world.channels.one)
    click.message = world.channels.one.sent[0]
    await RequestDecisionView(world.bot).accept(click)
    assert "no longer exists" in click.response.send_message.await_args.args[0]
    assert world.roles.one not in world.applicant.roles


# ------------------------------------------------------------------ two reviewers at once


def _told(click):
    """What a reviewer was told after their click: the private follow-up, or an immediate reply."""
    return (click.followup.send.await_args or click.response.send_message.await_args).args[0]


async def test_two_reviewers_clicking_accept_at_once_accept_the_member_once(world, monkeypatch):
    meet_at(monkeypatch, store, "claim")
    request, _ = await _apply(world)
    message = world.channels.one.sent[0]
    clicks = {who.id: _click(world, who, message) for who in (world.r5_one, world.r4_one)}

    await asyncio.gather(*(RequestDecisionView(world.bot).accept(click) for click in clicks.values()))

    request = await _reload(world, request)
    assert request.status == "accepted" and request.decided_by in clicks
    loser = next(user_id for user_id in clicks if user_id != request.decided_by)
    assert _told(clicks[request.decided_by]).startswith("✅")
    assert _told(clicks[loser]) == f"Already handled by <@{request.decided_by}>."
    # The member was let in once: one role change, one DM, one log post.
    world.applicant.edit.assert_awaited_once()
    world.applicant.send.assert_awaited_once()
    assert world.roles.one in world.applicant.roles and len(world.channels.log.sent) == 1


async def test_accept_and_reject_at_the_same_moment_never_both_happen(world, monkeypatch):
    meet_at(monkeypatch, store, "claim", "decide")  # Accept goes through claim, Reject through decide
    request, _ = await _apply(world)
    accept_click = _click(world, world.r5_one, world.channels.one.sent[0])
    # The other reviewer already had the reject reason form open, and submits it now.
    reject_form = RejectReasonModal(world.bot, request.id)
    reject_submit = fake_interaction(world.guild, world.r4_one)
    reject_form.reason._refresh_state(reject_submit, {"value": "Not on the roster"})

    await asyncio.gather(RequestDecisionView(world.bot).accept(accept_click), reject_form.on_submit(reject_submit))

    request = await _reload(world, request)
    world.applicant.send.assert_awaited_once()  # told once, about the decision that won
    assert len(world.channels.log.sent) == 1
    if request.status == "accepted":
        assert world.roles.one in world.applicant.roles and request.reason is None
        assert _told(reject_submit) == f"Already handled by <@{R5_ONE_ID}>."
    else:
        assert request.status == "rejected" and request.reason == "Not on the roster"
        assert world.roles.one not in world.applicant.roles
        world.applicant.edit.assert_not_awaited()
        assert _told(accept_click) == f"Already handled by <@{R4_ONE_ID}>."


# ------------------------------------------------------------------ /alliances commands


@pytest.fixture
def cog(world):
    return Alliances(world.bot)


def _command(world, member):
    interaction = fake_interaction(world.guild, member)
    interaction.client = world.bot
    return interaction


async def test_the_commands_need_manage_server_or_an_override_role(world, cog):
    assert cog.panel.checks == cog.pending.checks == cog.cancel.checks == [_manage_check]
    manager = fake_member(world.guild, OUTSIDER_ID, name="manager", manage_guild=True)
    assert await _manage_check(_command(world, manager)) is True
    assert await _manage_check(_command(world, world.king)) is True
    for member in (world.r5_one, world.applicant):  # leadership decides requests; it doesn't manage the module
        with pytest.raises(app_commands.CheckFailure, match="Manage Server"):
            await _manage_check(_command(world, member))


async def test_panel_posts_the_menu_and_replaces_the_previous_one(world, cog):
    first = fake_interaction(world.guild, world.king)
    await cog.panel.callback(cog, first)
    first.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    (panel,) = world.channels.panel.sent
    assert isinstance(panel.view, AlliancePanelView)
    assert [o.label for o in panel.view.children[0].options] == ["Kings Of Chaos", "Defenders of Chaos"]
    assert "Posted" in first.followup.send.await_args.args[0]

    await cog.panel.callback(cog, fake_interaction(world.guild, world.king))
    assert len(world.channels.panel.sent) == 2
    panel.delete.assert_awaited_once()  # only ever one panel


async def test_panel_says_what_is_missing(world, cog):
    await _save(world.db, enabled=False)
    click = fake_interaction(world.guild, world.king)
    await cog.panel.callback(cog, click)
    assert "switched off" in click.followup.send.await_args.args[0]
    assert world.channels.panel.sent == []


async def test_pending_lists_the_waiting_requests(world, cog):
    click = fake_interaction(world.guild, world.king)
    await cog.pending.callback(cog, click)
    assert "No pending" in click.response.send_message.await_args.args[0]

    request, _ = await _apply(world)
    click = fake_interaction(world.guild, world.king)
    await cog.pending.callback(cog, click)
    listing = click.response.send_message.await_args.kwargs["embed"].description
    assert f"<@{APPLICANT_ID}>" in listing and "Kings Of Chaos" in listing and "12345678" in listing
    assert f"/channels/{GUILD_A}/{ONE_REQUESTS}/{request.message_id}" in listing
    assert click.response.send_message.await_args.kwargs["ephemeral"] is True


async def test_cancel_closes_the_members_pending_request(world, cog):
    request, _ = await _apply(world)
    click = fake_interaction(world.guild, world.king)
    await cog.cancel.callback(cog, click, world.applicant)
    assert click.followup.send.await_args.args[0].startswith("✅")
    assert (await _reload(world, request)).status == "cancelled"

    click = fake_interaction(world.guild, world.king)
    await cog.cancel.callback(cog, click, world.applicant)
    assert "no pending request" in click.followup.send.await_args.args[0]


# ------------------------------------------------------------------ surviving a restart


async def test_the_bot_registers_the_views_and_commands_at_startup(db, two_guilds):
    """The real bot, started against a database that already holds a half-finished accept."""
    request_id = await store.create(
        db,
        guild_id=GUILD_A,
        user_id=APPLICANT_ID,
        username="alice",
        alliance_role_id=ONE,
        alliance_name="Kings Of Chaos",
        ingame_name="Lord Alice",
        governor_id="1",
        member_joined_at=None,
    )
    await store.claim(db, request_id, decided_by=R5_ONE_ID)

    bot = JoltBot(AppConfig(_env_file=None, database_url=str(db.engine.url)))
    async with bot:
        await bot.setup_hook()
        assert bot.failed_modules == []
        assert isinstance(bot.alliances, AllianceManager)
        # Old messages keep working: discord.py routes a click by these IDs to the registered views.
        registered = {item.custom_id for view in bot.persistent_views for item in view.children}
        assert {CHOOSE_ID, ACCEPT_ID, REJECT_ID} <= registered
        group = bot.tree.get_command("alliances")
        assert {command.name for command in group.commands} == {"panel", "pending", "cancel"}
        assert group.default_permissions.manage_guild and group.guild_only

    assert (await store.get(db, request_id)).status == "pending"  # reopened, so its buttons work again
