"""The dashboard tells the operator when the bot stops reporting in.

Docker only marks a hung or disconnected bot as "unhealthy"; nobody is told.
The dashboard process is still running then, so it raises the alarm: one ERROR
log line, which the alert webhook delivers (see core/alerts.py).
"""

import asyncio
import logging
from datetime import timedelta

import pytest

from joltbot.core.health import write_heartbeat
from joltbot.core.types import utcnow
from joltbot.web.app import create_app
from joltbot.web.watchdog import OFFLINE_AFTER, BotWatchdog

from .test_web_access import FakeDiscord, make_config

LOGGER = "joltbot.web.watchdog"


class Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = utcnow()

    def __call__(self):
        return self.now

    def forward(self, **amount) -> None:
        self.now += timedelta(**amount)


@pytest.fixture
def clock():
    return Clock()


async def _beat(db, when):
    async with db.session() as session, session.begin():
        await write_heartbeat(session, when)


def _errors(caplog):
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno >= logging.ERROR]


async def test_a_connected_bot_raises_no_alarm(db, clock, caplog):
    watchdog = BotWatchdog(db, clock=clock)
    for _ in range(20):
        clock.forward(minutes=1)
        await _beat(db, clock.now)
        await watchdog.check()
    assert _errors(caplog) == []


async def test_one_alarm_when_the_bot_has_been_gone_for_five_minutes(db, clock, caplog):
    watchdog = BotWatchdog(db, clock=clock)
    clock.forward(minutes=10)
    await _beat(db, clock.now)
    last_seen = clock.now

    clock.forward(seconds=OFFLINE_AFTER.total_seconds() - 1)
    await watchdog.check()
    assert _errors(caplog) == []  # a restart or a short reconnect is not worth an alarm

    for _ in range(30):  # ...and it stays down for another half hour
        clock.forward(minutes=1)
        await watchdog.check()

    (alarm,) = _errors(caplog)  # one alarm, not one a minute
    assert f"{last_seen:%Y-%m-%d %H:%M}" in alarm and "docker compose logs bot" in alarm


async def test_the_all_clear_is_logged_and_a_later_outage_raises_a_new_alarm(db, clock, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    watchdog = BotWatchdog(db, clock=clock)
    clock.forward(minutes=10)
    await watchdog.check()
    assert len(_errors(caplog)) == 1

    await _beat(db, clock.now)
    await watchdog.check()
    assert any("connected to Discord again" in r.getMessage() for r in caplog.records)

    clock.forward(minutes=10)
    await watchdog.check()
    assert len(_errors(caplog)) == 2


async def test_no_alarm_right_after_the_dashboard_starts(db, clock, caplog):
    # During an update both processes restart. The old heartbeat can look stale for a
    # moment; the bot needs a minute or two to connect and write a new one.
    await _beat(db, clock.now - timedelta(minutes=30))
    watchdog = BotWatchdog(db, clock=clock)
    clock.forward(minutes=4)
    await watchdog.check()
    assert _errors(caplog) == []

    clock.forward(minutes=2)  # still nothing from the bot: now it is a real problem
    await watchdog.check()
    assert len(_errors(caplog)) == 1


async def test_a_bot_that_never_connected_raises_the_alarm_too(db, clock, caplog):
    watchdog = BotWatchdog(db, clock=clock)
    clock.forward(minutes=6)
    await watchdog.check()
    (alarm,) = _errors(caplog)
    assert "has not connected to Discord" in alarm


async def test_a_database_hiccup_does_not_stop_the_watchdog(db, clock, caplog, monkeypatch):
    watchdog = BotWatchdog(db, clock=clock)
    calls = []

    async def check():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("database is locked")
        raise KeyboardInterrupt  # ends the loop for the test

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(watchdog, "check", check)
    monkeypatch.setattr("joltbot.web.watchdog.asyncio.sleep", no_wait)
    with pytest.raises(KeyboardInterrupt):
        await watchdog.run()
    assert len(calls) == 2  # the failed check was followed by another one
    assert _errors(caplog) == []  # and a failed check is not itself an alarm


async def test_the_dashboard_starts_the_watchdog_and_stops_it_on_shutdown(db):
    app = create_app(make_config(), db, FakeDiscord(), check_schema=False)
    async with app.router.lifespan_context(app):
        running = [t for t in asyncio.all_tasks() if "BotWatchdog.run" in repr(t.get_coro())]
        assert len(running) == 1
    assert running[0].cancelled()
