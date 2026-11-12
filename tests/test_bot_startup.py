"""Bot start-up and shutdown: a broken module must not take the bot down, and SIGTERM stops it cleanly.

Uses a real JoltBot object that never connects to Discord.
"""

import asyncio
import os
import signal
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from joltbot.bot import client as client_module
from joltbot.bot.__main__ import close_on_sigterm
from joltbot.bot.client import JoltBot
from joltbot.core.config import AppConfig
from joltbot.modules.general.settings import GeneralSettings
from joltbot.modules.registry import MODULES, ModuleInfo

BROKEN = ModuleInfo(
    name="broken",
    title="Broken",
    description="A module whose bot code can't be loaded.",
    settings_model=GeneralSettings,
    cog="tests.no_such_cog",
)


@pytest.fixture
async def bot(db):
    bot = JoltBot(AppConfig(_env_file=None, database_url=str(db.engine.url)))
    async with bot:  # prepares the bot's event loop state without logging in, and closes it afterwards
        yield bot


async def test_all_modules_load_and_command_sync_is_scheduled(bot):
    await bot.setup_hook()
    assert bot.failed_modules == []
    assert bot.get_cog("Tickets") is not None and bot.get_cog("GuildSync") is not None
    assert bot.get_cog("Heartbeat") is not None
    assert len(bot._background) == 1  # the slash-command sync, waiting for the bot to be ready


async def test_a_broken_module_is_skipped_and_the_rest_still_load(bot, monkeypatch, caplog):
    monkeypatch.setattr(client_module, "MODULES", {"broken": BROKEN, **MODULES})
    await bot.setup_hook()

    assert bot.failed_modules == ["broken"]
    assert bot.get_cog("Tickets") is not None  # tickets keep working
    assert "Module 'broken' failed to load" in caplog.text
    # Syncing now would tell Discord to delete the broken module's commands in every server.
    assert not bot._background
    assert "Not registering slash commands" in caplog.text


async def test_closing_the_bot_stops_its_background_work(db):
    bot = JoltBot(AppConfig(_env_file=None, database_url=str(db.engine.url)))
    async with bot:
        await bot.setup_hook()
        (sync_task,) = bot._background
    await asyncio.sleep(0)
    assert sync_task.cancelled()
    assert bot.is_closed()


async def test_sigterm_handler_never_breaks_startup():
    # Windows event loops can't install signal handlers; that must be ignored, not raised.
    fake_bot = MagicMock(close=AsyncMock())
    close_on_sigterm(fake_bot)
    if sys.platform != "win32":
        asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows event loops have no signal handlers")
async def test_sigterm_closes_the_bot():
    fake_bot = MagicMock(close=AsyncMock())
    close_on_sigterm(fake_bot)
    try:
        os.kill(os.getpid(), signal.SIGTERM)  # what `docker stop` and systemd send
        await asyncio.sleep(0.05)
        fake_bot.close.assert_awaited_once()
    finally:
        asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM)
