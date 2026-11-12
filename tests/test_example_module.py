"""Adding a module the way the README describes: three small pieces and nothing else.

The module itself is tests/example_module (the README's example, verbatim). These
tests are what keeps that guide honest: if adding a module ever needs more wiring
than a folder plus one registry entry, they fail.

The dashboard side (overview card, on/off switch) is tested in test_web_access.py.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from joltbot.bot.client import JoltBot
from joltbot.core.config import AppConfig
from joltbot.core.guild_settings import load_module, save_module
from joltbot.core.guilds import GuildInfo, ensure_guild
from joltbot.modules.registry import MODULES, ModuleInfo

from .conftest import GUILD_A
from .example_module.cog import Hello
from .example_module.settings import HelloSettings
from .fakes import TEST_ACTOR, FakeGuild, fake_interaction, fake_member

# The one registry entry a new module needs (joltbot/modules/registry.py).
HELLO = ModuleInfo(
    name="hello",
    title="Hello",
    description="A friendly /hello command.",
    settings_model=HelloSettings,
    cog="tests.example_module.cog",
)


@pytest.fixture
def hello_module(monkeypatch):
    monkeypatch.setitem(MODULES, "hello", HELLO)


async def test_the_bot_loads_the_module_and_registers_its_command(db, hello_module):
    bot = JoltBot(AppConfig(_env_file=None, database_url=str(db.engine.url)))
    async with bot:
        await bot.setup_hook()
        assert bot.failed_modules == []
        assert bot.get_cog("Hello") is not None
        assert bot.tree.get_command("hello") is not None
        assert bot.get_cog("Tickets") is not None  # and nothing else was disturbed


async def test_servers_start_with_the_module_switched_off(db, two_guilds, hello_module):
    # A server that was already there before the module existed: no row yet, defaults, off.
    async with db.session() as session:
        state = await load_module(session, GUILD_A, "hello")
    assert (state.enabled, state.version) == (False, 0)
    assert state.config == HelloSettings()

    # The start-up sync (and joining a new server) gives every server a row for it.
    async with db.session() as session, session.begin():
        await ensure_guild(session, GuildInfo(GUILD_A, "Server A"))
    async with db.session() as session:
        state = await load_module(session, GUILD_A, "hello")
    assert (state.enabled, state.version) == (False, 1)


async def test_the_command_obeys_the_servers_switch_and_settings(db, two_guilds, hello_module):
    guild = FakeGuild(GUILD_A, name="Server A")
    member = fake_member(guild, 7, name="alice")
    cog = Hello(SimpleNamespace(db=db))

    off = fake_interaction(guild, member)
    await cog.hello.callback(cog, off)
    off.response.send_message.assert_awaited_once_with("That feature is switched off on this server.", ephemeral=True)

    async with db.session() as session, session.begin():
        current = await load_module(session, GUILD_A, "hello")
        await save_module(
            session,
            GUILD_A,
            "hello",
            enabled=True,
            config=HelloSettings(greeting="Hi {user}! This is {server}."),
            expected_version=current.version,
            actor=TEST_ACTOR,
        )
    on = fake_interaction(guild, member)
    await cog.hello.callback(cog, on)
    on.response.send_message.assert_awaited_once_with("Hi <@7>! This is Server A.", ephemeral=True)


@pytest.mark.parametrize("filename", ["settings.py", "cog.py"])
def test_the_readme_shows_exactly_this_code(filename):
    root = Path(__file__).resolve().parents[1]
    source = (root / "tests" / "example_module" / filename).read_text(encoding="utf-8").replace("\r\n", "\n")
    code = source.split('"""\n', 1)[1].strip()  # everything after the file's one-line docstring
    readme = (root / "README.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert code in readme, f"README section 11 and tests/example_module/{filename} have drifted apart"
