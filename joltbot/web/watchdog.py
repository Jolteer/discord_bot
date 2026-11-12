"""Tells the operator when the bot has stopped reporting in.

A bot that has crashed logs its own error. A bot that hangs, or that keeps
running but can't reach Discord, logs nothing an alert could be built on:
Docker marks it "unhealthy" and that is all. The dashboard is a separate
process and is still running then, so it watches the bot's heartbeat
(``core/health.py``) and logs one ERROR when the bot has been gone too long.
With ``ALERT_WEBHOOK_URL`` set, that line reaches the operator
(``core/alerts.py``).

It can't report the whole server being down. For that, point an uptime
monitor at ``/health/bot`` (see the README).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from ..core.db import Database
from ..core.health import bot_status
from ..core.types import utcnow

log = logging.getLogger(__name__)

CHECK_SECONDS = 60
# Longer than health.STALE_AFTER (when the bot starts to show as offline), so a restart,
# an update or a short gateway reconnect never raises an alarm.
OFFLINE_AFTER = timedelta(minutes=5)


class BotWatchdog:
    """Logs one ERROR per outage of the bot, and one INFO when it is back.

    Args:
        db: The dashboard's database.
        clock: Returns the current UTC time; replaced in tests.
    """

    def __init__(self, db: Database, *, clock: Callable[[], datetime] = utcnow) -> None:
        self._db = db
        self._clock = clock
        self._started = clock()
        self._alerted = False

    async def check(self) -> None:
        """Read the heartbeat once and raise or clear the alarm if needed."""
        now = self._clock()
        async with self._db.session() as session:
            status = await bot_status(session, now)

        if status.online:
            if self._alerted:
                log.info("The bot is connected to Discord again.")
                self._alerted = False
            return
        if self._alerted:
            return  # this outage was already reported
        if now - self._started < OFFLINE_AFTER:
            return  # the dashboard just started (an update?); give the bot time to connect
        if status.last_seen is None:
            self._alerted = True
            log.error(
                "The bot has not connected to Discord since the dashboard started. "
                "Check `docker compose ps` and `docker compose logs bot`."
            )
        elif now - status.last_seen >= OFFLINE_AFTER:
            self._alerted = True
            log.error(
                "The bot has not been connected to Discord since %s UTC. Buttons and commands don't "
                "respond until it is back. Check `docker compose ps` and `docker compose logs bot`.",
                f"{status.last_seen:%Y-%m-%d %H:%M}",
            )

    async def run(self) -> None:
        """Check once a minute, forever. Cancel the task to stop it."""
        while True:
            await asyncio.sleep(CHECK_SECONDS)
            try:
                await self.check()
            except Exception:
                # A check that fails (e.g. the database is busy) says nothing about the bot.
                log.warning("Couldn't check the bot's heartbeat", exc_info=True)
