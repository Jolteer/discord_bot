"""Look at the website and dashboard without Discord: python -m scripts.preview_site

Starts the dashboard on http://localhost:8000 with a stand-in for Discord and a
throwaway database filled with example data. "Login with Discord" logs you in at
once as an example admin, so every page can be clicked through. No .env, bot
token or Discord application is needed, and nothing real is read or changed.

For checking how the site looks while working on templates and styles. It
listens on this computer only; never run it on a server.

    python -m scripts.preview_site                 # then open http://localhost:8000
    python -m scripts.preview_site --port 8010
    python -m scripts.preview_site --bot-down      # show the "JoltBot is not connected" notice
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import tempfile
from pathlib import Path
from typing import Any

import uvicorn

from joltbot.core.config import AppConfig
from joltbot.core.db import Database
from joltbot.core.discord_rest import (
    CATEGORY,
    DEFAULT_TOKEN_LIFETIME_SECONDS,
    TEXT_CHANNEL,
    Channel,
    DiscordError,
    GuildData,
    Role,
)
from joltbot.core.guild_settings import Actor, load_module, save_module
from joltbot.core.guilds import GuildInfo, ensure_guild
from joltbot.core.health import write_heartbeat
from joltbot.core.issues import record_issue
from joltbot.core.permissions import ADMINISTRATOR, BOT_PERMISSIONS_VALUE, ELEVATED_PERMISSIONS
from joltbot.modules.general.settings import GeneralSettings
from joltbot.modules.tickets.settings import ChoiceOption, ChoiceQuestion, ExtraRoleChoice, TicketSettings
from joltbot.web.app import create_app

HARBOUR, FRESH, NOT_JOINED = 111111111111111111, 222222222222222222, 333333333333333333
USER = {"id": "1001", "username": "morgan", "avatar": None}
GUILDS = [
    {"id": str(HARBOUR), "name": "Harbour Lights", "icon": None, "owner": True, "permissions": str(ADMINISTRATOR)},
    {"id": str(FRESH), "name": "Fresh Server", "icon": None, "owner": False, "permissions": str(ADMINISTRATOR)},
    {"id": str(NOT_JOINED), "name": "Weekend Raiders", "icon": None, "owner": True, "permissions": str(ADMINISTRATOR)},
]

# Example roles and channels. The IDs are made up.
BOT_ROLE, MOD, HELPER, MEMBER, UNVERIFIED, NORTH, SOUTH, BOOSTER = 5001, 5002, 5003, 5004, 5005, 5006, 5007, 5008
PANEL, LOG, STAFF_CHAT, WELCOME, TICKETS = 6001, 6002, 6003, 6004, 7001


def example_guild(guild_id: int) -> GuildData:
    """The example server's roles and channels. The same for every guild ID."""
    return GuildData(
        guild_id=guild_id,
        roles=[
            Role(BOT_ROLE, "JoltBot", 20, BOT_PERMISSIONS_VALUE, True, 0xFFB800),
            Role(MOD, "Moderator", 15, ELEVATED_PERMISSIONS["Kick Members"], False, 0xE74C3C),
            Role(HELPER, "Helper", 12, 0, False, 0x3498DB),
            Role(BOOSTER, "Server Booster", 11, 0, True, 0xF47FFF),
            Role(NORTH, "North Fleet", 8, 0, False, 0x1ABC9C),
            Role(SOUTH, "South Fleet", 7, 0, False, 0x9B59B6),
            Role(MEMBER, "Member", 5, 0, False, 0x2ECC71),
            Role(UNVERIFIED, "Unverified", 2, 0, False, 0),
        ],
        channels=[
            Channel(TICKETS, "Tickets", CATEGORY, 0, None),
            Channel(WELCOME, "welcome", TEXT_CHANNEL, 0, None),
            Channel(PANEL, "apply-here", TEXT_CHANNEL, 1, None),
            Channel(STAFF_CHAT, "staff-chat", TEXT_CHANNEL, 2, TICKETS),
            Channel(LOG, "ticket-log", TEXT_CHANNEL, 3, TICKETS),
        ],
        everyone_permissions=0,
        bot_role_ids=[BOT_ROLE],
    )


class PreviewDiscord:
    """Answers the dashboard's Discord questions from the example data above."""

    def authorize_url(self, state: str) -> str:
        """Skip Discord: the login link leads straight back to the callback."""
        return f"/auth/callback?code=preview&state={state}"

    async def exchange_code(self, code: str) -> dict[str, Any]:
        """Accept the stand-in login code."""
        if code != "preview":
            raise DiscordError("bad code", 400)
        return {
            "access_token": "preview-token",
            "expires_in": DEFAULT_TOKEN_LIFETIME_SECONDS,
            "scope": "identify guilds",
        }

    async def get_user(self, access_token: str) -> dict[str, Any]:
        """The example admin."""
        return USER

    async def get_user_guilds(self, access_token: str) -> list[dict[str, Any]]:
        """The three example servers."""
        return GUILDS

    async def guild_data(self, guild_id: int, *, refresh: bool = False) -> GuildData:
        """The example roles and channels."""
        return example_guild(guild_id)

    def forget_guild(self, guild_id: int) -> None:
        """Nothing is cached, so there is nothing to forget."""
        pass

    async def member_role_ids(self, guild_id: int, user_id: int) -> frozenset[int]:
        """No roles: the example user manages every server, so access roles never come into it."""
        return frozenset()

    async def aclose(self) -> None:
        """Nothing to close."""
        pass


async def seed(db: Database, *, bot_down: bool) -> None:
    """Fill the throwaway database: one server that is set up, one that is brand new."""
    morgan, riley = Actor(1001, "morgan", "dashboard"), Actor(1002, "riley", "dashboard")
    async with db.session() as session, session.begin():
        await ensure_guild(session, GuildInfo(HARBOUR, "Harbour Lights"))
        await ensure_guild(session, GuildInfo(FRESH, "Fresh Server"))
        if not bot_down:
            await write_heartbeat(session)

        # Saved in a few steps, so the audit log has something to show.
        tickets = TicketSettings(
            staff_role_ids=[MOD],
            panel_channel_id=PANEL,
            ticket_category_id=TICKETS,
            log_channel_id=LOG,
            accept_role_ids=[MEMBER],
        )
        version = (await load_module(session, HARBOUR, "tickets")).version
        state = await save_module(
            session, HARBOUR, "tickets", enabled=True, config=tickets, expected_version=version, actor=morgan
        )
        tickets = tickets.model_copy(
            update={
                "staff_role_ids": [MOD, HELPER],
                "pending_role_id": UNVERIFIED,
                "welcome_channel_id": WELCOME,
                "extra_role_choices": [ExtraRoleChoice(label="North Fleet", role_id=NORTH, emoji="⚓")],
                "choice_questions": [
                    ChoiceQuestion(
                        label="Which fleet?",
                        options=[
                            ChoiceOption(label="North Fleet", role_id=NORTH, emoji="⚓"),
                            ChoiceOption(label="South Fleet", role_id=SOUTH, emoji="🌊"),
                            ChoiceOption(label="Not sure yet", emoji="❓"),
                        ],
                    )
                ],
            }
        )
        state = await save_module(
            session, HARBOUR, "tickets", enabled=True, config=tickets, expected_version=state.version, actor=riley
        )
        tickets = tickets.model_copy(update={"delete_delay_seconds": 30, "require_attachment": True})
        await save_module(
            session, HARBOUR, "tickets", enabled=True, config=tickets, expected_version=state.version, actor=morgan
        )

        general = await load_module(session, HARBOUR, "general")
        await save_module(
            session,
            HARBOUR,
            "general",
            enabled=True,
            config=GeneralSettings(embed_color="#FFB800", notice_channel_id=STAFF_CHAT, dashboard_role_ids=[HELPER]),
            expected_version=general.version,
            actor=morgan,
        )
        await record_issue(
            session,
            HARBOUR,
            "tickets.log_channel",
            "JoltBot couldn't post a transcript in #ticket-log: it is missing the Attach Files permission there.",
        )


async def keep_bot_connected(db: Database) -> None:
    """Write a heartbeat every 30 seconds, so the dashboard shows the bot as connected."""
    while True:
        await asyncio.sleep(30)
        async with db.session() as session, session.begin():
            await write_heartbeat(session)


async def run(port: int, bot_down: bool) -> None:
    """Create the throwaway database, fill it, and serve the dashboard until Ctrl+C.

    Args:
        port: Port to listen on, on this computer only.
        bot_down: True leaves the heartbeat out, to preview the "bot is offline" notice.
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        database_url = f"sqlite+aiosqlite:///{(Path(folder) / 'preview.db').as_posix()}"
        db = Database(database_url)
        await db.create_all()
        await seed(db, bot_down=bot_down)
        config = AppConfig(
            discord_token="preview",
            discord_client_id="0",
            discord_client_secret="preview",
            session_secret=secrets.token_urlsafe(32),
            public_base_url=f"http://localhost:{port}",
            database_url=database_url,
        )
        app = create_app(config, db, PreviewDiscord(), check_schema=False)  # type: ignore[arg-type]
        # Stands in for the bot's own heartbeat, so the dashboard keeps showing it as connected.
        heartbeat = None if bot_down else asyncio.create_task(keep_bot_connected(db))
        print(f"\n  JoltBot preview with example data: http://localhost:{port}\n  Ctrl+C stops it.\n")
        try:
            await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")).serve()
        finally:
            if heartbeat:
                heartbeat.cancel()


def main() -> None:
    """CLI entry: parse the arguments and start the preview."""
    parser = argparse.ArgumentParser(description="Preview the JoltBot website with example data (no Discord needed)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--bot-down", action="store_true", help="show the dashboard as it looks when the bot is down")
    args = parser.parse_args()
    asyncio.run(run(args.port, args.bot_down))


if __name__ == "__main__":
    main()
