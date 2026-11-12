"""Back up the JoltBot database: python -m scripts.backup [--out FILE]

Safe to run while the bot and the dashboard are running. It uses SQLite's
online backup API, which copies a consistent snapshot even mid-write.

The copy is then cleaned before it is kept:
  - dashboard login sessions are removed, so a backup file never contains
    anyone's Discord login token. After a restore, everyone simply logs in again.
  - VACUUM rewrites the file, so the removed rows can't be recovered from it.
  - an integrity check must pass, or no backup file is produced.

Normally run through deploy/backup.sh (nightly, from cron), which also copies
the file out of Docker, compresses it and deletes old ones. A failure is
logged at ERROR, so it reaches ALERT_WEBHOOK_URL when that is set.
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import make_url

from joltbot.core.config import get_config
from joltbot.core.logsetup import setup_logging

log = logging.getLogger("joltbot.backup")


class BackupError(Exception):
    """The backup could not be made. The message says why, in plain words."""


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    size: int
    guilds: int
    tickets: int
    sessions_removed: int


def sqlite_path(database_url: str) -> Path:
    """Find the database file behind ``DATABASE_URL``.

    Args:
        database_url: SQLAlchemy URL from the config.

    Returns:
        Path of the SQLite file.

    Raises:
        BackupError: The database isn't a SQLite file (e.g. Postgres: use ``pg_dump``).
    """
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or url.database in (None, "", ":memory:"):
        raise BackupError("This script backs up SQLite files only. For Postgres, use pg_dump.")
    return Path(url.database)


def make_backup(source: Path, target: Path) -> BackupInfo:
    """Copy the live database to ``target``, without login sessions.

    The copy is built under a temporary name and renamed at the end, so a file
    called like a backup is always a complete, checked one.

    Args:
        source: The live SQLite database.
        target: Where the backup goes. Must not exist yet.

    Returns:
        ``BackupInfo`` describing the finished backup.

    Raises:
        BackupError: Source missing, target already there, not a JoltBot
            database, or the copy failed its integrity check.
    """
    if not source.is_file():
        raise BackupError(f"There is no database at {source}.")
    if target.exists():
        raise BackupError(f"{target} already exists; not overwriting it.")
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    part.unlink(missing_ok=True)

    try:
        with closing(sqlite3.connect(source, timeout=30)) as live, closing(sqlite3.connect(part)) as copy:
            live.backup(copy)
        # isolation_level=None: plain autocommit, which PRAGMA journal_mode and VACUUM need.
        with closing(sqlite3.connect(part, isolation_level=None)) as copy:
            try:
                sessions = copy.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0]
                guilds = copy.execute("SELECT COUNT(*) FROM guilds").fetchone()[0]
                tickets = copy.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
            except sqlite3.OperationalError as e:
                raise BackupError(f"{source} doesn't look like a JoltBot database ({e}).") from None
            copy.execute("DELETE FROM web_sessions")
            copy.execute("PRAGMA journal_mode=DELETE")  # one plain file, no -wal/-shm beside it
            copy.execute("VACUUM")  # rewrite the file so the deleted rows are really gone
            result = copy.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise BackupError(f"The copy failed SQLite's integrity check: {result}")
        os.chmod(part, 0o600)
        part.replace(target)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return BackupInfo(target, target.stat().st_size, guilds, tickets, sessions)


def default_target(source: Path, now: datetime | None = None) -> Path:
    """``backups/joltbot-<UTC time>.db`` next to the live database."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    return source.parent / "backups" / f"joltbot-{stamp}.db"


def main(argv: list[str] | None = None) -> int:
    """CLI entry. Returns the process exit code: 0 on success, 1 if no backup was made."""
    parser = argparse.ArgumentParser(prog="python -m scripts.backup", description="Back up the JoltBot database.")
    parser.add_argument("--out", help="backup file to create (default: backups/joltbot-<time>.db next to the database)")
    args = parser.parse_args(argv)
    config = get_config()
    setup_logging("backup", config.log_level, "", config.log_format, config.alert_webhook_url)
    try:
        source = sqlite_path(config.database_url)
        info = make_backup(source, Path(args.out) if args.out else default_target(source))
    except (BackupError, sqlite3.Error, OSError) as e:
        log.error("Database backup FAILED: %s", e)
        return 1
    log.info(
        "Backup written to %s (%.1f KB): %d server(s), %d ticket(s); %d login session(s) left out",
        info.path,
        info.size / 1024,
        info.guilds,
        info.tickets,
        info.sessions_removed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
