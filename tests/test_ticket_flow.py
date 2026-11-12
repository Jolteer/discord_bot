"""The live ticket flow: apply, accept, deny, close, and what gets logged.

These pin how tickets behave today, so later changes can be checked against
them. Everything runs against fake Discord objects (tests/fakes.py) and a real
SQLite file, so no network or bot token is needed.
"""

from datetime import timedelta
from types import SimpleNamespace

import discord
import pytest
from sqlalchemy import update

from joltbot.core.models import Ticket
from joltbot.core.types import utcnow
from joltbot.modules.tickets import store
from joltbot.modules.tickets.context import load_context
from joltbot.modules.tickets.service import DISCORD_TROUBLE, STUCK_AFTER_SECONDS, TicketManager
from joltbot.modules.tickets.views import (
    NOT_SET_UP,
    AcceptView,
    ApplicationModal,
    CloseConfirmView,
    DenyActionView,
    PanelView,
    TicketControlView,
)

from .conftest import GUILD_A, GUILD_B
from .fakes import (
    BOT_ROLE_POSITION,
    FakeBot,
    FakeGuild,
    FakeRole,
    enable_tickets,
    fake_attachment,
    fake_category,
    fake_interaction,
    fake_member,
    fake_message,
    fake_text_channel,
    http_error,
    settle,
)

STAFF_ROLE, ACCEPT_ROLE, PENDING_ROLE, EXTRA_ROLE = 50, 51, 52, 53
PANEL_CHANNEL, CATEGORY, LOG_CHANNEL = 60, 61, 62
STAFF_ID, APPLICANT_ID, OUTSIDER_ID = 5, 7, 8
ANSWERS = [{"label": "How did you find this server?", "value": "A friend"}]


@pytest.fixture
async def world(db, two_guilds):
    """One configured server: staff, an applicant with the pending role, and the ticket channels."""
    guild = FakeGuild(GUILD_A)
    roles = SimpleNamespace(
        staff=guild.add_role(FakeRole(STAFF_ROLE, "Staff", position=50)),
        accept=guild.add_role(FakeRole(ACCEPT_ROLE, "Member", position=10)),
        pending=guild.add_role(FakeRole(PENDING_ROLE, "Unverified", position=5)),
        extra=guild.add_role(FakeRole(EXTRA_ROLE, "Team A", position=6)),
    )
    fake_category(guild, CATEGORY)
    log = fake_text_channel(guild, LOG_CHANNEL, "ticket-logs")
    staff = fake_member(guild, STAFF_ID, name="staffer", roles=[roles.staff])
    applicant = fake_member(guild, APPLICANT_ID, name="Alice Smith", roles=[roles.pending])
    await enable_tickets(
        db,
        GUILD_A,
        staff_role_ids=[STAFF_ROLE],
        panel_channel_id=PANEL_CHANNEL,
        ticket_category_id=CATEGORY,
        log_channel_id=LOG_CHANNEL,
        pending_role_id=PENDING_ROLE,
        accept_role_ids=[ACCEPT_ROLE],
        extra_role_choices=[{"label": "Team A", "role_id": EXTRA_ROLE}],
        delete_delay_seconds=0,
    )
    bot = FakeBot(db, [guild])
    bot.tickets = TicketManager(bot)
    return SimpleNamespace(
        db=db, bot=bot, manager=bot.tickets, guild=guild, roles=roles, log=log, staff=staff, applicant=applicant
    )


async def _open(world):
    """The applicant submits the form; returns (ticket row, ticket channel)."""
    ctx = await load_context(world.bot, GUILD_A)
    channel, error = await world.manager.create_ticket(world.applicant, ctx, ANSWERS)
    assert error is None
    return await store.get_active_by_user(world.db, GUILD_A, APPLICANT_ID), channel


async def _reload(world, ticket):
    await settle(world.manager)
    return await store.get(world.db, ticket.id)


def _fields(embed):
    return {field.name: field.value for field in embed.fields}


# ------------------------------------------------------------------ opening a ticket


async def test_applying_opens_a_private_ticket_channel(world):
    ticket, channel = await _open(world)

    assert channel.name == "ticket-alice-smith"
    assert channel.category is world.guild.get_channel(CATEGORY)
    assert ticket.status == "open" and ticket.channel_id == channel.id and ticket.answers == ANSWERS

    # Hidden from everyone except the applicant, staff and the bot.
    assert channel.overwrites[world.guild.default_role].view_channel is False
    assert channel.overwrites[world.applicant].view_channel is True
    assert channel.overwrites[world.roles.staff].manage_messages is True
    assert channel.overwrites[world.guild.me].manage_channels is True

    # First message: pings, the application and the staff buttons. Then the intro.
    application, intro = channel.send.await_args_list
    assert application.kwargs["content"] == f"<@{APPLICANT_ID}> <@&{STAFF_ROLE}>"
    assert isinstance(application.kwargs["view"], TicketControlView)
    assert application.kwargs["embed"].footer.text == f"Ticket #{ticket.id}"
    assert _fields(application.kwargs["embed"])["How did you find this server?"] == "A friend"
    assert intro.args[0].startswith(f"Thanks for applying, <@{APPLICANT_ID}>!")


async def test_only_one_open_application_per_member(world):
    _, channel = await _open(world)
    ctx = await load_context(world.bot, GUILD_A)
    second, error = await world.manager.create_ticket(world.applicant, ctx, ANSWERS)
    assert second is None
    assert error == f"You already have an open application: <#{channel.id}>"
    assert len(world.guild.created_channels) == 1


async def test_missing_category_is_reported_and_nothing_is_reserved(world):
    world.guild.remove_channel(CATEGORY)
    ctx = await load_context(world.bot, GUILD_A)
    channel, error = await world.manager.create_ticket(world.applicant, ctx, ANSWERS)
    assert channel is None and "isn't set up correctly" in error
    assert (GUILD_A, "tickets.category") in world.bot.issues
    assert await store.get_active_by_user(world.db, GUILD_A, APPLICANT_ID) is None


@pytest.mark.parametrize(
    ("failure", "expected", "issue"),
    [
        (http_error(discord.Forbidden, 403), "missing permissions", "tickets.create_channel"),
        (http_error(discord.HTTPException, 500), "Discord refused to create the ticket", None),
        (OSError("connection dropped"), DISCORD_TROUBLE, None),
    ],
)
async def test_failed_channel_creation_lets_the_applicant_try_again(world, failure, expected, issue):
    world.guild.create_error = failure
    ctx = await load_context(world.bot, GUILD_A)
    channel, error = await world.manager.create_ticket(world.applicant, ctx, ANSWERS)
    assert channel is None and expected in error
    assert await store.get_active_by_user(world.db, GUILD_A, APPLICANT_ID) is None  # reservation undone
    if issue:
        assert (GUILD_A, issue) in world.bot.issues


async def test_ticket_whose_channel_vanished_no_longer_blocks_a_new_application(world):
    ticket, channel = await _open(world)
    world.guild.remove_channel(channel.id)
    assert await world.manager.active_ticket_for(world.applicant) is None
    ticket = await _reload(world, ticket)
    assert ticket.status == "closed" and ticket.outcome == "channel_deleted"


# ------------------------------------------------------------------ accept


async def test_accept_gives_roles_then_logs_and_deletes_the_ticket(world):
    ticket, channel = await _open(world)
    error = await world.manager.accept(world.guild, world.staff, ticket, world.roles.extra)
    assert error is None

    assert world.roles.accept in world.applicant.roles and world.roles.extra in world.applicant.roles
    assert world.roles.pending not in world.applicant.roles
    welcome = world.applicant.send.await_args.kwargs["embed"]
    assert "was accepted" in welcome.description and "Role: **Team A**" in welcome.description

    ticket = await _reload(world, ticket)
    assert (ticket.status, ticket.outcome, ticket.handled_by) == ("closed", "accepted", STAFF_ID)
    assert ticket.extra_role_id == EXTRA_ROLE and ticket.logged is True and ticket.channel_gone is True

    # The applicant is muted while the transcript is saved.
    muted = channel.set_permissions.await_args
    assert muted.args[0] is world.applicant and muted.kwargs["overwrite"].send_messages is False

    log = world.log.send.await_args.kwargs
    assert log["embed"].title == f"Ticket #{ticket.id} - ✅ Accepted"
    assert _fields(log["embed"])["Handled by"] == f"<@{STAFF_ID}>"
    assert [f.filename for f in log["files"]] == [f"ticket-{ticket.id}-Alice-Smith.txt"]
    channel.delete.assert_awaited_once()
    assert world.guild.get_channel(channel.id) is None


async def test_accept_without_extra_role(world):
    ticket, _ = await _open(world)
    assert await world.manager.accept(world.guild, world.staff, ticket, None) is None
    assert world.roles.accept in world.applicant.roles and world.roles.extra not in world.applicant.roles
    assert (await _reload(world, ticket)).extra_role_id is None


async def test_accept_is_refused_when_the_bot_cannot_give_the_role(world):
    world.roles.accept.position = BOT_ROLE_POSITION + 1  # above JoltBot's own role
    ticket, _ = await _open(world)
    error = await world.manager.accept(world.guild, world.staff, ticket, None)
    assert "is not above **Member**" in error
    world.applicant.add_roles.assert_not_awaited()
    assert (await _reload(world, ticket)).status == "open"


@pytest.mark.parametrize(
    ("failure", "expected"),
    [(http_error(discord.Forbidden, 403), "Discord blocked the role change"), (OSError("dropped"), DISCORD_TROUBLE)],
)
async def test_failed_role_change_reopens_the_ticket(world, failure, expected):
    ticket, channel = await _open(world)
    world.applicant.add_roles.side_effect = failure
    error = await world.manager.accept(world.guild, world.staff, ticket, None)
    assert expected in error
    ticket = await _reload(world, ticket)
    assert ticket.status == "open" and ticket.outcome is None and ticket.handled_by is None
    channel.delete.assert_not_awaited()


async def test_accept_when_the_applicant_already_left(world):
    ticket, _ = await _open(world)
    world.guild.remove_member(APPLICANT_ID)
    error = await world.manager.accept(world.guild, world.staff, ticket, None)
    assert error == "The applicant is no longer in the server. Use 🔒 Close instead."
    assert (await _reload(world, ticket)).status == "open"


async def test_accept_finds_an_applicant_missing_from_the_member_cache(world):
    ticket, _ = await _open(world)
    world.guild.remove_member(APPLICANT_ID)
    world.guild.add_member(world.applicant, cached=False)
    assert await world.manager.accept(world.guild, world.staff, ticket, None) is None
    assert world.roles.accept in world.applicant.roles
    assert (await _reload(world, ticket)).outcome == "accepted"


async def test_closed_dms_do_not_stop_an_accept(world):
    ticket, _ = await _open(world)
    world.applicant.send.side_effect = http_error(discord.Forbidden, 403)
    assert await world.manager.accept(world.guild, world.staff, ticket, None) is None
    ticket = await _reload(world, ticket)
    assert ticket.outcome == "accepted" and "Welcome DM failed" in ticket.notes
    assert "Welcome DM failed" in _fields(world.log.send.await_args.kwargs["embed"])["Notes"]


async def test_only_the_first_decision_counts(world):
    ticket, _ = await _open(world)
    assert await world.manager.accept(world.guild, world.staff, ticket, None) is None
    error = await world.manager.deny(world.guild, world.staff, ticket, "too late", kick=False)
    assert error == "Someone else already handled this ticket."
    assert await world.manager.close(world.staff, ticket) == "Someone else already handled this ticket."
    assert (await _reload(world, ticket)).outcome == "accepted"


# ------------------------------------------------------------------ deny and close


async def test_deny_without_kick_keeps_the_member_pending(world):
    world.applicant.roles.clear()  # e.g. the pending role was removed by hand
    ticket, _ = await _open(world)
    error = await world.manager.deny(world.guild, world.staff, ticket, "Not enough detail", kick=False)
    assert error is None

    dm = world.applicant.send.await_args.kwargs["embed"].description
    assert "**Reason:** Not enough detail" in dm and "open a new application" in dm
    world.applicant.kick.assert_not_awaited()
    assert world.roles.pending in world.applicant.roles and world.roles.accept not in world.applicant.roles

    ticket = await _reload(world, ticket)
    assert (ticket.status, ticket.outcome, ticket.reason, ticket.kicked) == (
        "closed",
        "denied",
        "Not enough detail",
        False,
    )
    assert _fields(world.log.send.await_args.kwargs["embed"])["Action"] == "Not kicked"


async def test_deny_and_kick_sends_the_dm_before_kicking(world):
    ticket, _ = await _open(world)
    order = []
    world.applicant.send.side_effect = lambda **_: order.append("dm")
    world.applicant.kick.side_effect = lambda **_: order.append("kick")
    assert await world.manager.deny(world.guild, world.staff, ticket, "Wrong server", kick=True) is None
    assert order == ["dm", "kick"]

    ticket = await _reload(world, ticket)
    assert ticket.kicked is True
    assert _fields(world.log.send.await_args.kwargs["embed"])["Action"] == "Kicked"


async def test_kick_is_refused_for_members_the_bot_cannot_kick(world):
    world.applicant.roles.append(FakeRole(99, "Leader", position=BOT_ROLE_POSITION))
    world.applicant.top_role = world.applicant.roles[-1]
    ticket, _ = await _open(world)
    error = await world.manager.deny(world.guild, world.staff, ticket, "No", kick=True)
    assert "Discord won't let me kick them" in error
    world.applicant.send.assert_not_awaited()
    assert (await _reload(world, ticket)).status == "open"


async def test_failed_kick_is_noted_and_the_ticket_still_closes(world):
    ticket, _ = await _open(world)
    world.applicant.kick.side_effect = http_error(discord.Forbidden, 403)
    assert await world.manager.deny(world.guild, world.staff, ticket, "No", kick=True) is None
    ticket = await _reload(world, ticket)
    assert ticket.status == "closed" and ticket.kicked is False and "Kick failed (403)" in ticket.notes


async def test_deny_when_the_applicant_already_left(world):
    ticket, _ = await _open(world)
    world.guild.remove_member(APPLICANT_ID)
    assert await world.manager.deny(world.guild, world.staff, ticket, "Gone", kick=True) is None
    ticket = await _reload(world, ticket)
    assert ticket.outcome == "denied" and "Applicant had already left" in ticket.notes
    world.applicant.send.assert_not_awaited()


async def test_close_changes_no_roles(world):
    ticket, channel = await _open(world)
    assert await world.manager.close(world.staff, ticket) is None
    ticket = await _reload(world, ticket)
    assert (ticket.status, ticket.outcome, ticket.handled_by) == ("closed", "closed", STAFF_ID)
    assert world.applicant.roles == [world.roles.pending]
    world.applicant.send.assert_not_awaited()
    channel.delete.assert_awaited_once()


# ------------------------------------------------------------------ logging and automatic closes


async def test_transcript_and_uploaded_files_go_to_the_log_channel(world):
    ticket, channel = await _open(world)
    channel.messages = [fake_message(world.applicant, "here is my screenshot", [fake_attachment("proof.png")])]
    await world.manager.close(world.staff, ticket)
    await settle(world.manager)

    decision, files = world.log.send.await_args_list
    transcript = decision.kwargs["files"][0].fp.read().decode()
    assert "here is my screenshot" in transcript and "[attachment] proof.png" in transcript
    assert "How did you find this server?: A friend" in transcript
    assert files.args[0].startswith(f"📎 Attachments from ticket #{ticket.id}")
    assert [f.filename for f in files.kwargs["files"]] == ["proof.png"]


async def test_ticket_channel_is_kept_when_the_log_cannot_be_posted(world):
    ticket, channel = await _open(world)
    world.guild.remove_channel(LOG_CHANNEL)
    await world.manager.close(world.staff, ticket)
    ticket = await _reload(world, ticket)

    assert ticket.status == "closed" and ticket.logged is False and ticket.channel_gone is False
    channel.delete.assert_not_awaited()
    assert "did **not** delete this channel" in channel.send.await_args.args[0]
    assert (GUILD_A, "tickets.log_channel") in world.bot.issues


async def test_log_channel_without_permission_keeps_the_ticket_channel(world):
    ticket, channel = await _open(world)
    world.log.send.side_effect = http_error(discord.Forbidden, 403)
    await world.manager.close(world.staff, ticket)
    ticket = await _reload(world, ticket)
    assert ticket.logged is False
    channel.delete.assert_not_awaited()
    assert (GUILD_A, "tickets.log_channel") in world.bot.issues


async def test_ticket_closes_when_the_applicant_leaves(world):
    ticket, channel = await _open(world)
    world.guild.remove_member(APPLICANT_ID)
    await world.manager.handle_member_left(GUILD_A, APPLICANT_ID)
    ticket = await _reload(world, ticket)
    assert (ticket.status, ticket.outcome, ticket.handled_by) == ("closed", "left", None)
    assert _fields(world.log.send.await_args.kwargs["embed"])["Handled by"] == "- (automatic)"
    channel.delete.assert_awaited_once()


async def test_ticket_closes_when_its_channel_is_deleted_by_hand(world):
    ticket, channel = await _open(world)
    world.guild.remove_channel(channel.id)
    await world.manager.handle_channel_deleted(channel.id)
    ticket = await _reload(world, ticket)
    assert (ticket.status, ticket.outcome, ticket.channel_gone) == ("closed", "channel_deleted", True)
    log = world.log.send.await_args.kwargs
    assert log["files"] == [] and "No transcript" in _fields(log["embed"])["Notes"]


# ------------------------------------------------------------------ who may press the staff buttons


async def test_staff_buttons_refuse_non_staff(world):
    view = TicketControlView(world.bot)
    click = fake_interaction(world.guild, world.applicant)
    assert await view.interaction_check(click) is False
    click.response.send_message.assert_awaited_once_with("Only staff can do this.", ephemeral=True)


async def test_staff_buttons_allow_staff_roles_and_administrators(world):
    view = TicketControlView(world.bot)
    assert await view.interaction_check(fake_interaction(world.guild, world.staff)) is True
    admin = fake_member(world.guild, OUTSIDER_ID, name="admin", administrator=True)
    assert await view.interaction_check(fake_interaction(world.guild, admin)) is True


async def test_administrators_are_not_staff_when_the_server_says_so(world):
    await enable_tickets(
        world.db,
        GUILD_A,
        staff_role_ids=[STAFF_ROLE],
        allow_administrators=False,
        panel_channel_id=PANEL_CHANNEL,
        ticket_category_id=CATEGORY,
        log_channel_id=LOG_CHANNEL,
        accept_role_ids=[ACCEPT_ROLE],
    )
    admin = fake_member(world.guild, OUTSIDER_ID, name="admin", administrator=True)
    view = TicketControlView(world.bot)
    assert await view.interaction_check(fake_interaction(world.guild, admin)) is False
    assert await view.interaction_check(fake_interaction(world.guild, world.staff)) is True


async def test_staff_buttons_only_act_on_this_servers_open_ticket(world):
    ticket, channel = await _open(world)
    view = TicketControlView(world.bot)

    here = fake_interaction(world.guild, world.staff, channel)
    assert (await view._open_ticket(here)).id == ticket.id

    # The same channel ID arriving from another server must never match.
    elsewhere = fake_interaction(FakeGuild(GUILD_B), world.staff, channel)
    assert await view._open_ticket(elsewhere) is None
    elsewhere.response.send_message.assert_awaited_once_with(
        "This ticket is already closed or being closed.", ephemeral=True
    )

    await store.claim(world.db, ticket.id, outcome="closed", handled_by=STAFF_ID)
    assert await view._open_ticket(fake_interaction(world.guild, world.staff, channel)) is None


@pytest.mark.parametrize("prompt", ["accept", "deny", "close"])
async def test_confirm_prompts_recheck_staff(world, prompt):
    # The prompt stays open for up to 5 minutes; someone can lose their staff role in between.
    ticket, channel = await _open(world)
    ctx = await load_context(world.bot, GUILD_A)
    view = {
        "accept": lambda: AcceptView(world.bot, ticket, ctx.settings),
        "deny": lambda: DenyActionView(world.bot, ticket, "reason"),
        "close": lambda: CloseConfirmView(world.bot, ticket),
    }[prompt]()

    assert await view.interaction_check(fake_interaction(world.guild, world.staff, channel)) is True

    await world.staff.remove_roles(world.roles.staff)
    click = fake_interaction(world.guild, world.staff, channel)
    assert await view.interaction_check(click) is False
    click.response.send_message.assert_awaited_once_with("Only staff can do this.", ephemeral=True)
    view.stop()


# ------------------------------------------------------------------ the panel button and form


async def test_panel_button_opens_the_form(world):
    click = fake_interaction(world.guild, world.applicant)
    await PanelView(world.bot).open_application(click)
    modal = click.response.send_modal.await_args.args[0]
    assert isinstance(modal, ApplicationModal)
    assert [label for label, _ in modal.inputs] == ["How did you find this server?", "Anything else staff should know?"]


async def test_panel_button_when_the_module_is_not_set_up(world):
    other = FakeGuild(GUILD_B)
    click = fake_interaction(other, fake_member(other, APPLICANT_ID))
    await PanelView(world.bot).open_application(click)
    click.response.send_message.assert_awaited_once_with(NOT_SET_UP, ephemeral=True)
    click.response.send_modal.assert_not_awaited()


async def test_panel_button_for_someone_already_verified(world):
    world.applicant.roles.append(world.roles.accept)
    click = fake_interaction(world.guild, world.applicant)
    await PanelView(world.bot).open_application(click)
    click.response.send_message.assert_awaited_once_with("You're already verified. Welcome back! 🎉", ephemeral=True)


async def test_panel_button_with_an_application_already_open(world):
    _, channel = await _open(world)
    click = fake_interaction(world.guild, world.applicant)
    await PanelView(world.bot).open_application(click)
    click.response.send_message.assert_awaited_once_with(
        f"You already have an open application: <#{channel.id}>", ephemeral=True
    )


async def test_submitting_the_form_opens_the_ticket_and_tells_the_applicant(world):
    ctx = await load_context(world.bot, GUILD_A)
    modal = ApplicationModal(world.bot, ctx.settings)
    submit = fake_interaction(world.guild, world.applicant)
    # Discord fills each input the same way when the form comes back.
    modal.inputs[0][1]._refresh_state(submit, {"value": "  A friend  "})

    await modal.on_submit(submit)

    ticket = await store.get_active_by_user(world.db, GUILD_A, APPLICANT_ID)
    assert ticket.answers == [
        {"label": "How did you find this server?", "value": "A friend"},
        {"label": "Anything else staff should know?", "value": ""},
    ]
    submit.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    submit.followup.send.assert_awaited_once_with(
        f"✅ Your application ticket is open: <#{ticket.channel_id}>", ephemeral=True
    )


# ------------------------------------------------------------------ a log post that failed is tried again


async def _close_with_a_broken_log_channel(world):
    """Close a ticket while JoltBot may not post in the log channel; returns (ticket, ticket channel)."""
    ticket, channel = await _open(world)
    world.log.send.side_effect = http_error(discord.Forbidden, 403)
    await world.manager.close(world.staff, ticket)
    ticket = await _reload(world, ticket)
    assert ticket.status == "closed" and ticket.logged is False
    world.log.send.reset_mock(side_effect=True)
    return ticket, channel


async def test_a_failed_log_post_is_retried_once_the_log_channel_works(world):
    ticket, channel = await _close_with_a_broken_log_channel(world)  # ...and then an admin fixes it

    await world.manager.recover(world.guild)
    ticket = await _reload(world, ticket)

    assert ticket.logged is True and ticket.channel_gone is True
    decision = world.log.send.await_args_list[0].kwargs
    assert decision["embed"].title == f"Ticket #{ticket.id} - 🔒 Closed without decision"
    assert decision["files"][0].filename == f"ticket-{ticket.id}-Alice-Smith.txt"
    channel.delete.assert_awaited_once()


async def test_a_log_channel_that_is_still_broken_is_left_alone(world):
    ticket, channel = await _close_with_a_broken_log_channel(world)
    world.log.permissions_for.return_value = discord.Permissions(view_channel=True)  # still may not send
    issues_before = list(world.bot.issues)
    channel.send.reset_mock()

    await world.manager.recover(world.guild)
    ticket = await _reload(world, ticket)

    assert ticket.logged is False and ticket.channel_gone is False
    world.log.send.assert_not_awaited()
    channel.send.assert_not_awaited()  # no second "I did not delete this channel" message
    channel.delete.assert_not_awaited()
    assert world.bot.issues == issues_before  # the dashboard warning is already open


async def test_an_unlogged_ticket_whose_channel_is_gone_is_not_posted_late(world):
    # Tickets copied from the old bot can be "closed, not logged" with a channel that was
    # deleted long ago. They must not all turn up in the log channel after an update.
    ticket, channel = await _close_with_a_broken_log_channel(world)
    world.guild.remove_channel(channel.id)

    await world.manager.recover(world.guild)
    ticket = await _reload(world, ticket)

    assert ticket.channel_gone is True and ticket.logged is False
    world.log.send.assert_not_awaited()


# ------------------------------------------------------------------ an accept cut short by a restart


async def _accept_cut_short(world, **claim):
    """The decision was saved, then the bot stopped before it gave any role. Returns the ticket."""
    ticket, _ = await _open(world)
    assert await store.claim(world.db, ticket.id, outcome="accepted", handled_by=STAFF_ID, **claim)
    long_ago = utcnow() - timedelta(seconds=STUCK_AFTER_SECONDS + 60)
    async with world.db.session() as session, session.begin():
        await session.execute(update(Ticket).where(Ticket.id == ticket.id).values(claimed_at=long_ago))
    return ticket


async def test_an_accept_cut_short_by_a_restart_still_gives_the_roles(world):
    ticket = await _accept_cut_short(world, extra_role_id=EXTRA_ROLE)

    await world.manager.recover(world.guild)
    ticket = await _reload(world, ticket)

    assert world.roles.accept in world.applicant.roles and world.roles.extra in world.applicant.roles
    assert world.roles.pending not in world.applicant.roles
    world.applicant.send.assert_awaited_once()  # the welcome DM they never got
    assert ticket.status == "closed" and ticket.outcome == "accepted"
    assert "Roles were given after a restart." in ticket.note_lines


async def test_resuming_a_finished_accept_changes_nothing_for_the_member(world):
    # Roles and DM went out; only the transcript and log were still to do.
    ticket = await _accept_cut_short(world)
    await world.applicant.add_roles(world.roles.accept)
    await world.applicant.remove_roles(world.roles.pending)
    world.applicant.add_roles.reset_mock()

    await world.manager.recover(world.guild)
    ticket = await _reload(world, ticket)

    world.applicant.add_roles.assert_not_awaited()
    world.applicant.send.assert_not_awaited()
    assert ticket.status == "closed" and ticket.note_lines == []


async def test_roles_that_cannot_be_confirmed_after_a_restart_are_noted_in_the_log(world):
    ticket = await _accept_cut_short(world)
    world.applicant.add_roles.side_effect = http_error(discord.Forbidden, 403)

    await world.manager.recover(world.guild)
    ticket = await _reload(world, ticket)

    assert ticket.status == "closed"
    notes = _fields(world.log.send.await_args_list[0].kwargs["embed"])["Notes"]
    assert "could not be confirmed after a restart" in notes


async def test_an_accepted_applicant_who_left_before_the_restart_is_noted(world):
    ticket = await _accept_cut_short(world)
    world.guild.remove_member(APPLICANT_ID)

    await world.manager.recover(world.guild)
    ticket = await _reload(world, ticket)

    assert ticket.status == "closed"
    assert any("no longer in the server" in note for note in ticket.note_lines)


# ------------------------------------------------------------------ nothing is left behind half-created


async def test_leaving_while_the_ticket_is_being_created_leaves_no_orphan_channel(world):
    create = world.guild.create_text_channel

    async def create_while_the_applicant_leaves(**options):
        channel = await create(**options)
        world.guild.remove_member(APPLICANT_ID)
        await world.manager.handle_member_left(GUILD_A, APPLICANT_ID)
        return channel

    world.guild.create_text_channel = create_while_the_applicant_leaves
    ctx = await load_context(world.bot, GUILD_A)
    channel, error = await world.manager.create_ticket(world.applicant, ctx, ANSWERS)
    await settle(world.manager)

    assert channel is None and "try again" in error
    (created,) = world.guild.created_channels
    created.delete.assert_awaited_once()
    created.send.assert_not_awaited()  # no application posted into a channel nobody owns
    assert await store.get_active_by_user(world.db, GUILD_A, APPLICANT_ID) is None
    assert await store.get_by_channel(world.db, created.id) is None


async def test_recovery_leaves_a_ticket_alone_while_its_channel_is_being_created(world):
    # Discord can take a long time to create a channel when it is rate limiting the bot.
    ticket_id = await store.reserve(world.db, guild_id=GUILD_A, user_id=APPLICANT_ID, username="u", answers=ANSWERS)
    long_ago = utcnow() - timedelta(seconds=STUCK_AFTER_SECONDS + 60)
    async with world.db.session() as session, session.begin():
        await session.execute(update(Ticket).where(Ticket.id == ticket_id).values(created_at=long_ago))
    world.manager._creating.add(ticket_id)

    await world.manager.recover(world.guild)

    assert await store.get(world.db, ticket_id) is not None
