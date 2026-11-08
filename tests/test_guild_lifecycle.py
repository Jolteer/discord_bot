"""Joining, leaving, startup sync and the 30-day purge."""

from datetime import timedelta

from sqlalchemy import func, select

from joltbot.core.guild_settings import Actor, save_module
from joltbot.core.guilds import GuildInfo, ensure_guild, mark_guild_left, purge_left_guilds, sync_guilds
from joltbot.core.models import AuditLog, Guild, ModuleSetting, Ticket
from joltbot.core.types import utcnow
from joltbot.modules.registry import MODULES
from joltbot.modules.tickets.settings import TicketSettings

from .conftest import GUILD_A, GUILD_B

GUILD_C = 333333333333333333


async def _count(session, model, guild_id):
    column = Guild.id if model is Guild else model.guild_id
    return (await session.execute(select(func.count()).select_from(model).where(column == guild_id))).scalar_one()


async def test_join_creates_every_module_switched_off(db):
    async with db.session() as session, session.begin():
        await ensure_guild(session, GuildInfo(GUILD_A, "A"))
    async with db.session() as session:
        rows = (await session.execute(select(ModuleSetting).where(ModuleSetting.guild_id == GUILD_A))).scalars().all()
    assert {r.module for r in rows} == set(MODULES)
    assert all(r.enabled is False for r in rows)


async def test_leave_then_rejoin_keeps_settings(db, two_guilds):
    actor = Actor(1, "admin", "dashboard")
    async with db.session() as session, session.begin():
        await save_module(
            session,
            GUILD_A,
            "tickets",
            enabled=True,
            config=TicketSettings(staff_role_ids=[5]),
            expected_version=1,
            actor=actor,
        )
        await mark_guild_left(session, GUILD_A)
    async with db.session() as session:
        guild = await session.get(Guild, GUILD_A)
        assert guild.active is False and guild.left_at is not None
    async with db.session() as session, session.begin():
        await ensure_guild(session, GuildInfo(GUILD_A, "A again"))
    async with db.session() as session:
        guild = await session.get(Guild, GUILD_A)
        setting = await session.get(ModuleSetting, (GUILD_A, "tickets"))
    assert guild.active is True and guild.left_at is None and guild.name == "A again"
    assert setting.config["staff_role_ids"] == ["5"]


async def test_startup_sync(db, two_guilds):
    # The bot is now in A and C. B is gone, unless Discord says it's just unavailable.
    async with db.session() as session, session.begin():
        joined, left = await sync_guilds(session, [GuildInfo(GUILD_A, "A"), GuildInfo(GUILD_C, "C")])
    assert joined == [GUILD_C]
    assert left == [GUILD_B]


async def test_startup_sync_ignores_unavailable_guilds(db, two_guilds):
    async with db.session() as session, session.begin():
        _, left = await sync_guilds(session, [GuildInfo(GUILD_A, "A")], unavailable_ids={GUILD_B})
    assert left == []
    async with db.session() as session:
        assert (await session.get(Guild, GUILD_B)).active is True


async def test_purge_after_30_days_deletes_everything_for_that_guild_only(db, two_guilds):
    actor = Actor(1, "admin", "dashboard")
    async with db.session() as session, session.begin():
        for guild_id in (GUILD_A, GUILD_B):
            await save_module(
                session,
                guild_id,
                "tickets",
                enabled=True,
                config=TicketSettings(staff_role_ids=[5]),
                expected_version=1,
                actor=actor,
            )
            session.add(Ticket(guild_id=guild_id, user_id=1, username="u", answers=[]))
        await mark_guild_left(session, GUILD_A)

    async with db.session() as session, session.begin():
        assert await purge_left_guilds(session, now=utcnow() + timedelta(days=29)) == []
    async with db.session() as session, session.begin():
        assert await purge_left_guilds(session, now=utcnow() + timedelta(days=31)) == [GUILD_A]

    async with db.session() as session:
        for model in (Guild, ModuleSetting, Ticket, AuditLog):
            assert await _count(session, model, GUILD_A) == 0, model.__name__
            assert await _count(session, model, GUILD_B) > 0, model.__name__


async def test_active_guilds_are_never_purged(db, two_guilds):
    async with db.session() as session, session.begin():
        assert await purge_left_guilds(session, now=utcnow() + timedelta(days=365)) == []
