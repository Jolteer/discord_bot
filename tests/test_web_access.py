"""Dashboard security: login, CSRF, and above all that one server's admins can
never read or change another server's settings.

Discord is replaced by FakeDiscord, so these run offline. The login itself
goes through the real /login -> /auth/callback flow.
"""

import html
import re
from dataclasses import replace
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select, update

from joltbot.core.config import AppConfig
from joltbot.core.discord_rest import CATEGORY, TEXT_CHANNEL, Channel, DiscordError, Emoji, GuildData, Role
from joltbot.core.guild_settings import SYSTEM_ACTOR, load_module, save_module
from joltbot.core.health import write_heartbeat
from joltbot.core.issues import record_issue
from joltbot.core.models import AuditLog, Guild, GuildIssue, WebSession
from joltbot.core.permissions import ADMINISTRATOR, ELEVATED_PERMISSIONS, MANAGE_GUILD
from joltbot.core.types import utcnow
from joltbot.modules.registry import MODULES, ModuleInfo
from joltbot.web.app import create_app
from joltbot.web.security import parse_user_guilds

from .conftest import GUILD_A, GUILD_B
from .example_module.settings import HelloSettings

GUILD_NO_BOT = 444444444444444444
ALICE = {"id": "1001", "username": "alice"}  # admin of A only
BOB = {"id": "1002", "username": "bob"}  # admin of B only
CAROL = {"id": "1003", "username": "carol"}  # plain member of A

# Each server's real roles/channels: IDs are made up but distinct per server.
ROLE = {GUILD_A: 5100, GUILD_B: 5200}
TEXT = {GUILD_A: 6100, GUILD_B: 6200}
CAT = {GUILD_A: 7100, GUILD_B: 7200}
BOT_ROLE = {GUILD_A: 5199, GUILD_B: 5299}
KICK_MEMBERS, MANAGE_MESSAGES = ELEVATED_PERMISSIONS["Kick Members"], ELEVATED_PERMISSIONS["Manage Messages"]


def guild_entry(guild_id, perms, owner=False):
    return {"id": str(guild_id), "name": f"Server {guild_id}", "icon": None, "owner": owner, "permissions": str(perms)}


def guild_data(guild_id):
    return GuildData(
        guild_id=guild_id,
        roles=[
            Role(BOT_ROLE[guild_id], "JoltBot", 10, ADMINISTRATOR, True, 0),
            Role(ROLE[guild_id], "Member", 2, 0, False, 0),
        ],
        channels=[
            Channel(TEXT[guild_id], "general", TEXT_CHANNEL, 0, CAT[guild_id]),
            Channel(CAT[guild_id], "Tickets", CATEGORY, 1, None),
        ],
        everyone_permissions=0,
        bot_role_ids=[BOT_ROLE[guild_id]],
    )


class FakeDiscord:
    def __init__(self):
        self.logins = {}  # code -> (user, guilds)
        self.tokens = {}  # access token -> [user, guilds]
        self.data = {GUILD_A: guild_data(GUILD_A), GUILD_B: guild_data(GUILD_B)}
        self.members = {}  # (guild ID, user ID) -> the role IDs that member holds

    async def member_role_ids(self, guild_id, user_id):
        return frozenset(self.members.get((guild_id, user_id), ()))

    def authorize_url(self, state):
        return f"https://discord.test/authorize?state={state}"

    async def exchange_code(self, code):
        if code not in self.logins:
            raise DiscordError("bad code", 400)
        token = f"token-{code}"
        self.tokens[token] = list(self.logins[code])
        return {"access_token": token, "expires_in": 604800, "scope": "identify guilds"}

    async def get_user(self, token):
        return self.tokens[token][0]

    async def get_user_guilds(self, token):
        return self.tokens[token][1]

    async def guild_data(self, guild_id, *, refresh=False):
        return self.data[guild_id]

    def forget_guild(self, guild_id):
        pass

    async def aclose(self):
        pass


def make_config(**overrides):
    values = dict(
        discord_token="bot",
        discord_client_id="999",
        discord_client_secret="s",
        session_secret="test-secret",
        public_base_url="http://localhost:8000",
    )
    return AppConfig(**{**values, **overrides})


@pytest.fixture
def fake():
    return FakeDiscord()


@pytest.fixture
async def app(db, two_guilds, fake):
    return create_app(make_config(), db, fake, check_schema=False)


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000")


async def login(client, fake, user, guilds):
    code = f"code-{user['id']}"
    fake.logins[code] = (user, guilds)
    r = await client.get("/login")
    assert r.status_code == 303
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    r = await client.get("/auth/callback", params={"code": code, "state": state})
    assert r.status_code == 303 and r.headers["location"] == "/servers"
    return client


@pytest.fixture
async def alice(app, fake):
    async with client_for(app) as client:
        yield await login(
            client,
            fake,
            ALICE,
            [guild_entry(GUILD_A, MANAGE_GUILD), guild_entry(GUILD_B, 0), guild_entry(GUILD_NO_BOT, ADMINISTRATOR)],
        )


@pytest.fixture
async def bob(app, fake):
    async with client_for(app) as client:
        yield await login(client, fake, BOB, [guild_entry(GUILD_B, 0, owner=True)])


async def csrf_of(client):
    page = await client.get("/servers")
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


def ticket_form(guild_id, csrf, version, extra=None, **fields):
    """A complete, valid ticket settings form for this guild."""
    form = {
        "csrf_token": csrf,
        "version": str(version),
        "enabled": "on",
        "staff_role_ids": str(ROLE[guild_id]),
        "panel_channel_id": str(TEXT[guild_id]),
        "ticket_category_id": str(CAT[guild_id]),
        "log_channel_id": str(TEXT[guild_id]),
        "accept_role_ids": str(ROLE[guild_id]),
        "q_label_0": "Why join?",
        "q_max_0": "200",
        "delete_delay_seconds": "10",
        "msg_panel_title": "Hello",
        "msg_panel_description": "Hi {server}",
        "msg_panel_button_label": "Apply",
        "msg_form_title": "Apply",
        "msg_ticket_opened": "Open: {channel}",
        "msg_ticket_intro": "Hi {user}",
        "msg_not_staff": "No",
        "msg_already_verified": "Done",
        "msg_welcome_dm": "Welcome",
        "msg_deny_dm": "No: {reason}",
    }
    form.update(fields)
    form.update(extra or {})
    return form


async def tickets_state(db, guild_id):
    async with db.session() as session:
        return await load_module(session, guild_id, "tickets")


# --------------------------------------------------------------------------- public pages & login


async def test_landing_page_is_public(app):
    async with client_for(app) as client:
        r = await client.get("/")
    assert r.status_code == 200 and "Add to Discord" in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


async def test_server_list_requires_login(app):
    async with client_for(app) as client:
        r = await client.get("/servers")
    assert r.status_code == 303 and r.headers["location"] == "/login"


async def test_callback_rejects_wrong_state(app, fake):
    fake.logins["c"] = (ALICE, [])
    async with client_for(app) as client:
        await client.get("/login")
        r = await client.get("/auth/callback", params={"code": "c", "state": "attacker-chosen"})
    assert r.status_code == 400
    assert "joltbot_session" not in r.headers.get("set-cookie", "")


async def test_callback_rejects_missing_state_cookie(app, fake):
    fake.logins["c"] = (ALICE, [])
    async with client_for(app) as client:  # never visited /login, so no state cookie
        r = await client.get("/auth/callback", params={"code": "c", "state": "anything"})
    assert r.status_code == 400


async def test_state_is_single_use(app, fake):
    fake.logins["c"] = (ALICE, [])
    async with client_for(app) as client:
        r = await client.get("/login")
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        assert (await client.get("/auth/callback", params={"code": "c", "state": state})).status_code == 303
        r = await client.get("/auth/callback", params={"code": "c", "state": state})
    assert r.status_code == 400


async def test_session_cookie_flags(db, two_guilds, fake):
    app = create_app(make_config(public_base_url="https://joltbot.example.com"), db, fake, check_schema=False)
    fake.logins["c"] = (ALICE, [])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://joltbot.example.com") as c:
        r = await c.get("/login")
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        r = await c.get("/auth/callback", params={"code": "c", "state": state})
    cookie = [h for h in r.headers.get_list("set-cookie") if h.startswith("__Host-joltbot_session=")][0].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie and "path=/" in cookie


async def test_session_token_is_not_stored_in_plain_text(db, alice):
    raw = alice.cookies.get("joltbot_session")
    async with db.session() as session:
        ids = list((await session.execute(select(WebSession.id))).scalars())
    assert raw not in ids and len(ids) == 1


# --------------------------------------------------------------------------- server picker


async def test_picker_splits_servers(alice):
    r = await alice.get("/servers")
    assert r.status_code == 200
    assert f'href="/servers/{GUILD_A}"' in r.text  # bot is in A: Manage
    assert f"guild_id={GUILD_NO_BOT}" in r.text  # admin but bot not there: Invite
    assert f"/servers/{GUILD_B}" not in r.text  # B: not an admin there, not listed at all


# --------------------------------------------------------------------------- cross-server isolation


@pytest.mark.parametrize("path", ["", "/tickets", "/general", "/audit"])
async def test_admin_of_a_cannot_open_b_pages(alice, path):
    assert (await alice.get(f"/servers/{GUILD_A}{path}")).status_code == 200
    assert (await alice.get(f"/servers/{GUILD_B}{path}")).status_code == 403


async def test_admin_of_a_cannot_save_b_settings(db, alice):
    csrf = await csrf_of(alice)
    before = await tickets_state(db, GUILD_B)
    r = await alice.post(f"/servers/{GUILD_B}/tickets", data=ticket_form(GUILD_B, csrf, before.version))
    assert r.status_code == 403
    after = await tickets_state(db, GUILD_B)
    assert after.version == before.version and after.enabled is False


async def test_guild_id_in_form_body_is_ignored(db, alice):
    csrf = await csrf_of(alice)
    version = (await tickets_state(db, GUILD_A)).version
    r = await alice.post(
        f"/servers/{GUILD_A}/tickets", data=ticket_form(GUILD_A, csrf, version, extra={"guild_id": str(GUILD_B)})
    )
    assert r.status_code == 303
    assert (await tickets_state(db, GUILD_A)).enabled is True
    assert (await tickets_state(db, GUILD_B)).enabled is False


async def test_ids_from_another_server_are_rejected(db, alice):
    """Alice can't point A's log channel at a channel in B (that would leak A's transcripts into B)."""
    csrf = await csrf_of(alice)
    version = (await tickets_state(db, GUILD_A)).version
    for field, foreign in (
        ("log_channel_id", TEXT[GUILD_B]),
        ("staff_role_ids", ROLE[GUILD_B]),
        ("ticket_category_id", CAT[GUILD_B]),
        ("accept_role_ids", ROLE[GUILD_B]),
    ):
        r = await alice.post(
            f"/servers/{GUILD_A}/tickets", data=ticket_form(GUILD_A, csrf, version, **{field: str(foreign)})
        )
        assert r.status_code == 400, field
        assert "isn't in this server" in html.unescape(r.text)
    assert (await tickets_state(db, GUILD_A)).version == version  # nothing saved


async def test_member_without_manage_server_is_refused(app, fake):
    async with client_for(app) as client:
        await login(client, fake, CAROL, [guild_entry(GUILD_A, 1 << 10)])  # View Channels only
        assert (await client.get(f"/servers/{GUILD_A}/tickets")).status_code == 403
        r = await client.get("/servers")
    assert f"/servers/{GUILD_A}" not in r.text


async def test_owner_without_permission_bits_is_allowed(bob):
    assert (await bob.get(f"/servers/{GUILD_B}/tickets")).status_code == 200
    assert (await bob.get(f"/servers/{GUILD_A}/tickets")).status_code == 403


async def test_server_without_bot_is_404(alice):
    assert (await alice.get(f"/servers/{GUILD_NO_BOT}/tickets")).status_code == 404


async def test_non_admin_cannot_tell_whether_bot_is_in_a_server(alice):
    # 403 for B (bot is there) and for a random server (bot isn't): same answer, nothing leaks.
    assert (await alice.get(f"/servers/{GUILD_B}")).status_code == 403
    assert (await alice.get("/servers/555555555555555555")).status_code == 403


async def test_audit_log_only_shows_own_server(db, alice, bob):
    csrf = await csrf_of(bob)
    version = (await tickets_state(db, GUILD_B)).version
    form = ticket_form(GUILD_B, csrf, version, msg_panel_title="Bob's secret title")
    assert (await bob.post(f"/servers/{GUILD_B}/tickets", data=form)).status_code == 303
    r = await alice.get(f"/servers/{GUILD_A}/audit")
    assert r.status_code == 200 and "Bob's secret title" not in html.unescape(r.text)
    r = await bob.get(f"/servers/{GUILD_B}/audit")
    assert "Bob's secret title" in html.unescape(r.text)


async def test_cannot_dismiss_another_servers_issue(db, alice):
    async with db.session() as session, session.begin():
        await record_issue(session, GUILD_B, "tickets.test", "B's problem")
    async with db.session() as session:
        issue_id = (await session.execute(select(GuildIssue.id))).scalar_one()
    csrf = await csrf_of(alice)
    # Alice uses HER server's URL with B's issue ID.
    r = await alice.post(f"/servers/{GUILD_A}/issues/{issue_id}/dismiss", data={"csrf_token": csrf})
    assert r.status_code == 404
    async with db.session() as session:
        assert (await session.get(GuildIssue, issue_id)) is not None


async def test_losing_manage_server_takes_effect_after_cache(db, alice, fake):
    assert (await alice.get(f"/servers/{GUILD_A}")).status_code == 200
    fake.tokens["token-code-1001"][1] = [guild_entry(GUILD_A, 0)]  # permission removed in Discord
    async with db.session() as session, session.begin():  # pretend 5+ minutes passed
        await session.execute(update(WebSession).values(guilds_fetched_at=utcnow() - timedelta(minutes=6)))
    assert (await alice.get(f"/servers/{GUILD_A}")).status_code == 403


# --------------------------------------------------------------------------- CSRF & sessions


async def test_post_without_csrf_token_is_refused(db, alice):
    version = (await tickets_state(db, GUILD_A)).version
    form = ticket_form(GUILD_A, "", version)
    del form["csrf_token"]
    assert (await alice.post(f"/servers/{GUILD_A}/tickets", data=form)).status_code == 403
    assert (await tickets_state(db, GUILD_A)).enabled is False


async def test_post_with_wrong_csrf_token_is_refused(db, alice):
    version = (await tickets_state(db, GUILD_A)).version
    r = await alice.post(f"/servers/{GUILD_A}/tickets", data=ticket_form(GUILD_A, "wrong", version))
    assert r.status_code == 403


async def test_post_from_another_website_is_refused(db, alice):
    csrf = await csrf_of(alice)
    version = (await tickets_state(db, GUILD_A)).version
    r = await alice.post(
        f"/servers/{GUILD_A}/tickets",
        data=ticket_form(GUILD_A, csrf, version),
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403


async def test_csrf_header_works_for_htmx(db, alice):
    csrf = await csrf_of(alice)
    r = await alice.post(f"/servers/{GUILD_A}/refresh", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 303


async def test_logout_kills_the_session_server_side(alice):
    old_cookie = alice.cookies.get("joltbot_session")
    csrf = await csrf_of(alice)
    assert (await alice.post("/logout", data={"csrf_token": csrf})).status_code == 303
    alice.cookies.set("joltbot_session", old_cookie)  # an attacker replaying the old cookie
    assert (await alice.get("/servers")).status_code == 303  # back to login


async def test_expired_session_is_refused(db, alice):
    async with db.session() as session, session.begin():
        await session.execute(update(WebSession).values(expires_at=utcnow() - timedelta(seconds=1)))
    assert (await alice.get("/servers")).status_code == 303


# --------------------------------------------------------------------------- saving


async def test_save_ticket_settings_and_audit(db, alice):
    csrf = await csrf_of(alice)
    version = (await tickets_state(db, GUILD_A)).version
    r = await alice.post(f"/servers/{GUILD_A}/tickets", data=ticket_form(GUILD_A, csrf, version))
    assert r.status_code == 303
    state = await tickets_state(db, GUILD_A)
    assert state.enabled and state.config.log_channel_id == TEXT[GUILD_A]
    assert state.config.form_questions[0].label == "Why join?"
    async with db.session() as session:
        entry = (await session.execute(select(AuditLog).where(AuditLog.guild_id == GUILD_A))).scalar_one()
    assert entry.actor_id == int(ALICE["id"]) and entry.source == "dashboard"


async def test_cannot_switch_on_before_setup(db, alice):
    csrf = await csrf_of(alice)
    version = (await tickets_state(db, GUILD_A)).version
    r = await alice.post(f"/servers/{GUILD_A}/tickets", data=ticket_form(GUILD_A, csrf, version, log_channel_id=""))
    assert r.status_code == 400 and "log channel" in r.text.lower()


async def test_stale_form_does_not_overwrite(db, alice):
    csrf = await csrf_of(alice)
    version = (await tickets_state(db, GUILD_A)).version
    assert (
        await alice.post(f"/servers/{GUILD_A}/tickets", data=ticket_form(GUILD_A, csrf, version))
    ).status_code == 303
    r = await alice.post(
        f"/servers/{GUILD_A}/tickets", data=ticket_form(GUILD_A, csrf, version, msg_panel_title="Old tab")
    )
    assert r.status_code == 400 and "Someone else saved" in r.text
    assert (await tickets_state(db, GUILD_A)).config.messages.panel_title == "Hello"


async def test_messages_are_html_escaped(db, alice):
    csrf = await csrf_of(alice)
    version = (await tickets_state(db, GUILD_A)).version
    evil = "<script>alert(1)</script>"
    await alice.post(f"/servers/{GUILD_A}/tickets", data=ticket_form(GUILD_A, csrf, version, msg_panel_title=evil))
    r = await alice.get(f"/servers/{GUILD_A}/tickets")
    assert evil not in r.text and "&lt;script&gt;" in r.text


async def test_general_page_toggles_modules(db, alice):
    csrf = await csrf_of(alice)
    # Tickets aren't set up, so switching them on must fail.
    r = await alice.post(
        f"/servers/{GUILD_A}/general",
        data={
            "csrf_token": csrf,
            "embed_color": "#123456",
            "enabled_tickets": "on",
            "version_general": "1",
            "version_tickets": "1",
        },
    )
    assert r.status_code == 400 and "isn't set up" in html.unescape(r.text)
    r = await alice.post(
        f"/servers/{GUILD_A}/general",
        data={"csrf_token": csrf, "embed_color": "#123456", "version_general": "1", "version_tickets": "1"},
    )
    assert r.status_code == 303
    async with db.session() as session:
        assert (await load_module(session, GUILD_A, "general")).config.embed_color == "#123456"


# --------------------------------------------------------------------------- bot status banner


async def test_overview_warns_when_the_bot_is_offline(db, alice):
    page = html.unescape((await alice.get(f"/servers/{GUILD_A}")).text)
    assert "JoltBot is offline right now" in page and "last connected" not in page  # it never reported in

    async with db.session() as session, session.begin():
        await write_heartbeat(session, utcnow() - timedelta(minutes=10))
    page = html.unescape((await alice.get(f"/servers/{GUILD_A}")).text)
    assert "JoltBot is offline right now" in page and "It was last connected at" in page


async def test_overview_has_no_warning_while_the_bot_is_online(db, alice):
    async with db.session() as session, session.begin():
        await write_heartbeat(session)
    assert "offline" not in (await alice.get(f"/servers/{GUILD_A}")).text


# --------------------------------------------------------------------------- notices channel (General page)


def _general_form(csrf, **fields):
    return {"csrf_token": csrf, "embed_color": "#123456", "version_general": "1", "version_tickets": "1", **fields}


async def _general(db, guild_id=GUILD_A):
    async with db.session() as session:
        return (await load_module(session, guild_id, "general")).config


async def test_general_page_offers_this_servers_channels(alice):
    page = (await alice.get(f"/servers/{GUILD_A}/general")).text
    assert 'name="notice_channel_id"' in page
    assert f'value="{TEXT[GUILD_A]}"' in page and f'value="{TEXT[GUILD_B]}"' not in page


async def test_picking_and_clearing_the_notices_channel(db, alice):
    csrf = await csrf_of(alice)
    r = await alice.post(f"/servers/{GUILD_A}/general", data=_general_form(csrf, notice_channel_id=str(TEXT[GUILD_A])))
    assert r.status_code == 303
    assert (await _general(db)).notice_channel_id == TEXT[GUILD_A]
    assert f'value="{TEXT[GUILD_A]}" selected' in (await alice.get(f"/servers/{GUILD_A}/general")).text

    r = await alice.post(
        f"/servers/{GUILD_A}/general", data=_general_form(csrf, notice_channel_id="", version_general="2")
    )
    assert r.status_code == 303
    assert (await _general(db)).notice_channel_id is None


@pytest.mark.parametrize("bad", [str(TEXT[GUILD_B]), "999999", "not-a-number"])
async def test_a_channel_from_another_server_is_refused(db, alice, bad):
    csrf = await csrf_of(alice)
    r = await alice.post(f"/servers/{GUILD_A}/general", data=_general_form(csrf, notice_channel_id=bad))
    assert r.status_code == 400 and "Notices channel" in html.unescape(r.text)
    assert (await _general(db)).notice_channel_id is None
    assert (await _general(db)).embed_color != "#123456"  # nothing was saved


async def test_general_page_still_works_when_discord_is_down(db, alice, fake):
    csrf = await csrf_of(alice)
    await alice.post(f"/servers/{GUILD_A}/general", data=_general_form(csrf, notice_channel_id=str(TEXT[GUILD_A])))

    async def down(guild_id, *, refresh=False):
        raise DiscordError("Discord is down", 503)

    fake.guild_data = down
    page = await alice.get(f"/servers/{GUILD_A}/general")
    assert page.status_code == 200 and "Couldn't load this server's channels" in html.unescape(page.text)

    # Saving other settings keeps the channel that was already picked...
    r = await alice.post(
        f"/servers/{GUILD_A}/general",
        data=_general_form(csrf, embed_color="#abcdef", notice_channel_id=str(TEXT[GUILD_A]), version_general="2"),
    )
    assert r.status_code == 303
    config = await _general(db)
    assert (config.embed_color, config.notice_channel_id) == ("#abcdef", TEXT[GUILD_A])

    # ...but a different channel can't be checked against the server, so it's refused.
    r = await alice.post(
        f"/servers/{GUILD_A}/general",
        data=_general_form(csrf, notice_channel_id="6101", version_general="3"),
    )
    assert r.status_code == 400 and "Try again in a minute" in html.unescape(r.text)
    assert (await _general(db)).notice_channel_id == TEXT[GUILD_A]


# --------------------------------------------------------------------------- roles with powerful permissions

MOD_ROLE = 5150
DAVE = {"id": "1004", "username": "dave"}  # Administrator in A
POWERFUL = "has powerful permissions (Kick Members, Manage Messages)"


@pytest.fixture
def mod_role(fake):
    """Server A also has a Moderator role that can kick and delete messages, below JoltBot's role."""
    data = fake.data[GUILD_A]
    moderator = Role(MOD_ROLE, "Moderator", 3, KICK_MEMBERS | MANAGE_MESSAGES, False, 0)
    fake.data[GUILD_A] = replace(data, roles=[*data.roles, moderator])


@pytest.fixture
async def dave(app, fake):
    async with client_for(app) as client:
        yield await login(client, fake, DAVE, [guild_entry(GUILD_A, ADMINISTRATOR)])


def _granting(field):
    """Form fields that make JoltBot give out the Moderator role in the given way."""
    if field == "extra":
        return {"extra_label_0": "Mods", "extra_role_0": str(MOD_ROLE)}
    if field == "dropdown":
        return {
            "c_label_0": "Team",
            "c_opt_label_0_0": "Mods",
            "c_opt_role_0_0": str(MOD_ROLE),
            "c_opt_label_0_1": "No team",
        }
    return {field: str(MOD_ROLE)}


@pytest.mark.parametrize("field", ["accept_role_ids", "pending_role_id", "extra", "dropdown"])
async def test_manage_server_alone_cannot_make_the_bot_give_out_a_powerful_role(db, alice, mod_role, field):
    """Otherwise someone who can't assign roles in Discord could get one through JoltBot."""
    csrf = await csrf_of(alice)
    version = (await tickets_state(db, GUILD_A)).version
    r = await alice.post(
        f"/servers/{GUILD_A}/tickets", data=ticket_form(GUILD_A, csrf, version, extra=_granting(field))
    )
    page = html.unescape(r.text)
    assert r.status_code == 400
    assert f"Moderator {POWERFUL}" in page and "Only the server owner or an Administrator" in page
    assert (await tickets_state(db, GUILD_A)).version == version  # nothing saved


@pytest.mark.parametrize("field", ["accept_role_ids", "pending_role_id", "extra", "dropdown"])
async def test_an_administrator_can_and_the_page_keeps_warning_about_it(db, dave, mod_role, field):
    csrf = await csrf_of(dave)
    version = (await tickets_state(db, GUILD_A)).version
    r = await dave.post(f"/servers/{GUILD_A}/tickets", data=ticket_form(GUILD_A, csrf, version, extra=_granting(field)))
    assert r.status_code == 303
    page = html.unescape((await dave.get(f"/servers/{GUILD_A}/tickets")).text)
    assert f"Moderator, which {POWERFUL}" in page


async def test_a_powerful_role_set_by_an_admin_does_not_lock_out_other_managers(db, alice, dave, mod_role):
    csrf = await csrf_of(dave)
    r = await dave.post(
        f"/servers/{GUILD_A}/tickets", data=ticket_form(GUILD_A, csrf, 1, extra=_granting("accept_role_ids"))
    )
    assert r.status_code == 303

    # Alice changes something else and leaves the roles as the admin set them.
    csrf = await csrf_of(alice)
    form = ticket_form(GUILD_A, csrf, 2, extra=_granting("accept_role_ids"), delete_delay_seconds="30")
    assert (await alice.post(f"/servers/{GUILD_A}/tickets", data=form)).status_code == 303
    assert (await tickets_state(db, GUILD_A)).config.delete_delay_seconds == 30


async def test_a_powerful_role_cannot_be_moved_to_a_wider_use_by_a_non_admin(db, alice, dave, mod_role):
    """Staff may hand it out as an extra role; that doesn't let a non-admin make everyone get it."""
    csrf = await csrf_of(dave)
    await dave.post(f"/servers/{GUILD_A}/tickets", data=ticket_form(GUILD_A, csrf, 1, extra=_granting("extra")))

    csrf = await csrf_of(alice)
    form = ticket_form(GUILD_A, csrf, 2, extra={**_granting("extra"), "pending_role_id": str(MOD_ROLE)})
    r = await alice.post(f"/servers/{GUILD_A}/tickets", data=form)
    assert r.status_code == 400 and f"Pending role: Moderator {POWERFUL}" in html.unescape(r.text)


async def test_ordinary_roles_need_no_special_rights(db, alice, mod_role):
    csrf = await csrf_of(alice)
    r = await alice.post(f"/servers/{GUILD_A}/tickets", data=ticket_form(GUILD_A, csrf, 1))
    assert r.status_code == 303
    assert "powerful permissions" not in (await alice.get(f"/servers/{GUILD_A}/tickets")).text


def test_who_counts_as_an_administrator():
    owner, admin, manager, member = parse_user_guilds(
        [
            guild_entry(1, 0, owner=True),
            guild_entry(2, ADMINISTRATOR),
            guild_entry(3, MANAGE_GUILD),
            guild_entry(4, 0),
        ]
    )
    assert (owner.is_admin, admin.is_admin, manager.is_admin, member.is_admin) == (True, True, False, False)
    assert (owner.can_manage, admin.can_manage, manager.can_manage, member.can_manage) == (True, True, True, False)


# --------------------------------------------------------------------------- Discord outage and logout


async def _discord_stops_answering(fake):
    async def down(token):
        raise DiscordError("Discord is down", 503)

    fake.get_user_guilds = down


async def _age_guild_list(db, minutes):
    async with db.session() as session, session.begin():
        await session.execute(update(WebSession).values(guilds_fetched_at=utcnow() - timedelta(minutes=minutes)))


async def test_a_short_discord_outage_does_not_lock_admins_out(db, alice, fake):
    await _discord_stops_answering(fake)
    await _age_guild_list(db, 10)  # older than the 5-minute refresh, so Discord is asked and fails
    assert (await alice.get(f"/servers/{GUILD_A}")).status_code == 200


async def test_an_old_server_list_is_not_trusted_forever(db, alice, fake):
    """Permissions can't be re-checked during an outage, so access stops after 30 minutes."""
    await _discord_stops_answering(fake)
    await _age_guild_list(db, 31)
    r = await alice.get(f"/servers/{GUILD_A}")
    assert r.status_code == 503 and "Discord isn't responding" in html.unescape(r.text)


async def test_logout_clears_the_https_cookie(db, two_guilds, fake):
    """A __Host- cookie is only accepted (and so only removed) when Set-Cookie says Secure."""
    base = "https://joltbot.example.com"
    app = create_app(make_config(public_base_url=base), db, fake, check_schema=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base) as client:
        await login(client, fake, ALICE, [guild_entry(GUILD_A, MANAGE_GUILD)])
        r = await client.post("/logout", data={"csrf_token": await csrf_of(client)})
    assert r.status_code == 303
    cleared = [h for h in r.headers.get_list("set-cookie") if h.startswith("__Host-joltbot_session=")][0].lower()
    assert "max-age=0" in cleared and "secure" in cleared and "path=/" in cleared and "httponly" in cleared


# --------------------------------------------------------------------------- a bot-only module (README section 11)


@pytest.fixture
def hello_module(monkeypatch):
    """The README's example module: a cog and settings, but no dashboard page of its own."""
    info = ModuleInfo(
        name="hello",
        title="Hello",
        description="A friendly /hello command.",
        settings_model=HelloSettings,
        cog="tests.example_module.cog",
    )
    monkeypatch.setitem(MODULES, "hello", info)


async def test_a_module_without_a_page_shows_on_the_overview_but_links_nowhere(alice, hello_module):
    page = (await alice.get(f"/servers/{GUILD_A}")).text
    assert "A friendly /hello command." in page
    assert f'href="/servers/{GUILD_A}/hello"' not in page  # no dead link in the sidebar or on the card
    assert f'href="/servers/{GUILD_A}/tickets"' in page  # modules with a page are still linked
    assert (await alice.get(f"/servers/{GUILD_A}/hello")).status_code == 404


async def test_a_module_without_a_page_can_be_switched_on_under_general(db, alice, hello_module):
    page = (await alice.get(f"/servers/{GUILD_A}/general")).text
    assert 'name="enabled_hello"' in page and 'name="version_hello" value="0"' in page

    csrf = await csrf_of(alice)
    form = {
        "csrf_token": csrf,
        "embed_color": "#123456",
        "enabled_hello": "on",
        "version_general": "1",
        "version_tickets": "1",
        "version_hello": "0",
    }
    assert (await alice.post(f"/servers/{GUILD_A}/general", data=form)).status_code == 303
    async with db.session() as session:
        assert (await load_module(session, GUILD_A, "hello")).enabled is True


# --------------------------------------------------------------------------- dropdown questions


def _troop_question(role_id):
    """Form fields for one dropdown question whose first option gives a role."""
    return {
        "c_label_0": "Troop type",
        "c_required_0": "on",
        "c_opt_label_0_0": "Infantry",
        "c_opt_role_0_0": str(role_id),
        "c_opt_label_0_1": "Not sure yet",
    }


async def test_dropdown_questions_are_saved_and_shown_again(db, alice):
    csrf = await csrf_of(alice)
    form = ticket_form(GUILD_A, csrf, 1, extra=_troop_question(ROLE[GUILD_A]))
    assert (await alice.post(f"/servers/{GUILD_A}/tickets", data=form)).status_code == 303

    (question,) = (await tickets_state(db, GUILD_A)).config.choice_questions
    assert question.label == "Troop type" and question.required is True
    assert [(o.label, o.role_id) for o in question.options] == [("Infantry", ROLE[GUILD_A]), ("Not sure yet", None)]

    page = (await alice.get(f"/servers/{GUILD_A}/tickets")).text
    assert 'name="c_label_0" value="Troop type"' in page
    assert 'name="c_opt_label_0_1" value="Not sure yet"' in page
    assert 'name="c_label_1" value=""' in page  # an empty block to add the next question


async def test_a_dropdown_role_from_another_server_is_refused(db, alice):
    csrf = await csrf_of(alice)
    form = ticket_form(GUILD_A, csrf, 1, extra=_troop_question(ROLE[GUILD_B]))
    r = await alice.post(f"/servers/{GUILD_A}/tickets", data=form)
    assert r.status_code == 400 and "Dropdown questions: a selected role isn't in this server" in html.unescape(r.text)
    assert (await tickets_state(db, GUILD_A)).config.choice_questions == []


async def test_a_dropdown_question_needs_two_options_and_the_form_keeps_what_was_typed(db, alice):
    csrf = await csrf_of(alice)
    fields = {"c_label_0": "Troop type", "c_opt_label_0_0": "Infantry"}
    r = await alice.post(f"/servers/{GUILD_A}/tickets", data=ticket_form(GUILD_A, csrf, 1, extra=fields))
    page = html.unescape(r.text)
    assert r.status_code == 400 and "Dropdown question 1: needs at least 2 options" in page
    assert 'name="c_opt_label_0_0" value="Infantry"' in r.text


async def test_clearing_a_dropdown_questions_name_removes_it(db, alice):
    csrf = await csrf_of(alice)
    form = ticket_form(GUILD_A, csrf, 1, extra=_troop_question(ROLE[GUILD_A]))
    assert (await alice.post(f"/servers/{GUILD_A}/tickets", data=form)).status_code == 303

    form = ticket_form(GUILD_A, csrf, 2, extra={**_troop_question(ROLE[GUILD_A]), "c_label_0": ""})
    assert (await alice.post(f"/servers/{GUILD_A}/tickets", data=form)).status_code == 303
    assert (await tickets_state(db, GUILD_A)).config.choice_questions == []


# --------------------------------------------------------------------------- icons on dropdown options


@pytest.fixture
def infantry_emoji(fake):
    """Server A has one emoji of its own, called infantry."""
    fake.data[GUILD_A] = replace(fake.data[GUILD_A], emojis=[Emoji(9001, "infantry", False)])


async def test_an_option_icon_is_saved_and_previewed(db, alice, infantry_emoji):
    csrf = await csrf_of(alice)
    fields = {**_troop_question(ROLE[GUILD_A]), "c_opt_emoji_0_0": "infantry", "c_opt_emoji_0_1": "❓"}
    r = await alice.post(f"/servers/{GUILD_A}/tickets", data=ticket_form(GUILD_A, csrf, 1, extra=fields))
    assert r.status_code == 303

    options = (await tickets_state(db, GUILD_A)).config.choice_questions[0].options
    assert [o.emoji for o in options] == ["infantry", "❓"]
    page = (await alice.get(f"/servers/{GUILD_A}/tickets")).text
    assert 'name="c_opt_emoji_0_0" value="infantry"' in page
    assert "https://cdn.discordapp.com/emojis/9001.png" in page  # the admin sees which icon they picked


async def test_an_icon_name_the_server_does_not_have_is_refused(db, alice, infantry_emoji):
    csrf = await csrf_of(alice)
    fields = {**_troop_question(ROLE[GUILD_A]), "c_opt_emoji_0_0": ":infantery:"}
    r = await alice.post(f"/servers/{GUILD_A}/tickets", data=ticket_form(GUILD_A, csrf, 1, extra=fields))
    page = html.unescape(r.text)
    assert r.status_code == 400
    assert "Dropdown question 1, option 1: this server has no emoji called :infantery:" in page
    assert (await tickets_state(db, GUILD_A)).config.choice_questions == []


async def test_plain_text_is_not_an_icon(db, alice):
    csrf = await csrf_of(alice)
    fields = {**_troop_question(ROLE[GUILD_A]), "c_opt_emoji_0_0": "my icon"}
    r = await alice.post(f"/servers/{GUILD_A}/tickets", data=ticket_form(GUILD_A, csrf, 1, extra=fields))
    assert r.status_code == 400 and "Dropdown question 1, option 1" in html.unescape(r.text)


# --------------------------------------------------------------------------- dashboard access roles

HELPERS = 5160  # a role in server A with no Discord permissions at all
MAYA = {"id": "1005", "username": "maya"}  # member of A who holds Helpers
ERIN = {"id": "1006", "username": "erin"}  # member of A with no roles
MEMBER_ONLY = 1 << 10  # View Channels: an ordinary member's permissions


@pytest.fixture
def helpers_role(fake):
    """Server A has a Helpers role, and Maya holds it."""
    data = fake.data[GUILD_A]
    fake.data[GUILD_A] = replace(data, roles=[*data.roles, Role(HELPERS, "Helpers", 4, 0, False, 0)])
    fake.members[(GUILD_A, int(MAYA["id"]))] = {HELPERS}


async def set_access_roles(db, role_ids, guild_id=GUILD_A):
    """Store a server's dashboard access roles, as the Dashboard access page would."""
    async with db.session() as session, session.begin():
        state = await load_module(session, guild_id, "general")
        await save_module(
            session,
            guild_id,
            "general",
            enabled=True,
            config=state.config.model_copy(update={"dashboard_role_ids": list(role_ids)}),
            expected_version=state.version,
            actor=SYSTEM_ACTOR,
        )


async def general_state(db, guild_id=GUILD_A):
    async with db.session() as session:
        return await load_module(session, guild_id, "general")


@pytest.fixture
async def maya(app, fake, db, helpers_role):
    """Logged in, with Helpers already given dashboard access to server A."""
    await set_access_roles(db, [HELPERS])
    async with client_for(app) as client:
        yield await login(client, fake, MAYA, [guild_entry(GUILD_A, MEMBER_ONLY), guild_entry(GUILD_B, MEMBER_ONLY)])


@pytest.mark.parametrize("path", ["", "/tickets", "/general", "/audit"])
async def test_a_member_with_an_access_role_can_open_the_dashboard(maya, path):
    assert (await maya.get(f"/servers/{GUILD_A}{path}")).status_code == 200
    assert (await maya.get(f"/servers/{GUILD_B}{path}")).status_code == 403  # in B too, but B gave nobody access


async def test_the_server_list_shows_servers_reached_through_a_role(maya):
    page = (await maya.get("/servers")).text
    assert f'href="/servers/{GUILD_A}"' in page
    assert f"/servers/{GUILD_B}" not in page and "Invite JoltBot" not in page


async def test_a_role_holder_can_save_settings_and_the_audit_log_names_them(db, maya):
    csrf = await csrf_of(maya)
    version = (await tickets_state(db, GUILD_A)).version
    r = await maya.post(f"/servers/{GUILD_A}/tickets", data=ticket_form(GUILD_A, csrf, version))
    assert r.status_code == 303 and (await tickets_state(db, GUILD_A)).enabled is True
    async with db.session() as session:
        entry = (await session.execute(select(AuditLog).where(AuditLog.module == "tickets"))).scalar_one()
    assert (entry.actor_id, entry.actor_name, entry.source) == (int(MAYA["id"]), "maya", "dashboard")


async def test_a_member_without_the_role_is_refused(app, fake, db, helpers_role):
    await set_access_roles(db, [HELPERS])
    async with client_for(app) as client:
        await login(client, fake, ERIN, [guild_entry(GUILD_A, MEMBER_ONLY)])
        for path in ("", "/tickets", "/general", "/audit", "/access"):
            assert (await client.get(f"/servers/{GUILD_A}{path}")).status_code == 403, path
        assert f"/servers/{GUILD_A}" not in (await client.get("/servers")).text


async def test_losing_the_role_in_discord_removes_access(maya, fake):
    assert (await maya.get(f"/servers/{GUILD_A}")).status_code == 200
    fake.members[(GUILD_A, int(MAYA["id"]))] = set()  # the role was taken away in Discord
    assert (await maya.get(f"/servers/{GUILD_A}")).status_code == 403
    assert f"/servers/{GUILD_A}" not in (await maya.get("/servers")).text


async def test_removing_the_role_from_the_list_removes_access_at_once(db, maya):
    assert (await maya.get(f"/servers/{GUILD_A}/tickets")).status_code == 200
    await set_access_roles(db, [])
    assert (await maya.get(f"/servers/{GUILD_A}/tickets")).status_code == 403


async def test_a_role_holder_cannot_see_or_change_who_has_access(db, maya):
    assert f"/servers/{GUILD_A}/access" not in (await maya.get(f"/servers/{GUILD_A}")).text  # no link to it
    assert (await maya.get(f"/servers/{GUILD_A}/access")).status_code == 403
    before = await general_state(db)
    csrf = await csrf_of(maya)
    r = await maya.post(
        f"/servers/{GUILD_A}/access",
        data={"csrf_token": csrf, "version": str(before.version), "dashboard_role_ids": str(ROLE[GUILD_A])},
    )
    assert r.status_code == 403 and "Only members with Manage Server" in html.unescape(r.text)
    after = await general_state(db)
    assert (after.version, after.config.dashboard_role_ids) == (before.version, [HELPERS])


async def test_a_manager_picks_the_access_roles_and_it_is_audited(db, alice, helpers_role):
    page = await alice.get(f"/servers/{GUILD_A}/access")
    assert page.status_code == 200 and f'name="dashboard_role_ids" value="{HELPERS}"' in page.text
    assert f'href="/servers/{GUILD_A}/access"' in page.text

    csrf = await csrf_of(alice)
    form = {"csrf_token": csrf, "version": "1", "dashboard_role_ids": str(HELPERS)}
    assert (await alice.post(f"/servers/{GUILD_A}/access", data=form)).status_code == 303
    assert (await general_state(db)).config.dashboard_role_ids == [HELPERS]

    async with db.session() as session:
        entry = (await session.execute(select(AuditLog).where(AuditLog.guild_id == GUILD_A))).scalar_one()
    assert (entry.actor_id, entry.source, entry.module) == (int(ALICE["id"]), "dashboard", "general")
    assert entry.changes == {"dashboard_role_ids": [[], [str(HELPERS)]]}
    audit = html.unescape((await alice.get(f"/servers/{GUILD_A}/audit")).text)
    assert "dashboard_role_ids" in audit and "@Helpers" in audit and "alice" in audit

    # Unticking everything is audited too, and closes the door again.
    form = {"csrf_token": csrf, "version": "2"}
    assert (await alice.post(f"/servers/{GUILD_A}/access", data=form)).status_code == 303
    assert (await general_state(db)).config.dashboard_role_ids == []
    async with db.session() as session:
        assert len(list((await session.execute(select(AuditLog))).scalars())) == 2


@pytest.mark.parametrize("bad", [str(ROLE[GUILD_B]), "999999", "not-a-number"])
async def test_an_access_role_from_another_server_is_refused(db, alice, bad):
    csrf = await csrf_of(alice)
    r = await alice.post(
        f"/servers/{GUILD_A}/access", data={"csrf_token": csrf, "version": "1", "dashboard_role_ids": bad}
    )
    assert r.status_code == 400
    assert (await general_state(db)).config.dashboard_role_ids == []


async def test_an_old_access_form_does_not_overwrite_a_newer_save(db, alice, helpers_role):
    csrf = await csrf_of(alice)
    form = {"csrf_token": csrf, "version": "1", "dashboard_role_ids": str(HELPERS)}
    assert (await alice.post(f"/servers/{GUILD_A}/access", data=form)).status_code == 303
    r = await alice.post(f"/servers/{GUILD_A}/access", data={"csrf_token": csrf, "version": "1"})  # an old tab
    assert r.status_code == 400 and "Someone else saved" in r.text
    assert (await general_state(db)).config.dashboard_role_ids == [HELPERS]


async def test_saving_the_general_page_never_changes_the_access_roles(db, maya):
    csrf = await csrf_of(maya)
    form = _general_form(csrf, embed_color="#abcdef", version_general="2", dashboard_role_ids=str(ROLE[GUILD_A]))
    assert (await maya.post(f"/servers/{GUILD_A}/general", data=form)).status_code == 303
    config = (await general_state(db)).config
    assert (config.embed_color, config.dashboard_role_ids) == ("#abcdef", [HELPERS])


async def test_only_an_administrator_can_make_the_bot_give_out_an_access_role(db, maya, alice, dave):
    """Otherwise a role holder could have JoltBot hand dashboard access to every new member."""
    for client in (maya, alice):  # let in by the role; Manage Server alone
        csrf = await csrf_of(client)
        form = ticket_form(GUILD_A, csrf, 1, accept_role_ids=str(HELPERS))
        r = await client.post(f"/servers/{GUILD_A}/tickets", data=form)
        page = html.unescape(r.text)
        assert r.status_code == 400 and "Helpers gives access to this dashboard" in page
        assert "Only the server owner or an Administrator" in page
    assert (await tickets_state(db, GUILD_A)).version == 1  # nothing saved

    csrf = await csrf_of(dave)
    form = ticket_form(GUILD_A, csrf, 1, accept_role_ids=str(HELPERS))
    assert (await dave.post(f"/servers/{GUILD_A}/tickets", data=form)).status_code == 303


async def test_a_role_the_bot_gives_out_cannot_be_made_an_access_role(db, alice):
    csrf = await csrf_of(alice)
    assert (await alice.post(f"/servers/{GUILD_A}/tickets", data=ticket_form(GUILD_A, csrf, 1))).status_code == 303
    r = await alice.post(
        f"/servers/{GUILD_A}/access",
        data={"csrf_token": csrf, "version": "1", "dashboard_role_ids": str(ROLE[GUILD_A])},
    )
    page = html.unescape(r.text)
    assert r.status_code == 400 and "JoltBot gives out Member" in page and "Role given on accept" in page
    assert (await general_state(db)).config.dashboard_role_ids == []


async def test_a_role_holder_learns_nothing_about_servers_they_cannot_use(db, maya):
    assert (await maya.get(f"/servers/{GUILD_B}")).status_code == 403  # JoltBot is there
    assert (await maya.get("/servers/555555555555555555")).status_code == 403  # not a member at all
    async with db.session() as session, session.begin():  # JoltBot leaves server A
        await session.execute(update(Guild).where(Guild.id == GUILD_A).values(active=False))
    assert (await maya.get(f"/servers/{GUILD_A}")).status_code == 403  # the same answer, not "isn't in that server"


async def test_roles_are_never_guessed_while_discord_is_down(maya, fake):
    async def down(guild_id, user_id):
        raise DiscordError("Discord is down", 503)

    fake.member_role_ids = down
    r = await maya.get(f"/servers/{GUILD_A}")
    assert r.status_code == 503 and "roles couldn't be checked" in html.unescape(r.text)
    page = await maya.get("/servers")  # the list still loads, without the server that couldn't be checked
    assert page.status_code == 200 and f"/servers/{GUILD_A}" not in page.text


async def test_managers_are_not_asked_for_their_roles(alice, fake):
    """Manage Server is enough on its own, also while member lookups fail."""

    async def down(guild_id, user_id):
        raise DiscordError("Discord is down", 503)

    fake.member_role_ids = down
    assert (await alice.get(f"/servers/{GUILD_A}/tickets")).status_code == 200
