"""Operator alerts: which log lines reach the webhook, and the limits that stop a flood.

Nothing here touches the network: the function that posts to Discord is replaced.
"""

import json
import logging
import urllib.error

import pytest

from joltbot.bot import __main__ as bot_main
from joltbot.core import alerts
from joltbot.core.alerts import (
    DEDUPE_SECONDS,
    MAX_CONTENT,
    MAX_PER_WINDOW,
    WINDOW_SECONDS,
    AlertError,
    WebhookAlertHandler,
    format_alert,
    post_to_webhook,
)
from joltbot.core.config import AppConfig
from joltbot.core.logsetup import setup_logging

URL = "https://discord.example/api/webhooks/1/secret-token"


class Harness:
    """A WebhookAlertHandler with a fake clock; ``sent`` lists every message posted."""

    def __init__(self):
        self.now = 1000.0
        self.sent: list[str] = []
        self.fail = False
        self.handler = WebhookAlertHandler(URL, "bot", send=self._send, clock=lambda: self.now, pause=lambda _s: None)

    def _send(self, url, content):
        assert url == URL
        if self.fail:
            raise AlertError("Discord answered 500")
        self.sent.append(content)

    def log(self, message, *args, level=logging.ERROR, name="joltbot.test"):
        record = logging.LogRecord(name, level, __file__, 1, message, args, None)
        record.alert_key = (name, message)  # what the queue handler adds in real use
        self.handler.handle(record)


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    alerts.stop_alerts()
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers[:], root.level = handlers, level


def test_an_error_becomes_one_alert():
    h = Harness()
    h.log("Finalizing ticket #%s failed", 12)
    assert h.sent == ["🚨 **JoltBot bot** · ERROR · `joltbot.test`\n```\nFinalizing ticket #12 failed\n```"]


def test_warnings_and_info_are_not_alerts():
    h = Harness()
    h.log("just so you know", level=logging.WARNING)
    h.log("all good", level=logging.INFO)
    assert h.sent == []


def test_the_same_error_is_sent_once_per_ten_minutes():
    h = Harness()
    for seconds in (1.5, 3.0, 6.0):  # same template, different numbers: still the same alert
        h.log("Attempting a reconnect in %.2fs", seconds, name="discord.client")
    assert len(h.sent) == 1

    h.now += DEDUPE_SECONDS + 1
    h.log("Attempting a reconnect in %.2fs", 12.0, name="discord.client")
    assert len(h.sent) == 2 and "+2 more like this since the last alert" in h.sent[1]


def test_different_errors_are_separate_alerts():
    h = Harness()
    h.log("first problem")
    h.log("second problem")
    h.log("first problem", name="joltbot.other")
    assert len(h.sent) == 3


def test_a_flood_is_muted_after_ten_alerts_until_the_window_ends():
    h = Harness()
    for i in range(MAX_PER_WINDOW + 5):
        h.log(f"distinct problem {i}")
    assert len(h.sent) == MAX_PER_WINDOW + 1  # ten alerts, then one notice
    assert "muted" in h.sent[-1] and "distinct problem" not in h.sent[-1]

    h.now += WINDOW_SECONDS + 1
    h.log("a new problem")
    assert h.sent[-1].endswith("```\na new problem\n```")


def test_a_failed_delivery_never_raises(capsys):
    h = Harness()
    h.fail = True
    h.log("something broke")  # must not raise
    assert "alert not delivered" in capsys.readouterr().err
    h.fail = False
    h.log("something else broke")
    assert len(h.sent) == 1


def test_alerts_fit_discords_limit_and_keep_the_end_of_a_traceback():
    record = logging.LogRecord(
        "joltbot.test", logging.ERROR, __file__, 1, "%s", ("start " + "x" * 5000 + " ValueError: the real cause",), None
    )
    text = format_alert("web", record)
    assert len(text) <= MAX_CONTENT
    assert (
        text.startswith("🚨 **JoltBot web**") and "start " in text and text.endswith("ValueError: the real cause\n```")
    )


def test_log_text_cannot_break_out_of_the_code_block():
    record = logging.LogRecord("joltbot.test", logging.ERROR, __file__, 1, "evil ``` @everyone", (), None)
    assert format_alert("bot", record).count("```") == 2


def test_only_https_urls_are_ever_opened():
    with pytest.raises(AlertError, match="must start with https://"):
        post_to_webhook("file:///etc/passwd", "hi")


def test_the_request_sent_to_discord(monkeypatch):
    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout):
        seen.append((request, timeout))
        return Response()

    monkeypatch.setattr(alerts.urllib.request, "urlopen", fake_urlopen)
    post_to_webhook(URL, "hello @everyone " + "x" * 3000)

    request, timeout = seen[0]
    body = json.loads(request.data)
    assert request.full_url == URL and request.get_method() == "POST" and timeout == 5
    assert request.get_header("Content-type") == "application/json"
    assert len(body["content"]) == MAX_CONTENT
    assert body["allowed_mentions"] == {"parse": []}  # nothing in a log line can ping anyone


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (urllib.error.HTTPError(URL, 404, "Not Found", None, None), "Discord answered 404"),
        (urllib.error.URLError("no route to host"), "couldn't reach Discord (URLError)"),
        (TimeoutError("timed out"), "couldn't reach Discord (TimeoutError)"),
    ],
)
def test_delivery_problems_are_reported_without_the_url(monkeypatch, failure, message):
    def fake_urlopen(request, timeout):
        raise failure

    monkeypatch.setattr(alerts.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(AlertError) as raised:
        post_to_webhook(URL, "hi")
    assert str(raised.value) == message and "secret-token" not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__  # no URL in a chained traceback


# ------------------------------------------------------------------ wiring


def test_alerts_are_off_without_a_url(restore_logging):
    setup_logging("bot", "INFO", "", "text", "")
    assert [type(h) for h in logging.getLogger().handlers] == [logging.StreamHandler]


def test_a_url_that_is_not_https_switches_alerts_off_without_printing_it(restore_logging, capsys):
    setup_logging("bot", "INFO", "", "text", "http://insecure.example/secret-token")
    assert len(logging.getLogger().handlers) == 1
    out = capsys.readouterr().out
    assert "alerts are off" in out and "secret-token" not in out


def test_errors_logged_anywhere_reach_the_webhook_with_their_traceback(restore_logging, monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "post_to_webhook", lambda url, content: sent.append((url, content)))
    monkeypatch.setattr(alerts, "SECONDS_BETWEEN_POSTS", 0)
    setup_logging("web", "INFO", "", "text", URL)

    logging.getLogger("joltbot.web.app").info("not an alert")
    try:
        raise RuntimeError("database is locked")
    except RuntimeError:
        logging.getLogger("joltbot.web.app").exception("Saving settings for guild %s failed", 5)
    alerts.stop_alerts()  # waits until the alert thread has sent everything queued

    assert len(sent) == 1
    url, content = sent[0]
    assert url == URL
    assert "**JoltBot web** · ERROR · `joltbot.web.app`" in content
    assert "Saving settings for guild 5 failed" in content and "RuntimeError: database is locked" in content


def test_the_webhook_url_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    assert AppConfig(_env_file=None).alert_webhook_url == ""
    monkeypatch.setenv("ALERT_WEBHOOK_URL", f"  {URL}  ")
    assert AppConfig(_env_file=None).alert_webhook_url == URL


# ------------------------------------------------------------------ python -m joltbot.bot --test-alert


def test_test_alert_command_reports_success(monkeypatch, capsys):
    posted = []
    monkeypatch.setattr(bot_main, "post_to_webhook", lambda url, content: posted.append(url))
    assert bot_main.send_test_alert(URL) == 0
    assert posted == [URL] and "Test alert sent" in capsys.readouterr().out


def test_test_alert_command_explains_a_failure_without_printing_the_url(monkeypatch, capsys):
    def refuse(url, content):
        raise AlertError("Discord answered 404")

    monkeypatch.setattr(bot_main, "post_to_webhook", refuse)
    assert bot_main.send_test_alert(URL) == 1
    out = capsys.readouterr().out
    assert "Discord answered 404" in out and "secret-token" not in out


def test_test_alert_command_when_no_url_is_set(capsys):
    assert bot_main.send_test_alert("") == 1
    assert "ALERT_WEBHOOK_URL is not set" in capsys.readouterr().out
