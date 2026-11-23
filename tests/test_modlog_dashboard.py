"""The dashboard's Logging page: saving the mod log settings, and the checks that guard them.

Discord is replaced by FakeDiscord (from test_web_access), so these run offline.
"""

import html
from dataclasses import replace

import pytest
from sqlalchemy import select

from joltbot.core.discord_rest import CATEGORY, TEXT_CHANNEL, Channel, GuildData, Role
from joltbot.core.guild_settings import SYSTEM_ACTOR, load_module, save_module
from joltbot.core.issues import record_issue
from joltbot.core.models import AuditLog
from joltbot.core.permissions import ADMINISTRATOR, BOT_PERMISSIONS, MANAGE_GUILD
from joltbot.modules.modlog.constants import EVENTS, ISSUE_VIEW_AUDIT_LOG
from joltbot.modules.modlog.events import Facts
from joltbot.modules.modlog.forms import parse_form
from joltbot.modules.modlog.rules import should_log
from joltbot.modules.modlog.settings import MAX_IGNORED, ModLogSettings
from joltbot.web.app import create_app

from .conftest import GUILD_A, GUILD_B
from .test_web_access import ALICE, ROLE, TEXT, FakeDiscord, client_for, csrf_of, guild_entry, login, make_config

BOT_ROLE, MODERATOR, MUTED = 5399, 5330, 5331
MEMBER_LOG, MESSAGE_LOG, MOD_LOG, SERVER_LOG, VOICE_LOG, STAFF_CHAT, STAFF_AREA = (
    6401,
    6402,
    6403,
    6404,
    6405,
    6406,
    6490,
)
URL = f"/servers/{GUILD_A}/modlog"
POSTING = sum(
    BOT_PERMISSIONS[name] for name in ("View Channels", "Send Messages", "Embed Links", "Read Message History")
)


def server(bot_permissions=ADMINISTRATOR):
    """Server A with a staff area: one log channel per group, and a staff chat."""
    return GuildData(
        guild_id=GUILD_A,
        roles=[
            Role(BOT_ROLE, "JoltBot", 10, bot_permissions, True, 0),
            Role(MODERATOR, "Moderator", 5, 0, False, 0),
            Role(MUTED, "Muted", 4, 0, False, 0),
            Role(ROLE[GUILD_A], "Member", 1, 0, False, 0),
        ],
        channels=[
            Channel(STAFF_AREA, "Staff", CATEGORY, 0, None),
            Channel(MEMBER_LOG, "member-log", TEXT_CHANNEL, 0, STAFF_AREA),
            Channel(MESSAGE_LOG, "message-log", TEXT_CHANNEL, 1, STAFF_AREA),
            Channel(MOD_LOG, "mod-log", TEXT_CHANNEL, 2, STAFF_AREA),
            Channel(SERVER_LOG, "server-log", TEXT_CHANNEL, 3, STAFF_AREA),
            Channel(VOICE_LOG, "voice-log", TEXT_CHANNEL, 4, STAFF_AREA),
            Channel(STAFF_CHAT, "staff-chat", TEXT_CHANNEL, 5, STAFF_AREA),
            Channel(TEXT[GUILD_A], "general", TEXT_CHANNEL, 6, None),
        ],
        everyone_permissions=0,
        bot_role_ids=[BOT_ROLE],
    )


@pytest.fixture
def fake():
    discord = FakeDiscord()
    discord.data[GUILD_A] = server()
    return discord


@pytest.fixture
async def app(db, two_guilds, fake):
    return create_app(make_config(), db, fake, check_schema=False)


@pytest.fixture
async def alice(app, fake):
    """Manage Server in A; an ordinary member of B."""
    async with client_for(app) as client:
        yield await login(client, fake, ALICE, [guild_entry(GUILD_A, MANAGE_GUILD), guild_entry(GUILD_B, 0)])


def default_events():
    """The event tick boxes as the page first shows them: each event at its default."""
    return {f"ev_{key}": "on" for key, spec in EVENTS.items() if spec.default_on}


def form(csrf, version=1, events=None, **fields):
    """A complete, valid Logging form: four log channels, every event at its default."""
    values = {
        "csrf_token": csrf,
        "version": str(version),
        "enabled": "on",
        "members_channel_id": str(MEMBER_LOG),
        "messages_channel_id": str(MESSAGE_LOG),
        "moderation_channel_id": str(MOD_LOG),
        "server_channel_id": str(SERVER_LOG),
        "voice_channel_id": "",
        "ignore_bots": "on",
        "ignored_user_ids": "",
        **(default_events() if events is None else events),
    }
    values.update(fields)
    return {key: value for key, value in values.items() if value is not None}  # None = the box is not ticked


async def state(db, guild_id=GUILD_A):
    async with db.session() as session:
        return await load_module(session, guild_id, "modlog")


async def post(client, **fields):
    return await client.post(URL, data=form(await csrf_of(client), **fields))


# ------------------------------------------------------------------ the page


async def test_the_page_is_linked_and_only_opens_for_this_servers_dashboard_users(alice):
    page = await alice.get(URL)
    assert page.status_code == 200 and f'href="{URL}"' in page.text  # in the sidebar
    text = html.unescape(page.text)
    assert "Logging is ON" in text and "Member banned" in text and "Moved voice channel" in text
    assert (await alice.get(f"/servers/{GUILD_B}/modlog")).status_code == 403


async def test_a_new_server_sees_every_event_at_its_default(alice):
    page = (await alice.get(URL)).text
    assert 'name="ev_member_ban" checked' in page and 'name="ev_message_delete" checked' in page
    # Off until someone asks for them: they are a lot of posts.
    assert 'name="ev_voice_join" checked' not in page and 'name="ev_avatar_change" checked' not in page
    assert 'name="ignore_bots" checked' in page
    assert page.count("nothing in this group is recorded") == 5  # no channel is picked yet


async def test_saving_stores_the_channels_and_is_audited(db, alice):
    r = await post(alice)
    assert r.status_code == 303 and r.headers["location"] == f"{URL}?saved=1"

    saved = await state(db)
    config = saved.config
    assert saved.enabled is True
    assert (config.members_channel_id, config.messages_channel_id) == (MEMBER_LOG, MESSAGE_LOG)
    assert (config.moderation_channel_id, config.server_channel_id, config.voice_channel_id) == (
        MOD_LOG,
        SERVER_LOG,
        None,
    )
    assert config.events == {}  # nothing was switched away from its default
    assert config.ignore_bots is True

    async with db.session() as session:
        entry = (await session.execute(select(AuditLog).where(AuditLog.module == "modlog"))).scalar_one()
    assert (entry.actor_id, entry.source) == (int(ALICE["id"]), "dashboard")

    page = await alice.get(f"{URL}?saved=1")
    assert "Saved." in page.text
    assert f'<option value="{MOD_LOG}" selected>' in page.text
    # Voice has no channel of its own, but posts in the members channel.
    assert page.text.count("nothing in this group is recorded") == 0


async def test_what_is_saved_is_what_the_bot_then_records(db, alice):
    events = default_events() | {"ev_voice_join": "on"}
    del events["ev_nickname_change"]
    r = await post(alice, events=events, messages_channel_id="", ignored_role_ids=str(MUTED))
    assert r.status_code == 303
    saved = await state(db)

    def recorded(event, **facts):
        return should_log(saved.enabled, saved.config, Facts(event, **facts))

    assert recorded("member_join") and recorded("member_ban")
    assert recorded("voice_join")  # switched on, and voice posts in the members channel
    assert not recorded("nickname_change")  # switched off
    assert not recorded("message_delete")  # that group has no channel
    assert not recorded("member_join", user_role_ids=frozenset({MUTED}))  # an ignored role
    assert recorded("member_ban", user_role_ids=frozenset({MUTED}))  # but a ban is always recorded


async def test_only_events_switched_away_from_their_default_are_stored(db, alice):
    events = default_events() | {"ev_voice_move": "on"}
    del events["ev_member_join"]
    assert (await post(alice, events=events)).status_code == 303
    assert (await state(db)).config.events == {"voice_move": True, "member_join": False}

    page = (await alice.get(URL)).text
    assert 'name="ev_voice_move" checked' in page and 'name="ev_member_join" checked' not in page

    # Ticking them back to their defaults stores nothing again.
    assert (await post(alice, version=2)).status_code == 303
    assert (await state(db)).config.events == {}


async def test_the_ignore_lists_are_saved_and_shown_again(db, alice):
    r = await post(
        alice,
        ignore_bots=None,
        ignored_role_ids=[str(MODERATOR), str(MUTED)],
        ignored_channel_ids=[str(STAFF_AREA), str(TEXT[GUILD_A])],
        ignored_user_ids="111111111111111111\n222222222222222222, 111111111111111111",
    )
    assert r.status_code == 303
    config = (await state(db)).config
    assert config.ignore_bots is False
    assert config.ignored_role_ids == [MODERATOR, MUTED]
    assert config.ignored_channel_ids == [STAFF_AREA, TEXT[GUILD_A]]
    assert config.ignored_user_ids == [111111111111111111, 222222222222222222]  # the repeat is dropped

    page = html.unescape((await alice.get(URL)).text)
    assert f'name="ignored_role_ids" value="{MUTED}" checked' in page
    assert f'name="ignored_channel_ids" value="{STAFF_AREA}" checked' in page and "Category: Staff" in page
    assert "111111111111111111\n222222222222222222</textarea>" in page
    assert 'name="ignore_bots" checked' not in page


async def test_a_setting_the_page_does_not_show_keeps_its_value(db, alice):
    async with db.session() as session, session.begin():
        await save_module(
            session,
            GUILD_A,
            "modlog",
            enabled=False,
            config=ModLogSettings(retention_days=90),
            expected_version=1,
            actor=SYSTEM_ACTOR,
        )
    assert (await post(alice, version=2)).status_code == 303
    assert (await state(db)).config.retention_days == 90


# ------------------------------------------------------------------ what is refused


async def test_it_cannot_be_switched_on_without_a_log_channel(db, alice):
    r = await post(alice, members_channel_id="", messages_channel_id="", moderation_channel_id="", server_channel_id="")
    page = html.unescape(r.text)
    assert r.status_code == 400 and "Can't switch logging on yet" in page and "Pick at least one log channel" in page
    assert (await state(db)).enabled is False

    # Saved switched off, the same form is fine.
    assert (
        await post(
            alice,
            enabled=None,
            members_channel_id="",
            messages_channel_id="",
            moderation_channel_id="",
            server_channel_id="",
        )
    ).status_code == 303


async def test_a_voice_channel_alone_is_enough_to_switch_it_on(db, alice):
    r = await post(
        alice,
        members_channel_id="",
        messages_channel_id="",
        moderation_channel_id="",
        server_channel_id="",
        voice_channel_id=str(VOICE_LOG),
    )
    assert r.status_code == 303 and (await state(db)).enabled is True


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("messages_channel_id", str(TEXT[GUILD_B]), "Messages channel: that channel isn't in this server"),
        (
            "moderation_channel_id",
            str(STAFF_AREA),
            "Moderation channel: that channel isn't in this server",
        ),  # a category
        ("ignored_channel_ids", str(TEXT[GUILD_B]), "Ignored channels: a selected channel isn't in this server"),
        ("ignored_role_ids", str(ROLE[GUILD_B]), "Ignored roles: a selected role isn't in this server"),
    ],
)
async def test_channels_and_roles_from_elsewhere_are_refused(db, alice, field, value, message):
    r = await post(alice, **{field: value})
    assert r.status_code == 400 and message in html.unescape(r.text)
    assert (await state(db)).version == 1  # nothing was saved


async def test_a_member_id_that_is_not_a_number_is_refused_and_what_was_typed_is_kept(db, alice):
    r = await post(alice, ignored_user_ids="@alice\n111111111111111111", ignored_role_ids=str(MUTED))
    page = html.unescape(r.text)
    assert r.status_code == 400 and "Ignored members: every entry must be a member's user ID" in page
    assert "@alice\n111111111111111111</textarea>" in page  # still there to correct
    assert f'name="ignored_role_ids" value="{MUTED}" checked' in page
    assert (await state(db)).version == 1


async def test_the_ignore_lists_have_a_limit(db, alice):
    too_many = "\n".join(str(100000000000000000 + n) for n in range(MAX_IGNORED + 1))
    r = await post(alice, ignored_user_ids=too_many)
    assert r.status_code == 400 and f"Ignored members: at most {MAX_IGNORED} entries." in html.unescape(r.text)


async def test_typed_text_is_shown_back_escaped(alice):
    r = await post(alice, ignored_user_ids="<script>alert(1)</script>")
    assert r.status_code == 400 and "<script>alert(1)</script>" not in r.text


async def test_a_second_save_from_an_old_page_is_refused(db, alice):
    assert (await post(alice)).status_code == 303
    r = await post(alice, ignore_bots=None)  # still carries version 1
    assert r.status_code == 400 and "Someone else saved settings" in html.unescape(r.text)
    assert (await state(db)).config.ignore_bots is True


async def test_an_event_name_the_code_does_not_know_is_dropped(db, alice):
    assert (await post(alice, events=default_events() | {"ev_made_up_event": "on"})).status_code == 303
    assert (await state(db)).config.events == {}


def test_unticked_boxes_are_read_as_off():
    class Form(dict):
        def getlist(self, name):
            return []

    parsed = parse_form(Form())
    assert parsed["ignore_bots"] is False
    assert parsed["events"] == {key: False for key, spec in EVENTS.items() if spec.default_on}
    assert parsed["members_channel_id"] is None and parsed["ignored_user_ids"] == []
    assert "retention_days" not in parsed  # not on the page, so the saved value is kept


# ------------------------------------------------------------------ warnings above the form


async def test_missing_permissions_are_pointed_out(alice, fake):
    fake.data[GUILD_A] = server(bot_permissions=BOT_PERMISSIONS["View Channels"])
    page = html.unescape((await alice.get(URL)).text)
    assert "missing these permissions: Send Messages, Embed Links, Read Message History" in page
    assert "missing View Audit Log, so entries can't say who did something" in page


async def test_a_bot_that_can_post_and_read_the_audit_log_gets_no_warning(alice, fake):
    fake.data[GUILD_A] = server(bot_permissions=POSTING | BOT_PERMISSIONS["View Audit Log"])
    page = html.unescape((await alice.get(URL)).text)
    assert "missing" not in page


async def test_deleted_channels_and_roles_are_pointed_out(db, alice, fake):
    assert (await post(alice, ignored_role_ids=str(MUTED), ignored_channel_ids=str(STAFF_CHAT))).status_code == 303
    before = server()
    fake.data[GUILD_A] = replace(
        before,
        roles=[role for role in before.roles if role.id != MUTED],
        channels=[channel for channel in before.channels if channel.id not in (MOD_LOG, STAFF_CHAT)],
    )

    page = html.unescape((await alice.get(URL)).text)
    assert "The Moderation channel you picked was deleted" in page
    assert "One of the ignored channels was deleted" in page and "One of the ignored roles was deleted" in page


async def test_problems_the_bot_reported_are_shown_on_the_page(db, alice):
    async with db.session() as session, session.begin():
        await record_issue(session, GUILD_A, ISSUE_VIEW_AUDIT_LOG, "JoltBot can't see who did things.")
        await record_issue(session, GUILD_A, "tickets.category", "The ticket category is gone.")
    page = html.unescape((await alice.get(URL)).text)
    assert "JoltBot can't see who did things." in page
    assert "The ticket category is gone." not in page  # another module's problem belongs on its own page
