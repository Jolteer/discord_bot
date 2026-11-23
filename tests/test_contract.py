"""What JoltBot has already promised Discord and its servers. These must not change by accident.

Two things outlive any one version of the code:

- Slash commands. Their names, descriptions and parameters are registered with
  Discord, and moderators have learned them.
- Button and menu IDs. Discord sends a component's ``custom_id`` back on every
  click, and that is the only way a click on a message posted months ago finds
  its handler. Rename one and every panel and ticket already posted stops working.

If a test here fails, the change is one the server's members will notice.
Change the expected values below only when that is the intention. The full
list of what is frozen is in docs/refactor/AUDIT.md, section 3.
"""

import pytest

from joltbot.bot.client import JoltBot
from joltbot.core.config import AppConfig

# Discord's own numbers, as they appear in the registration payload.
SUBCOMMAND, USER_OPTION = 1, 6
MANAGE_SERVER = 1 << 5
GUILD_ONLY = [0]  # the "guild" interaction context: not usable in DMs

# The command payload JoltBot sends to Discord, reduced to what a member can see.
COMMANDS = {
    "tickets": {
        "description": "Ticket and verification tools",
        "default_member_permissions": MANAGE_SERVER,
        "dm_permission": False,
        "contexts": GUILD_ONLY,
        "options": [
            {
                "type": SUBCOMMAND,
                "name": "panel",
                "description": "Post (or re-post) the application panel in the panel channel.",
                "options": [],
            },
            {
                "type": SUBCOMMAND,
                "name": "diagnostics",
                "description": "Check JoltBot's permissions, role order and ticket setup.",
                "options": [],
            },
            {
                "type": SUBCOMMAND,
                "name": "grant-existing",
                "description": "One time: give the accepted-member role(s) to everyone already here.",
                "options": [],
            },
        ],
    },
    "alliances": {
        "description": "Alliance join request tools",
        "default_member_permissions": MANAGE_SERVER,
        "dm_permission": False,
        "contexts": GUILD_ONLY,
        "options": [
            {
                "type": SUBCOMMAND,
                "name": "panel",
                "description": "Post (or re-post) the alliance menu in the panel channel.",
                "options": [],
            },
            {
                "type": SUBCOMMAND,
                "name": "pending",
                "description": "List the alliance join requests still waiting for a decision.",
                "options": [],
            },
            {
                "type": SUBCOMMAND,
                "name": "cancel",
                "description": "Cancel a member's pending alliance join request.",
                "options": [
                    {
                        "type": USER_OPTION,
                        "name": "member",
                        "description": "Whose pending request to cancel",
                        "required": True,
                    }
                ],
            },
        ],
    },
}

# Which permission check guards each command. Discord's "default permissions" only hide a
# command, and a server's admins can change that, so these checks are the real gate.
COMMAND_CHECKS = {
    ("tickets", "panel"): ["_setup_check"],
    ("tickets", "diagnostics"): ["_setup_check"],
    ("tickets", "grant-existing"): ["_manage_server_check"],
    ("alliances", "panel"): ["_manage_check"],
    ("alliances", "pending"): ["_manage_check"],
    ("alliances", "cancel"): ["_manage_check"],
}

# custom_id -> (view class, handler method). Written out as literals on purpose: importing
# the constants from the code would let a rename pass this test.
COMPONENTS = {
    "joltbot:tickets:open": ("PanelView", "open_application"),
    "joltbot:tickets:accept": ("TicketControlView", "accept"),
    "joltbot:tickets:deny": ("TicketControlView", "deny"),
    "joltbot:tickets:close": ("TicketControlView", "close"),
    # The old KOC Gatekeeper bot's IDs, still on panels and tickets it posted before the migration.
    "rokverify:open_application": ("PanelView", "open_application"),
    "rokticket:accept": ("TicketControlView", "accept"),
    "rokticket:deny": ("TicketControlView", "deny"),
    "rokticket:close": ("TicketControlView", "close"),
    "joltbot:alliances:choose": ("AlliancePanelView", "choose"),
    "joltbot:alliances:accept": ("RequestDecisionView", "accept"),
    "joltbot:alliances:reject": ("RequestDecisionView", "reject"),
}


@pytest.fixture
async def bot(db):
    """The real bot with every module loaded, as at startup. It never connects to Discord."""
    bot = JoltBot(AppConfig(_env_file=None, database_url=str(db.engine.url)))
    async with bot:
        await bot.setup_hook()
        assert bot.failed_modules == []
        yield bot


@pytest.mark.parametrize("group_name", sorted(COMMANDS))
async def test_the_slash_commands_registered_with_discord_are_unchanged(bot, group_name):
    payload = {command.name: command.to_dict(bot.tree) for command in bot.tree.get_commands()}
    expected = COMMANDS[group_name]
    assert {key: payload[group_name][key] for key in expected} == expected


async def test_every_command_is_still_guarded_by_its_permission_check(bot):
    checks = {
        (group.name, command.name): [check.__name__ for check in command.checks]
        for group in bot.tree.get_commands()
        for command in group.commands
    }
    assert {key: checks.get(key) for key in COMMAND_CHECKS} == COMMAND_CHECKS


async def test_every_button_and_menu_already_posted_still_finds_its_handler(bot):
    registered = {
        item.custom_id: (type(view).__name__, item.callback.__name__)
        for view in bot.persistent_views
        for item in view.children
    }
    assert {custom_id: registered.get(custom_id) for custom_id in COMPONENTS} == COMPONENTS


async def test_the_registered_views_never_expire(bot):
    # A view with a timeout stops answering after it, and is not restored after a restart.
    assert [type(view).__name__ for view in bot.persistent_views if view.timeout is not None] == []
