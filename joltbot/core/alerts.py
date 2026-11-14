"""Operator alerts: ERROR-level log lines are posted to a Discord webhook.

This is for whoever runs JoltBot, not for server admins (they get dashboard
issues). It is off unless ALERT_WEBHOOK_URL is set.

A webhook needs no bot connection, so alerts still arrive when the bot is
offline or stuck in its start-up retry loop, and the dashboard process can
send them too. Sending happens on a background thread, so a slow or
unreachable Discord can never stall the bot or a web request.

Safeguards against flooding the channel:
- the same log line (same logger and message template) is sent at most once
  per 10 minutes; the next one says how many were skipped
- at most 10 alerts per 10 minutes overall, then one "muted" notice
"""

from __future__ import annotations

import atexit
import json
import logging
import queue
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from logging.handlers import QueueHandler, QueueListener

from .limits import MESSAGE_MAX

log = logging.getLogger(__name__)

DEDUPE_SECONDS = 600
WINDOW_SECONDS = 600
MAX_PER_WINDOW = 10
MAX_CONTENT = MESSAGE_MAX  # an alert is one Discord message
QUEUE_SIZE = 200
SECONDS_BETWEEN_POSTS = 0.5  # Discord allows 5 webhook posts per 2 seconds
WEBHOOK_TIMEOUT_SECONDS = 5
TRACEBACK_HEAD_CHARS = 300  # how much of the start of an over-long message is kept
MAX_REMEMBERED_ALERTS = 500  # de-duplication entries kept before expired ones are dropped


class AlertError(Exception):
    """The webhook couldn't be reached or refused the message. Never contains the URL."""


def post_to_webhook(url: str, content: str, timeout: float = WEBHOOK_TIMEOUT_SECONDS) -> None:
    """Post one message to a Discord webhook. Blocks; call it off the event loop.

    Args:
        url: The webhook URL (a secret: anyone who has it can post to the channel).
        content: Message text; cut to Discord's 2000-character limit.
        timeout: Seconds to wait for Discord.

    Raises:
        AlertError: Bad URL, Discord unreachable, or Discord refused the message.
    """
    if not url.startswith("https://"):
        raise AlertError("ALERT_WEBHOOK_URL must start with https://")
    # allowed_mentions: nothing in a log line may ever ping anyone.
    payload = {"content": content[:MAX_CONTENT], "allowed_mentions": {"parse": []}}
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "JoltBot alerts"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            pass
    except urllib.error.HTTPError as e:
        raise AlertError(f"Discord answered {e.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise AlertError(f"couldn't reach Discord ({e.__class__.__name__})") from None


def format_alert(process_name: str, record: logging.LogRecord, repeats: int = 0) -> str:
    """Build the Discord message for one log record.

    Args:
        process_name: ``"bot"`` or ``"web"``.
        record: The record; its message already includes any traceback.
        repeats: How many identical alerts were skipped since the last one sent.

    Returns:
        Message text within Discord's length limit. A long traceback keeps its
        first lines and its end, where the actual error is.
    """
    head = f"🚨 **JoltBot {process_name}** · {record.levelname} · `{record.name}`"
    if repeats:
        head += f" · +{repeats} more like this since the last alert"
    body = record.getMessage().replace("```", "'''")  # can't break out of the code block
    room = MAX_CONTENT - len(head) - len("\n```\n\n```")
    if len(body) > room:
        body = body[:TRACEBACK_HEAD_CHARS] + "\n…\n" + body[-(room - TRACEBACK_HEAD_CHARS - 3) :]
    return f"{head}\n```\n{body}\n```"


class WebhookAlertHandler(logging.Handler):
    """Sends ERROR and above to the webhook, de-duplicated and rate-limited.

    Runs on the alert thread only (behind a ``QueueListener``), so it needs no
    locks and may block.

    Args:
        url: Discord webhook URL.
        process_name: ``"bot"`` or ``"web"``; shown in every alert.
        send: Function that posts ``(url, content)``; defaults to ``post_to_webhook``.
        clock: Monotonic clock; replaced in tests.
        pause: Sleep function used between posts; replaced in tests.
    """

    def __init__(
        self,
        url: str,
        process_name: str,
        *,
        send: Callable[[str, str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        pause: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create the handler with nothing sent yet (see the class docstring for the arguments)."""
        super().__init__(level=logging.ERROR)
        self._url = url
        self._process_name = process_name
        self._send = send
        self._clock = clock
        self._pause = pause
        self._last_sent: dict[tuple[str, str], float] = {}
        self._skipped: dict[tuple[str, str], int] = {}
        self._window_start: float | None = None
        self._window_count = 0

    def emit(self, record: logging.LogRecord) -> None:
        """Send one record as an alert. Never raises: a failure is printed to stderr."""
        try:
            self._emit(record)
        except Exception as e:
            # An alert that can't be delivered must never raise, and must never be
            # logged (that would produce another alert). stderr still shows it.
            print(f"JoltBot alert not delivered: {e}", file=sys.stderr)

    def _emit(self, record: logging.LogRecord) -> None:
        """Decide whether this record becomes an alert, and post it if so.

        Not posted: records below ERROR, this module's own records, a repeat within
        ``DEDUPE_SECONDS`` (counted and mentioned in the next alert), and anything past
        ``MAX_PER_WINDOW`` in the current window (one "muted" notice is posted instead).
        """
        # The level is checked here too, so this is safe however the handler is attached.
        if record.levelno < self.level or record.name == __name__:
            return
        now = self._clock()
        key = getattr(record, "alert_key", None) or (record.name, record.getMessage())

        last = self._last_sent.get(key)
        if last is not None and now - last < DEDUPE_SECONDS:
            self._skipped[key] = self._skipped.get(key, 0) + 1
            return

        if self._window_start is None or now - self._window_start >= WINDOW_SECONDS:
            self._window_start, self._window_count = now, 0
        self._window_count += 1
        if self._window_count > MAX_PER_WINDOW:
            if self._window_count == MAX_PER_WINDOW + 1:
                self._post(
                    f"🔇 **JoltBot {self._process_name}**: too many errors. Further alerts are muted for "
                    "up to 10 minutes; check the logs."
                )
            return

        if len(self._last_sent) > MAX_REMEMBERED_ALERTS:  # forget old entries so this can't grow forever
            self._last_sent = {k: t for k, t in self._last_sent.items() if now - t < DEDUPE_SECONDS}
        self._last_sent[key] = now
        self._post(format_alert(self._process_name, record, self._skipped.pop(key, 0)))

    def _post(self, content: str) -> None:
        """Post one message, then pause so a burst stays under Discord's webhook rate limit."""
        try:
            (self._send or post_to_webhook)(self._url, content)
        finally:
            self._pause(SECONDS_BETWEEN_POSTS)


class _AlertQueueHandler(QueueHandler):
    """Hands ERROR records to the alert thread without ever blocking the caller."""

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        """Format the record for the alert thread and attach the key it is de-duplicated by."""
        # Remember the message *template* before it's filled in: "Reconnecting in %ss"
        # is one kind of alert, however many different numbers it's logged with.
        key = (record.name, record.msg if isinstance(record.msg, str) else repr(record.msg))
        record = super().prepare(record)  # formats the message, including any traceback
        record.alert_key = key
        return record

    def enqueue(self, record: logging.LogRecord) -> None:
        """Queue the record without waiting. If the queue is full, the record is dropped."""
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            pass  # a flood of errors: the rate limit would drop these anyway


_listener: QueueListener | None = None


def install_alerts(url: str, process_name: str) -> logging.Handler | None:
    """Start the alert thread and return the handler to add to the root logger.

    Args:
        url: ``ALERT_WEBHOOK_URL``; empty means alerts are off.
        process_name: ``"bot"`` or ``"web"``.

    Returns:
        A handler for the root logger, or ``None`` when alerts are off.
    """
    global _listener
    stop_alerts()
    if not url:
        return None
    if not url.startswith("https://"):
        log.warning("ALERT_WEBHOOK_URL must start with https://, so alerts are off.")
        return None
    alert_queue: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=QUEUE_SIZE)
    _listener = QueueListener(alert_queue, WebhookAlertHandler(url, process_name), respect_handler_level=True)
    _listener.start()
    handler = _AlertQueueHandler(alert_queue)
    handler.setLevel(logging.ERROR)
    return handler


def stop_alerts() -> None:
    """Send what is still queued, then stop the alert thread. Safe to call twice."""
    global _listener
    listener, _listener = _listener, None
    if listener is not None:
        try:
            listener.stop()
        except Exception as e:
            print(f"JoltBot alerts didn't shut down cleanly: {e}", file=sys.stderr)


# So a final "the bot is exiting because ..." alert is delivered before the process ends.
atexit.register(stop_alerts)
