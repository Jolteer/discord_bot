"""Dashboard security: login, CSRF, and above all that one server's admins can
never read or change another server's settings.

Discord is replaced by FakeDiscord, so these run offline. The login itself
goes through the real /login -> /auth/callback flow.
"""

import html
import re
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select, update

from joltbot.core.config import AppConfig
from joltbot.core.discord_rest import CATEGORY, TEXT_CHANNEL, Channel, DiscordError, GuildData, Role
from joltbot.core.guild_settings import load_module
from joltbot.core.issues import record_issue
from joltbot.core.models import AuditLog, GuildIssue, WebSession
from joltbot.core.permissions import ADMINISTRATOR, MANAGE_GUILD
from joltbot.core.types import utcnow
from joltbot.web.app import create_app

from .conftest import GUILD_A, GUILD_B

GUILD_NO_BOT = 444444444444444444
ALICE = {"id": "1001", "username": "alice"}  # admin of A only
BOB = {"id": "1002", "username": "bob"}  # admin of B only
CAROL = {"id": "1003", "username": "carol"}  # plain member of A

# Each server's real roles/channels: IDs are made up but distinct per server.
ROLE = {GUILD_A: 5100, GUILD_B: 5200}
TEXT = {GUILD_A: 6100, GUILD_B: 6200}
CAT = {GUILD_A: 7100, GUILD_B: 7200}
BOT_ROLE = {GUILD_A: 5199, GUILD_B: 5299}


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
