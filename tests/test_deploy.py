"""Deployment plumbing: /health, settings from env vars, logging, and the CSP switch."""

import json
import logging
from datetime import datetime

import httpx
import pytest
from pydantic import ValidationError

from joltbot.core.config import AppConfig
from joltbot.core.logsetup import HideLoginCode, JsonFormatter, setup_logging
from joltbot.web import app as app_module
from joltbot.web.app import TAILWIND_CDN, create_app

from .test_web_access import FakeDiscord, make_config


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000")


# ------------------------------------------------------------------ /health


async def test_health_is_ok_without_login(db):
    app = create_app(make_config(), db, FakeDiscord(), check_schema=False)
    async with client_for(app) as client:
        r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_health_reports_a_broken_database(db):
    class BrokenDb:
        def session(self):
            raise RuntimeError("database is gone")

    app = create_app(make_config(), db, FakeDiscord(), check_schema=False)
    app.state.db = BrokenDb()
    async with client_for(app) as client:
        r = await client.get("/health")
    assert r.status_code == 503
    assert r.json() == {"status": "error"}  # no internal details leaked


# ------------------------------------------------------------------ CSP


@pytest.mark.parametrize("built", [True, False])
async def test_cdn_allowed_only_until_css_is_built(db, monkeypatch, built):
    monkeypatch.setattr(app_module, "css_built", lambda: built)
    app = create_app(make_config(), db, FakeDiscord(), check_schema=False)
    async with client_for(app) as client:
        csp = (await client.get("/health")).headers["content-security-policy"]
    assert (TAILWIND_CDN in csp) is (not built)


# ------------------------------------------------------------------ settings from env vars


def test_settings_come_from_env_vars(monkeypatch):
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "9000")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("LOG_DIR", "")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    config = AppConfig(_env_file=None)
    assert (config.host, config.port, config.log_level, config.log_dir, config.forwarded_allow_ips) == (
        "0.0.0.0",
        9000,
        "DEBUG",
        "",
        "*",
    )


def test_defaults_match_local_development(monkeypatch):
    for name in ("HOST", "PORT", "LOG_LEVEL", "LOG_DIR", "FORWARDED_ALLOW_IPS"):
        monkeypatch.delenv(name, raising=False)
    config = AppConfig(_env_file=None)
    assert (config.host, config.port, config.log_level, config.log_dir) == ("127.0.0.1", 8000, "INFO", "logs")


def test_log_format_defaults_to_text_and_ignores_case(monkeypatch):
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    assert AppConfig(_env_file=None).log_format == "text"
    monkeypatch.setenv("LOG_FORMAT", "JSON")
    assert AppConfig(_env_file=None).log_format == "json"


@pytest.mark.parametrize("name,value", [("PORT", "0"), ("PORT", "http"), ("LOG_LEVEL", "LOUD"), ("LOG_FORMAT", "xml")])
def test_bad_values_are_rejected(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        AppConfig(_env_file=None)


# ------------------------------------------------------------------ logging


@pytest.fixture
def restore_root_logger():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers[:], root.level = handlers, level


def test_empty_log_dir_means_console_only(tmp_path, monkeypatch, restore_root_logger):
    monkeypatch.chdir(tmp_path)
    setup_logging("web", "WARNING", "")
    root = logging.getLogger()
    assert [type(h) for h in root.handlers] == [logging.StreamHandler]
    assert root.level == logging.WARNING
    assert not (tmp_path / "logs").exists()


def test_log_dir_gets_a_file(tmp_path, restore_root_logger):
    setup_logging("bot", "INFO", str(tmp_path / "logs"))
    logging.getLogger("joltbot.test").info("hello")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "hello" in (tmp_path / "logs" / "bot.log").read_text(encoding="utf-8")


def test_login_code_is_hidden_from_access_logs():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", "/auth/callback?code=SECRET&state=X", "1.1", 303),
        None,
    )
    HideLoginCode().filter(record)
    message = record.getMessage()
    assert "SECRET" not in message and "/auth/callback?[hidden]" in message


def test_other_urls_are_logged_unchanged():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", "/servers?page=2", "1.1", 200),
        None,
    )
    HideLoginCode().filter(record)
    assert "/servers?page=2" in record.getMessage()


def test_text_logs_look_the_same_as_before(tmp_path, restore_root_logger):
    setup_logging("bot", "INFO", str(tmp_path / "logs"))
    logging.getLogger("joltbot.test").warning("Guild %s: %s", 5, "something", extra={"guild_id": 5})
    for handler in logging.getLogger().handlers:
        handler.flush()
    line = (tmp_path / "logs" / "bot.log").read_text(encoding="utf-8").strip()
    assert line.endswith("WARNING  joltbot.test: Guild 5: something")


def test_json_logs_are_one_object_per_line_with_extra_fields(tmp_path, restore_root_logger):
    setup_logging("bot", "INFO", str(tmp_path / "logs"), "json")
    log = logging.getLogger("joltbot.test")
    log.warning(
        "Guild %s: %s", 5, "something\nwith a line break", extra={"guild_id": 5, "issue": "tickets.log_channel"}
    )
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("It failed")
    for handler in logging.getLogger().handlers:
        handler.flush()

    lines = (tmp_path / "logs" / "bot.log").read_text(encoding="utf-8").splitlines()
    first, second = (json.loads(line) for line in lines)  # exactly two lines, each valid JSON
    assert first["level"] == "WARNING" and first["logger"] == "joltbot.test"
    assert first["message"] == "Guild 5: something\nwith a line break"
    assert first["guild_id"] == 5 and first["issue"] == "tickets.log_channel"
    assert datetime.fromisoformat(first["ts"]).tzinfo is not None
    assert second["level"] == "ERROR" and "ValueError: boom" in second["exception"]
    assert "exception" not in first


def test_json_logs_survive_values_json_cannot_encode():
    record = logging.LogRecord("joltbot.test", logging.INFO, __file__, 1, "hi", (), None)
    record.thing = object()
    assert json.loads(JsonFormatter().format(record))["thing"].startswith("<object object")


def test_json_logs_drop_uvicorns_colour_codes():
    record = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, "Uvicorn running", (), None)
    record.color_message = "Uvicorn running \x1b[1m...\x1b[0m"
    assert "color_message" not in json.loads(JsonFormatter().format(record))
