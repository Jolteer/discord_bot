"""The real bot loads the mod log, asks Discord for what it needs, and the model matches the migration."""

from __future__ import annotations

from joltbot.bot.client import MESSAGE_CACHE_SIZE, JoltBot
from joltbot.core.config import AppConfig
from joltbot.core.permissions import BOT_PERMISSIONS, VIEW_AUDIT_LOG
from joltbot.modules.modlog.constants import CATEGORIES, EVENTS, MODULE
from joltbot.modules.modlog.service import ModLogManager
from joltbot.modules.modlog.settings import ModLogSettings
from joltbot.modules.registry import MODULES


async def test_the_bot_loads_the_mod_log_with_its_intents_and_message_cache(db):
    bot = JoltBot(AppConfig(_env_file=None, database_url=str(db.engine.url)))
    async with bot:
        await bot.setup_hook()
        assert bot.failed_modules == []
        assert bot.get_cog("ModLog") is not None
        assert isinstance(bot.modlog, ModLogManager)
        listeners = {name for name, _ in bot.get_cog("ModLog").get_listeners()}
        assert {"on_audit_log_entry_create", "on_raw_message_delete", "on_member_update"} <= listeners

    intents = bot.intents
    assert intents.moderation and intents.voice_states and intents.guilds  # not privileged
    assert intents.members and intents.message_content  # privileged: switched on in the Developer Portal
    assert not intents.presences  # not needed, so not asked for
    assert bot._connection.max_messages == MESSAGE_CACHE_SIZE == 5000


def test_the_invite_link_asks_for_view_audit_log():
    assert BOT_PERMISSIONS["View Audit Log"] == VIEW_AUDIT_LOG == 1 << 7


def test_the_module_is_registered_and_every_event_has_a_known_category():
    assert MODULES[MODULE].settings_model is ModLogSettings
    assert {spec.category for spec in EVENTS.values()} == set(CATEGORIES)
    assert all(len(key) <= 32 for key in EVENTS)  # modlog_entries.event_type is String(32)
    assert all(len(category) <= 16 for category in CATEGORIES)  # ...and category is String(16)
