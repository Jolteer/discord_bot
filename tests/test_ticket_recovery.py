"""The ticket store and the recovery that fixes the old bot's "stuck until restart" bugs.

These use tiny fake Discord objects (tests/fakes.py), so no network or bot token is needed.
"""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import discord
from sqlalchemy import update

from joltbot.core.models import Ticket
from joltbot.core.types import utcnow
from joltbot.modules.tickets import store
from joltbot.modules.tickets.service import STUCK_AFTER_SECONDS, TicketManager

from .conftest import GUILD_A, GUILD_B
from .fakes import FakeBot, FakeGuild, http_error
from .fakes import settle as _settle


async def _reserve(db, guild_id=GUILD_A, user_id=1):
    return await store.reserve(
        db, guild_id=guild_id, user_id=user_id, username="u", answers=[{"label": "Q", "value": "A"}]
    )


async def _age(db, ticket_id, **columns):
    """Pretend something happened long ago."""
    old = utcnow() - timedelta(seconds=STUCK_AFTER_SECONDS + 60)
    async with db.session() as session, session.begin():
        await session.execute(update(Ticket).where(Ticket.id == ticket_id).values(**{c: old for c in columns}))


async def test_one_active_ticket_per_member(db, two_guilds):
    assert await _reserve(db) is not None
    assert await _reserve(db) is None  # same member, same server
    assert await _reserve(db, guild_id=GUILD_B) is not None  # same member, other server is fine


async def test_only_one_claim_wins(db, two_guilds):
    ticket_id = await _reserve(db)
    results = await asyncio.gather(
        store.claim(db, ticket_id, outcome="accepted", handled_by=1),
        store.claim(db, ticket_id, outcome="denied", handled_by=2),
    )
    assert sorted(results) == [False, True]


async def test_release_reopens_ticket(db, two_guilds):
    ticket_id = await _reserve(db)
    assert await store.claim(db, ticket_id, outcome="accepted", handled_by=1)
    await store.release(db, ticket_id)
    ticket = await store.get(db, ticket_id)
    assert ticket.status == "open" and ticket.outcome is None and ticket.claimed_at is None


async def test_recover_removes_old_reservation_without_channel(db, two_guilds):
    old_id = await _reserve(db, user_id=1)
    new_id = await _reserve(db, user_id=2)
    await _age(db, old_id, created_at=True)
    manager = TicketManager(FakeBot(db, [FakeGuild(GUILD_A)]))
    await manager.recover(FakeGuild(GUILD_A))
    assert await store.get(db, old_id) is None  # applicant can apply again
    assert await store.get(db, new_id) is not None  # may still be mid-creation; left alone


async def test_recover_finishes_ticket_stuck_in_closing(db, two_guilds):
    stuck_id = await _reserve(db, user_id=1)
    fresh_id = await _reserve(db, user_id=2)
    for ticket_id in (stuck_id, fresh_id):
        await store.set_channel(db, ticket_id, 9000 + ticket_id)
        await store.claim(db, ticket_id, outcome="accepted", handled_by=5)
    await _age(db, stuck_id, claimed_at=True)

    guild = FakeGuild(GUILD_A, channels=(), members=(1, 2))
    bot = FakeBot(db, [guild])
    manager = TicketManager(bot)
    await manager.recover(guild)
    await _settle(manager)

    stuck = await store.get(db, stuck_id)
    fresh = await store.get(db, fresh_id)
    assert stuck.status == "closed"
    assert fresh.status == "closing"  # just claimed; something may still be working on it
    # No log channel is set up, so the admin is told on the dashboard.
    assert (GUILD_A, "tickets.log_channel") in bot.issues


async def test_recover_skips_tickets_being_worked_on(db, two_guilds):
    ticket_id = await _reserve(db, user_id=1)
    await store.set_channel(db, ticket_id, 9001)
    await store.claim(db, ticket_id, outcome="accepted", handled_by=5)
    await _age(db, ticket_id, claimed_at=True)
    guild = FakeGuild(GUILD_A, members=(1,))
    manager = TicketManager(FakeBot(db, [guild]))
    manager._busy.add(ticket_id)
    await manager.recover(guild)
    await _settle(manager)
    assert (await store.get(db, ticket_id)).status == "closing"


async def test_recover_closes_ticket_whose_member_left(db, two_guilds):
    ticket_id = await _reserve(db, user_id=1)
    await store.set_channel(db, ticket_id, 9001)
    guild = FakeGuild(GUILD_A, channels=(9001,), members=())  # channel exists, member gone
    manager = TicketManager(FakeBot(db, [guild]))
    await manager.recover(guild)
    await _settle(manager)
    ticket = await store.get(db, ticket_id)
    assert ticket.outcome == "left" and ticket.status == "closed"


async def test_recover_keeps_ticket_when_member_is_only_missing_from_the_cache(db, two_guilds):
    # Discord's member list can fail to load at startup. The applicant is still in the
    # server, so their ticket must not be closed as "left".
    ticket_id = await _reserve(db, user_id=1)
    await store.set_channel(db, ticket_id, 9001)
    guild = FakeGuild(GUILD_A, channels=(9001,), members=())
    guild.add_member(1, cached=False)
    manager = TicketManager(FakeBot(db, [guild]))
    await manager.recover(guild)
    await _settle(manager)
    ticket = await store.get(db, ticket_id)
    assert ticket.status == "open" and ticket.outcome is None


async def test_recover_keeps_ticket_when_discord_cannot_say_whether_the_member_left(db, two_guilds):
    ticket_id = await _reserve(db, user_id=1)
    await store.set_channel(db, ticket_id, 9001)
    guild = FakeGuild(GUILD_A, channels=(9001,), members=())
    guild.fetch_member = AsyncMock(side_effect=http_error(discord.HTTPException, 503))
    manager = TicketManager(FakeBot(db, [guild]))
    await manager.recover(guild)
    await _settle(manager)
    assert (await store.get(db, ticket_id)).status == "open"  # checked again on the next round
