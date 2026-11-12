"""scripts/backup.py: a consistent copy of the database with no login tokens in it."""

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from joltbot.core.config import AppConfig
from joltbot.core.models import Guild, Ticket, WebSession
from joltbot.core.types import utcnow
from scripts import backup
from scripts.backup import BackupError, default_target, make_backup, sqlite_path

from .conftest import GUILD_A

TOKEN = "discord-oauth-access-token-that-must-not-leak"


def _db_path(db):
    return sqlite_path(str(db.engine.url))


@pytest.fixture
async def live(db, two_guilds):
    """A database with a ticket and one logged-in dashboard user. Returns its file path."""
    async with db.session() as session, session.begin():
        session.add(Ticket(guild_id=GUILD_A, user_id=7, username="alice", status="open", answers=[]))
        session.add(
            WebSession(
                id="hashed-session-id",
                user_id=1001,
                username="alice",
                access_token=TOKEN,
                token_expires_at=utcnow() + timedelta(days=7),
                csrf_token="csrf",
                expires_at=utcnow() + timedelta(days=7),
            )
        )
    return _db_path(db)


def _count(path, table):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


async def test_backup_keeps_the_data_and_drops_login_sessions(live, tmp_path):
    target = tmp_path / "out" / "backup.db"
    info = make_backup(live, target)

    assert info.path == target and info.size == target.stat().st_size > 0
    assert (info.guilds, info.tickets, info.sessions_removed) == (2, 1, 1)
    assert _count(target, "guilds") == 2 and _count(target, "tickets") == 1
    assert _count(target, "web_sessions") == 0
    # Not just deleted rows: the token must not be recoverable from the file at all.
    assert TOKEN.encode() not in target.read_bytes()
    assert TOKEN.encode() in live.read_bytes() or _count(live, "web_sessions") == 1  # the live DB is untouched


async def test_backup_is_one_plain_healthy_file(live, tmp_path):
    target = tmp_path / "backup.db"
    make_backup(live, target)
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("backup")) == ["backup.db"]
    with closing(sqlite3.connect(target)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


async def test_the_live_database_keeps_working_and_keeps_its_sessions(db, live, tmp_path):
    make_backup(live, tmp_path / "backup.db")
    async with db.session() as session:
        assert await session.get(WebSession, "hashed-session-id") is not None
        assert await session.get(Guild, GUILD_A) is not None


async def test_an_existing_backup_is_never_overwritten(live, tmp_path):
    target = tmp_path / "backup.db"
    target.write_text("yesterday's backup")
    with pytest.raises(BackupError, match="already exists"):
        make_backup(live, target)
    assert target.read_text() == "yesterday's backup"


def test_a_missing_database_is_an_error_not_an_empty_backup(tmp_path):
    with pytest.raises(BackupError, match="There is no database"):
        make_backup(tmp_path / "nope.db", tmp_path / "backup.db")
    assert list(tmp_path.iterdir()) == []  # sqlite must not have created an empty file either


def test_a_file_that_is_not_a_joltbot_database_leaves_nothing_behind(tmp_path):
    other = tmp_path / "other.db"
    with closing(sqlite3.connect(other)) as conn:
        conn.execute("CREATE TABLE notes (text TEXT)")
    with pytest.raises(BackupError, match="doesn't look like a JoltBot database"):
        make_backup(other, tmp_path / "backup.db")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["other.db"]  # no half-made .part file


def test_only_sqlite_is_supported():
    assert sqlite_path("sqlite+aiosqlite:///data/joltbot.db").as_posix() == "data/joltbot.db"
    for url in ("postgresql+asyncpg://user:pw@localhost/joltbot", "sqlite+aiosqlite:///:memory:"):
        with pytest.raises(BackupError, match="SQLite files only"):
            sqlite_path(url)


def test_default_backup_name_sits_next_to_the_database(tmp_path):
    when = datetime(2026, 11, 12, 3, 15, 0, tzinfo=timezone.utc)
    assert default_target(tmp_path / "joltbot.db", when) == tmp_path / "backups" / "joltbot-20261112-031500.db"


# ------------------------------------------------------------------ python -m scripts.backup


@pytest.fixture
def restore_logging():
    import logging

    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:], root.level = handlers, level


async def test_command_writes_the_backup_and_exits_0(live, tmp_path, monkeypatch, restore_logging, capsys):
    monkeypatch.setattr(
        backup, "get_config", lambda: AppConfig(_env_file=None, database_url=f"sqlite+aiosqlite:///{live.as_posix()}")
    )
    target = tmp_path / "cli.db"
    assert backup.main(["--out", str(target)]) == 0
    assert _count(target, "tickets") == 1
    assert "1 login session(s) left out" in capsys.readouterr().out


def test_command_reports_failure_as_an_error_and_exits_1(tmp_path, monkeypatch, restore_logging, capsys):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'missing.db').as_posix()}"
    monkeypatch.setattr(backup, "get_config", lambda: AppConfig(_env_file=None, database_url=url))
    assert backup.main(["--out", str(tmp_path / "cli.db")]) == 1
    out = capsys.readouterr().out
    assert "ERROR" in out and "Database backup FAILED" in out  # ERROR level is what reaches the alert webhook
