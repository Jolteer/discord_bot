"""The Alembic migrations must build exactly the schema described in models.py.

If this fails after you edit models.py, run:
    alembic revision --autogenerate -m "describe your change"
and review the new file in migrations/versions/.
"""
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from joltbot.core.models import Base

ROOT = Path(__file__).resolve().parents[1]


def test_migrations_match_models(tmp_path):
    db_file = (tmp_path / "migrated.db").as_posix()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logging"] = False
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_file}")
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()
    assert diff == [], f"Models and migrations differ: {diff}"
