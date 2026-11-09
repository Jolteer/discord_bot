"""Startup check: is the database schema up to date?

Both processes call this before doing anything else, so a forgotten
`alembic upgrade head` gives one clear message instead of confusing
"no such table" errors later.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

from .db import Database

ROOT = Path(__file__).resolve().parents[2]


def head_revision() -> str | None:
    """Return the Alembic head revision id from ``migrations/``.

    Returns:
        Head revision string, or ``None`` if no scripts exist.
    """
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return ScriptDirectory.from_config(config).get_current_head()


async def schema_problem(db: Database) -> str | None:
    """Check whether the live DB matches Alembic head.

    Args:
        db: Database whose engine should be inspected.

    Returns:
        ``None`` if current; otherwise a message telling the operator to run
        ``alembic upgrade head``.
    """
    fix = "Run `alembic upgrade head` in the project folder (with the venv active), then start again."
    try:
        async with db.engine.connect() as conn:
            current = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one_or_none()
    except (OperationalError, ProgrammingError):
        return f"The database has no tables yet. {fix}"
    if current != head_revision():
        return f"The database schema is out of date (at {current}, code expects {head_revision()}). {fix}"
    return None
