"""Settings are stored per guild, audited, and never leak between guilds."""
import pytest
from sqlalchemy import select, update

from joltbot.core.guild_settings import Actor, StaleSettingsError, load_all, load_module, save_module
from joltbot.core.models import AuditLog, ModuleSetting
from joltbot.modules.general.settings import GeneralSettings
from joltbot.modules.tickets.settings import TicketSettings

ADMIN = Actor(user_id=999, name="admin#0001", source="dashboard")


async def test_unconfigured_guild_loads_defaults_switched_off(db, two_guilds):
    guild_a, _ = two_guilds
    async with db.session() as session:
        state = await load_module(session, guild_a, "tickets")
    assert state.enabled is False
    assert isinstance(state.config, TicketSettings)
    assert state.config == TicketSettings()


async def test_guild_with_no_rows_at_all_still_loads(db):
    # E.g. an event arrives before on_guild_join finished. Must not crash.
    async with db.session() as session:
        states = await load_all(session, 123456789012345678)
    assert states["tickets"].enabled is False
    assert states["tickets"].version == 0
    assert states["general"].enabled is True  # general settings are always on


async def test_save_then_load(db, two_guilds):
    guild_a, _ = two_guilds
    new = TicketSettings(staff_role_ids=[42], log_channel_id=43)
    async with db.session() as session, session.begin():
        current = await load_module(session, guild_a, "tickets")
        saved = await save_module(session, guild_a, "tickets", enabled=True, config=new,
                                  expected_version=current.version, actor=ADMIN)
    assert saved.version == current.version + 1

    async with db.session() as session:
        loaded = await load_module(session, guild_a, "tickets")
    assert loaded.enabled is True
    assert loaded.config.staff_role_ids == [42]
    assert loaded.config.log_channel_id == 43


async def test_one_guilds_settings_never_show_up_in_another(db, two_guilds):
    guild_a, guild_b = two_guilds
    async with db.session() as session, session.begin():
        await save_module(session, guild_a, "tickets", enabled=True, config=TicketSettings(staff_role_ids=[42]),
                          expected_version=1, actor=ADMIN)
    async with db.session() as session:
        b = await load_module(session, guild_b, "tickets")
        b_audit = (await session.execute(select(AuditLog).where(AuditLog.guild_id == guild_b))).scalars().all()
    assert b.enabled is False
    assert b.config.staff_role_ids == []
    assert b_audit == []


async def test_save_writes_an_audit_entry_with_old_and_new_values(db, two_guilds):
    guild_a, _ = two_guilds
    config = TicketSettings()
    config.messages.panel_title = "Hello"
    async with db.session() as session, session.begin():
        await save_module(session, guild_a, "tickets", enabled=True, config=config, expected_version=1, actor=ADMIN)
    async with db.session() as session:
        entry = (await session.execute(select(AuditLog))).scalar_one()
    assert entry.guild_id == guild_a
    assert entry.actor_id == 999 and entry.source == "dashboard"
    assert entry.changes["enabled"] == [False, True]
    assert entry.changes["messages.panel_title"] == ["📝 Verification", "Hello"]
    assert len(entry.changes) == 2  # unchanged fields aren't listed


async def test_saving_without_changes_writes_nothing(db, two_guilds):
    guild_a, _ = two_guilds
    async with db.session() as session, session.begin():
        state = await save_module(session, guild_a, "tickets", enabled=False, config=TicketSettings(),
                                  expected_version=1, actor=ADMIN)
    assert state.version == 1
    async with db.session() as session:
        assert (await session.execute(select(AuditLog))).first() is None


async def test_stale_save_is_rejected(db, two_guilds):
    guild_a, _ = two_guilds
    # Two admins load version 1. The first one saves...
    async with db.session() as session, session.begin():
        await save_module(session, guild_a, "tickets", enabled=True, config=TicketSettings(staff_role_ids=[1]),
                          expected_version=1, actor=ADMIN)
    # ...so the second one's save (still based on version 1) must not overwrite it.
    with pytest.raises(StaleSettingsError):
        async with db.session() as session, session.begin():
            await save_module(session, guild_a, "tickets", enabled=True, config=TicketSettings(staff_role_ids=[2]),
                              expected_version=1, actor=ADMIN)
    async with db.session() as session:
        assert (await load_module(session, guild_a, "tickets")).config.staff_role_ids == [1]


async def test_wrong_settings_class_is_rejected(db, two_guilds):
    guild_a, _ = two_guilds
    with pytest.raises(TypeError):
        async with db.session() as session, session.begin():
            await save_module(session, guild_a, "tickets", enabled=True, config=GeneralSettings(),
                              expected_version=1, actor=ADMIN)


async def test_corrupt_stored_settings_fall_back_to_defaults_and_off(db, two_guilds):
    guild_a, _ = two_guilds
    async with db.session() as session, session.begin():
        await session.execute(
            update(ModuleSetting)
            .where(ModuleSetting.guild_id == guild_a, ModuleSetting.module == "tickets")
            .values(enabled=True, config={"staff_role_ids": ["not-an-id"]})
        )
    async with db.session() as session:
        state = await load_module(session, guild_a, "tickets")
    assert state.enabled is False
    assert state.config == TicketSettings()


async def test_general_module_is_always_enabled(db, two_guilds):
    guild_a, _ = two_guilds
    async with db.session() as session, session.begin():
        state = await save_module(session, guild_a, "general", enabled=False,
                                  config=GeneralSettings(embed_color="#123456"), expected_version=1, actor=ADMIN)
    assert state.enabled is True
