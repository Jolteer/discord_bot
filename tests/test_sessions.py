"""Login sessions in the database: secrets stay out of error text, and expired rows are removed."""

import asyncio
import logging
from datetime import timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from joltbot.core.logsetup import JsonFormatter
from joltbot.core.models import WebSession
from joltbot.core.types import utcnow
from joltbot.web import sessions
from joltbot.web.app import create_app
from joltbot.web.sessions import create_session, purge_expired, purge_expired_forever

from .test_web_access import FakeDiscord, make_config

ACCESS_TOKEN = "discord-access-token-that-must-stay-secret"
WEEK = 7 * 24 * 3600


async def _login(db, user_id=1001):
    """Store a session as a successful login would. Returns the cookie token."""
    return await create_session(
        db,
        "test-secret",
        user={"id": str(user_id), "username": "alice"},
        access_token=ACCESS_TOKEN,
        expires_in=WEEK,
        guilds=[],
    )


async def _session_ids(db):
    async with db.session() as session:
        return set((await session.execute(select(WebSession.id))).scalars())


async def _expire(db, session_id):
    async with db.session() as session, session.begin():
        await session.execute(
            update(WebSession).where(WebSession.id == session_id).values(expires_at=utcnow() - timedelta(minutes=1))
        )


# ------------------------------------------------------------------ secrets in error text


async def test_a_failed_database_write_does_not_put_the_access_token_in_the_error(db):
    await _login(db)
    (session_id,) = await _session_ids(db)
    async with db.session() as session:
        row = await session.get(WebSession, session_id)
    now = utcnow()
    twin = WebSession(  # the same primary key again, so the insert fails
        id=row.id,
        user_id=row.user_id,
        username=row.username,
        avatar=None,
        access_token=ACCESS_TOKEN,
        token_expires_at=now,
        guilds_cache=None,
        guilds_fetched_at=None,
        csrf_token="csrf-token-that-must-stay-secret",
        created_at=now,
        expires_at=now,
    )
    with pytest.raises(SQLAlchemyError) as failure:
        async with db.session() as session, session.begin():
            session.add(twin)

    # What ends up in the log file and in the operator's alert: the message and the traceback.
    error_info = (failure.type, failure.value, failure.tb)
    record = logging.LogRecord("joltbot", logging.ERROR, __file__, 1, "Login failed", None, error_info)
    for text in (str(failure.value), JsonFormatter().format(record)):
        assert "web_sessions" in text  # still says which statement failed
        assert ACCESS_TOKEN not in text and "csrf-token-that-must-stay-secret" not in text


# ------------------------------------------------------------------ removing expired sessions


async def test_expired_sessions_are_removed_and_live_ones_kept(db):
    await _login(db, 1001)
    (expired,) = await _session_ids(db)
    await _login(db, 1002)
    (live,) = await _session_ids(db) - {expired}
    await _expire(db, expired)

    assert await purge_expired(db) == 1
    assert await _session_ids(db) == {live}
    assert await purge_expired(db) == 0


async def test_the_dashboard_removes_expired_sessions_at_start_and_stops_the_job_on_shutdown(db):
    await _login(db)
    (expired,) = await _session_ids(db)
    await _expire(db, expired)

    app = create_app(make_config(), db, FakeDiscord(), check_schema=False)
    async with app.router.lifespan_context(app):
        running = [t for t in asyncio.all_tasks() if "purge_expired_forever" in repr(t.get_coro())]
        assert len(running) == 1
        for _ in range(100):  # the first round runs as soon as the dashboard is up
            if not await _session_ids(db):
                break
            await asyncio.sleep(0.01)
        assert await _session_ids(db) == set()
    assert running[0].cancelled()


async def test_a_failed_round_is_followed_by_another_and_raises_no_alarm(db, monkeypatch, caplog):
    rounds = []

    async def purge(_db):
        rounds.append(1)
        if len(rounds) == 1:
            raise OSError("database is locked")
        raise KeyboardInterrupt  # ends the loop for the test

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(sessions, "purge_expired", purge)
    monkeypatch.setattr("joltbot.web.sessions.asyncio.sleep", no_wait)
    with pytest.raises(KeyboardInterrupt):
        await purge_expired_forever(db)
    assert len(rounds) == 2
    # A WARNING, not an ERROR: errors are sent to the operator's alert channel.
    assert [r.levelname for r in caplog.records if r.name == "joltbot.web.sessions"] == ["WARNING"]
