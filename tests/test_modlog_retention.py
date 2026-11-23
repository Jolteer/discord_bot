"""The mod log cleanup: entries older than each server's retention period are deleted, nothing else."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from sqlalchemy import delete, select, update

from joltbot.core.guild_settings import load_module, save_module
from joltbot.core.models import ModLogEntry, ModuleSetting
from joltbot.modules.modlog import retention, store
from joltbot.modules.modlog.cog import ModLog
from joltbot.modules.modlog.constants import EVENT_MEMBER_JOIN, MODULE
from joltbot.modules.modlog.settings import ModLogSettings

from .conftest import GUILD_A, GUILD_B
from .fakes import TEST_ACTOR, FakeBot
from .modlog_helpers import all_log_channels

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


async def _add(db, guild_id: int, *ages: timedelta) -> None:
    """Save one entry per age, each created that long before ``NOW``."""
    async with db.session() as session, session.begin():
        for age in ages:
            session.add(
                ModLogEntry(
                    guild_id=guild_id,
                    event_type=EVENT_MEMBER_JOIN,
                    category="members",
                    details={"content": "a deleted message"},
                    created_at=NOW - age,
                )
            )


async def _ages(db, guild_id: int) -> list[timedelta]:
    """How old each of a server's remaining entries is, youngest first."""
    async with db.session() as session:
        result = await session.execute(select(ModLogEntry.created_at).where(ModLogEntry.guild_id == guild_id))
        return sorted(NOW - created for created in result.scalars())


async def _set_retention(db, guild_id: int, days: int, *, enabled: bool = True) -> None:
    """Save mod log settings with this retention period, logging on or off."""
    config = ModLogSettings(**all_log_channels(), retention_days=days)
    async with db.session() as session, session.begin():
        current = await load_module(session, guild_id, MODULE)
        await save_module(
            session,
            guild_id,
            MODULE,
            enabled=enabled,
            config=config,
            expected_version=current.version,
            actor=TEST_ACTOR,
        )


def days(n: float) -> timedelta:
    return timedelta(days=n)


# ------------------------------------------------------------------ what is deleted


async def test_entries_older_than_the_retention_period_are_deleted_and_the_rest_kept(db, two_guilds):
    await _set_retention(db, GUILD_A, 7)
    exactly_at_cutoff = days(7)
    await _add(db, GUILD_A, days(1), days(6.9), exactly_at_cutoff, days(7) + timedelta(seconds=1), days(40))

    assert await retention.purge_expired(db, now=NOW) == {GUILD_A: 2}
    assert await _ages(db, GUILD_A) == [days(1), days(6.9), exactly_at_cutoff]
    assert await retention.purge_expired(db, now=NOW) == {}  # nothing left to delete


async def test_each_server_uses_its_own_retention_and_never_touches_another_servers_entries(db, two_guilds):
    await _set_retention(db, GUILD_A, 7)
    await _set_retention(db, GUILD_B, 30)
    await _add(db, GUILD_A, days(3), days(10), days(45))
    await _add(db, GUILD_B, days(3), days(10), days(45))

    assert await retention.purge_expired(db, now=NOW) == {GUILD_A: 2, GUILD_B: 1}
    assert await _ages(db, GUILD_A) == [days(3)]
    assert await _ages(db, GUILD_B) == [days(3), days(10)]


async def test_a_server_with_logging_switched_off_is_still_cleaned(db, two_guilds):
    await _set_retention(db, GUILD_A, 7, enabled=False)
    async with db.session() as session:
        assert (await load_module(session, GUILD_A, MODULE)).enabled is False
    await _add(db, GUILD_A, days(3), days(10))

    assert await retention.purge_expired(db, now=NOW) == {GUILD_A: 1}
    assert await _ages(db, GUILD_A) == [days(3)]


async def test_a_server_without_settings_or_with_invalid_settings_keeps_entries_for_30_days(db, two_guilds):
    async with db.session() as session, session.begin():
        await session.execute(
            delete(ModuleSetting).where(ModuleSetting.guild_id == GUILD_A, ModuleSetting.module == MODULE)
        )
        await session.execute(
            update(ModuleSetting)
            .where(ModuleSetting.guild_id == GUILD_B, ModuleSetting.module == MODULE)
            .values(config={"retention_days": 0})  # fails validation, so the defaults apply
        )
    await _add(db, GUILD_A, days(29), days(31))
    await _add(db, GUILD_B, days(29), days(31))

    assert await retention.purge_expired(db, now=NOW) == {GUILD_A: 1, GUILD_B: 1}
    assert await _ages(db, GUILD_A) == [days(29)]
    assert await _ages(db, GUILD_B) == [days(29)]


async def test_more_entries_than_one_batch_are_all_deleted(db, two_guilds):
    await _set_retention(db, GUILD_A, 7)
    await _add(db, GUILD_A, days(1), *(days(10 + n) for n in range(5)))

    assert await store.delete_older_than(db, GUILD_A, NOW - days(7), batch=2) == 5
    assert await _ages(db, GUILD_A) == [days(1)]


async def test_a_failure_in_one_server_does_not_stop_the_others(db, two_guilds, monkeypatch, caplog):
    await _set_retention(db, GUILD_A, 7)
    await _set_retention(db, GUILD_B, 7)
    await _add(db, GUILD_A, days(10))
    await _add(db, GUILD_B, days(10))
    real_delete = store.delete_older_than

    async def delete_fails_for_a(db, guild_id, cutoff, **kwargs):
        if guild_id == GUILD_A:
            raise OSError("database is locked")
        return await real_delete(db, guild_id, cutoff, **kwargs)

    monkeypatch.setattr(store, "delete_older_than", delete_fails_for_a)
    with caplog.at_level(logging.ERROR, logger="joltbot.modules.modlog.retention"):
        assert await retention.purge_expired(db, now=NOW) == {GUILD_B: 1}
    assert await _ages(db, GUILD_A) == [days(10)]
    assert await _ages(db, GUILD_B) == []
    assert [r.getMessage() for r in caplog.records if r.name == "joltbot.modules.modlog.retention"] == [
        f"Guild {GUILD_A}: couldn't delete old mod log entries"
    ]


# ------------------------------------------------------------------ the daily loop


def _cog(db) -> ModLog:
    bot = FakeBot(db, [])
    bot.wait_until_ready = AsyncMock()  # type: ignore[attr-defined]
    return ModLog(bot)  # type: ignore[arg-type]


async def _until(condition, tries: int = 200) -> None:
    for _ in range(tries):
        if await condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


async def test_the_cleanup_runs_when_the_cog_loads_and_stops_when_it_unloads(db, two_guilds):
    await _set_retention(db, GUILD_A, 7)
    await _add(db, GUILD_A, timedelta(0), days(400))  # created relative to NOW; the loop uses the real clock
    cog = _cog(db)

    await cog.cog_load()
    assert cog.cleanup.is_running()

    async def only_the_new_entry_is_left() -> bool:
        return len(await _ages(db, GUILD_A)) == 1

    await _until(only_the_new_entry_is_left)
    task = cog.cleanup.get_task()
    assert task is not None
    await cog.cog_unload()

    async def stopped() -> bool:
        return task.done()

    try:
        await _until(stopped)
        assert task.cancelled()
    finally:
        task.cancel()  # never leave a loop running into the next test, even when this one fails


async def test_a_failed_round_is_logged_and_the_loop_keeps_going(db, monkeypatch, caplog):
    rounds: list[int] = []
    second_round = asyncio.Event()

    async def purge(_db):
        rounds.append(1)
        if len(rounds) == 1:
            raise OSError("database is locked")
        second_round.set()
        return {}

    monkeypatch.setattr(retention, "purge_expired", purge)
    cog = _cog(db)
    cog.cleanup.change_interval(seconds=0)
    with caplog.at_level(logging.ERROR, logger="joltbot.modules.modlog.cog"):
        await cog.cog_load()
        try:
            await asyncio.wait_for(second_round.wait(), timeout=5)
        finally:
            await cog.cog_unload()
            cog.cleanup.cancel()
    assert len(rounds) >= 2
    assert [r.getMessage() for r in caplog.records if r.name == "joltbot.modules.modlog.cog"] == [
        "Mod log cleanup failed"
    ]
