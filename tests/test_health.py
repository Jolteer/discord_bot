"""The bot's heartbeat: when it is written, and how the dashboard and Docker read it."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from joltbot.bot import healthcheck
from joltbot.bot.heartbeat import Heartbeat
from joltbot.core.config import AppConfig
from joltbot.core.db import Database
from joltbot.core.health import HEARTBEAT_KEY, STALE_AFTER, bot_status, write_heartbeat
from joltbot.core.models import BotMeta
from joltbot.core.types import utcnow
from joltbot.web.app import create_app

from .test_web_access import FakeDiscord, client_for, make_config


async def _status(db, now=None):
    async with db.session() as session:
        return await bot_status(session, now)


async def _beat(db, when=None):
    async with db.session() as session, session.begin():
        await write_heartbeat(session, when)


# ------------------------------------------------------------------ reading and writing


async def test_a_bot_that_never_reported_in_is_offline(db):
    status = await _status(db)
    assert status.online is False and status.last_seen is None


async def test_a_recent_heartbeat_means_online(db):
    await _beat(db)
    status = await _status(db)
    assert status.online is True and utcnow() - status.last_seen < timedelta(seconds=5)


async def test_the_bot_goes_offline_after_three_missed_beats(db):
    now = utcnow()
    await _beat(db, now)
    assert (await _status(db, now + STALE_AFTER - timedelta(seconds=1))).online is True
    late = await _status(db, now + STALE_AFTER)
    assert late.online is False and late.last_seen == now  # still says when it was last seen


async def test_each_beat_replaces_the_last(db):
    await _beat(db, utcnow() - timedelta(hours=1))
    await _beat(db)
    assert (await _status(db)).online is True


@pytest.mark.parametrize("garbage", ["", "yesterday", "2026-11-12T02:00:00"])  # the last one has no timezone
async def test_an_unreadable_heartbeat_counts_as_offline(db, garbage):
    async with db.session() as session, session.begin():
        session.add(BotMeta(key=HEARTBEAT_KEY, value=garbage))
    status = await _status(db)
    assert status.online is False and status.last_seen is None


# ------------------------------------------------------------------ the bot side


def _fake_bot(db, ready=True, closed=False):
    return SimpleNamespace(db=db, is_ready=lambda: ready, is_closed=lambda: closed)


async def test_heartbeat_is_written_only_while_connected_to_discord(db):
    cog = Heartbeat(_fake_bot(db))
    await cog.beat()  # process is up, but no gateway connection yet
    assert (await _status(db)).last_seen is None

    await cog.on_connect()
    await cog.beat()
    assert (await _status(db)).online is True

    # Connection drops: the old beat ages out because no new one is written.
    old = utcnow() - timedelta(minutes=10)
    await _beat(db, old)
    await cog.on_disconnect()
    await cog.beat()
    assert (await _status(db)).last_seen == old

    await cog.on_resumed()
    await cog.beat()
    assert (await _status(db)).online is True


@pytest.mark.parametrize(("ready", "closed"), [(False, False), (True, True)])
async def test_no_heartbeat_before_ready_or_after_close(db, ready, closed):
    cog = Heartbeat(_fake_bot(db, ready=ready, closed=closed))
    await cog.on_connect()
    await cog.beat()
    assert (await _status(db)).last_seen is None


async def test_a_database_hiccup_does_not_stop_the_heartbeat(db, caplog):
    class BrokenDb:
        def session(self):
            raise RuntimeError("database is locked")

    cog = Heartbeat(_fake_bot(BrokenDb()))
    await cog.on_connect()
    await cog.beat()  # must not raise, or the loop would stop for good
    assert "Couldn't write the bot heartbeat" in caplog.text
    assert all(record.levelname == "WARNING" for record in caplog.records)  # not an operator alert


# ------------------------------------------------------------------ the dashboard side


async def test_bot_health_endpoint_follows_the_heartbeat(db):
    app = create_app(make_config(), db, FakeDiscord(), check_schema=False)
    async with client_for(app) as client:
        r = await client.get("/health/bot")
        assert (r.status_code, r.json()) == (503, {"status": "offline"})

        await _beat(db)
        r = await client.get("/health/bot")
        assert (r.status_code, r.json()) == (200, {"status": "ok"})

        await _beat(db, utcnow() - timedelta(minutes=10))
        assert (await client.get("/health/bot")).status_code == 503
        # The dashboard's own health check must not care whether the bot is up.
        assert (await client.get("/health")).status_code == 200


async def test_bot_health_endpoint_with_a_broken_database(db):
    class BrokenDb:
        def session(self):
            raise RuntimeError("database is gone")

    app = create_app(make_config(), db, FakeDiscord(), check_schema=False)
    app.state.db = BrokenDb()
    async with client_for(app) as client:
        r = await client.get("/health/bot")
    assert (r.status_code, r.json()) == (503, {"status": "error"})


# ------------------------------------------------------------------ python -m joltbot.bot.healthcheck


def _run_healthcheck(monkeypatch, url):
    monkeypatch.setattr(healthcheck, "get_config", lambda: AppConfig(_env_file=None, database_url=url))
    with pytest.raises(SystemExit) as exit_info:
        healthcheck.main()
    return exit_info.value.code


def test_docker_health_check_exit_codes(tmp_path, monkeypatch, capsys):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'health.db').as_posix()}"

    async def prepare(beat_at=None):
        db = Database(url)
        await db.create_all()
        if beat_at is not None:
            async with db.session() as session, session.begin():
                await write_heartbeat(session, beat_at)
        await db.dispose()

    asyncio.run(prepare())
    assert _run_healthcheck(monkeypatch, url) == 1
    assert "no heartbeat yet" in capsys.readouterr().out

    asyncio.run(prepare(utcnow()))
    assert _run_healthcheck(monkeypatch, url) == 0
    assert capsys.readouterr().out.strip() == "healthy"

    asyncio.run(prepare(utcnow() - timedelta(minutes=10)))
    assert _run_healthcheck(monkeypatch, url) == 1
    assert "last heartbeat" in capsys.readouterr().out


def test_docker_health_check_without_a_usable_database(tmp_path, monkeypatch, capsys):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'empty.db').as_posix()}"  # no tables: migrations never ran
    assert _run_healthcheck(monkeypatch, url) == 1
    assert "couldn't read the database" in capsys.readouterr().out
