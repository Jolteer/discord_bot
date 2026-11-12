"""The /tickets slash commands: who may run them, and that they answer Discord in time.

Discord gives a command 3 seconds to answer. A command that reads the database
or walks the member list first must say "working on it" (defer) before it does.
"""

from types import SimpleNamespace

import pytest
from discord import app_commands

from joltbot.modules.tickets import cog as tickets_cog
from joltbot.modules.tickets import store
from joltbot.modules.tickets.cog import GrantConfirmView, Tickets, _manage_server_check
from joltbot.modules.tickets.diagnostics import OK, Finding

from .conftest import GUILD_A
from .fakes import FakeBot, FakeGuild, FakeRole, enable_tickets, fake_interaction, fake_member

STAFF_ROLE, ACCEPT_ROLE, PENDING_ROLE = 50, 51, 52
ADMIN_ID, STAFF_ID, OLD_TIMER_ID, APPLICANT_ID, VERIFIED_ID, NEWCOMER_ID = 4, 5, 6, 7, 8, 9


async def _world(db, *, pending_role: bool):
    """A server with one long-time member, one applicant mid-application and one verified member."""
    guild = FakeGuild(GUILD_A)
    roles = SimpleNamespace(
        staff=guild.add_role(FakeRole(STAFF_ROLE, "Staff", position=50)),
        accept=guild.add_role(FakeRole(ACCEPT_ROLE, "Member", position=10)),
        pending=guild.add_role(FakeRole(PENDING_ROLE, "Unverified", position=5)),
    )
    admin = fake_member(guild, ADMIN_ID, name="admin", roles=[roles.accept], manage_guild=True)
    staff = fake_member(guild, STAFF_ID, name="staffer", roles=[roles.staff, roles.accept])
    old_timer = fake_member(guild, OLD_TIMER_ID, name="old-timer")
    applicant = fake_member(guild, APPLICANT_ID, name="applicant")
    fake_member(guild, VERIFIED_ID, name="verified", roles=[roles.accept])
    await enable_tickets(
        db,
        GUILD_A,
        staff_role_ids=[STAFF_ROLE],
        panel_channel_id=60,
        ticket_category_id=61,
        log_channel_id=62,
        pending_role_id=PENDING_ROLE if pending_role else None,
        accept_role_ids=[ACCEPT_ROLE],
    )
    ticket_id = await store.reserve(db, guild_id=GUILD_A, user_id=APPLICANT_ID, username="applicant", answers=[])
    await store.set_channel(db, ticket_id, 7001)
    bot = FakeBot(db, [guild])
    return SimpleNamespace(
        guild=guild, roles=roles, cog=Tickets(bot), admin=admin, staff=staff, old_timer=old_timer, applicant=applicant
    )


@pytest.fixture
async def world(db, two_guilds):
    return await _world(db, pending_role=False)


async def _grant_existing(world):
    """Run /tickets grant-existing as the admin; returns the interaction."""
    interaction = fake_interaction(world.guild, world.admin)
    await world.cog.grant_existing.callback(world.cog, interaction)
    return interaction


# ------------------------------------------------------------------ /tickets grant-existing


async def test_grant_existing_needs_manage_server(world):
    # It hands the accepted role to many members at once, so a staff role is not enough.
    assert world.cog.grant_existing.checks == [_manage_server_check]
    assert await _manage_server_check(fake_interaction(world.guild, world.admin)) is True
    with pytest.raises(app_commands.CheckFailure, match="Manage Server"):
        await _manage_server_check(fake_interaction(world.guild, world.staff))


async def test_grant_existing_leaves_out_members_with_an_open_application(world):
    interaction = await _grant_existing(world)

    reply = interaction.followup.send.await_args
    view = reply.kwargs["view"]
    assert isinstance(view, GrantConfirmView)
    assert view.targets == [world.old_timer]  # not the applicant, not anyone already verified
    assert "**1** member(s) with an open application are left out" in reply.args[0]
    view.stop()


async def test_grant_existing_warns_when_the_server_has_no_pending_role(world):
    interaction = await _grant_existing(world)
    assert "No pending role is set" in interaction.followup.send.await_args.args[0]
    interaction.followup.send.await_args.kwargs["view"].stop()


async def test_grant_existing_skips_pending_members_when_the_server_has_a_pending_role(db, two_guilds):
    world = await _world(db, pending_role=True)
    newcomer = fake_member(world.guild, NEWCOMER_ID, name="newcomer", roles=[world.roles.pending])

    interaction = await _grant_existing(world)

    reply = interaction.followup.send.await_args
    assert newcomer not in reply.kwargs["view"].targets and reply.kwargs["view"].targets == [world.old_timer]
    assert "No pending role is set" not in reply.args[0]
    reply.kwargs["view"].stop()


async def test_grant_existing_gives_the_roles_after_the_confirm_click(world):
    interaction = await _grant_existing(world)
    view = interaction.followup.send.await_args.kwargs["view"]
    view.stop()

    click = fake_interaction(world.guild, world.admin)
    await world.cog.run_grant(click, view.targets, view.roles)

    assert world.roles.accept in world.old_timer.roles
    assert world.roles.accept not in world.applicant.roles  # still has to be accepted by staff
    click.edit_original_response.assert_awaited_with(content="✅ Done: updated **1** member(s).")


# ------------------------------------------------------------------ answering within 3 seconds


async def test_grant_existing_answers_discord_before_doing_the_work(world):
    interaction = await _grant_existing(world)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    interaction.response.send_message.assert_not_awaited()
    interaction.followup.send.await_args.kwargs["view"].stop()


async def test_diagnostics_answers_discord_before_doing_the_work(world, monkeypatch):
    order = []
    interaction = fake_interaction(world.guild, world.admin)
    interaction.response.defer.side_effect = lambda **_: order.append("defer")

    def run_diagnostics(guild, settings, enabled):
        order.append("check")
        return [Finding(OK, "No blocking problems found.")]

    monkeypatch.setattr(tickets_cog, "run_diagnostics", run_diagnostics)
    await world.cog.diagnostics.callback(world.cog, interaction)

    assert order == ["defer", "check"]
    interaction.response.send_message.assert_not_awaited()
    reply = interaction.followup.send.await_args.kwargs
    assert reply["ephemeral"] is True and "No blocking problems found." in reply["embed"].description
