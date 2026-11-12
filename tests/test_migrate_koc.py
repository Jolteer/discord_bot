"""The one-time King of Chaos migration, run against a fake copy of the old bot's files."""

import json
import sqlite3

import pytest
from sqlalchemy import select

from joltbot.core.guild_settings import load_module
from joltbot.core.models import BotMeta, PanelMessage, Ticket
from joltbot.core.templating import placeholders_in
from scripts.migrate_koc import MigrationError, migrate

GUILD = "900000000000000001"

OLD_CONFIG = {
    "guild_id": GUILD,
    "roles": {
        "approval": {"King": "900000000000000011", "R4": "900000000000000012", "Mod": "900000000000000013"},
        "unverified": "900000000000000021",
        "member": "900000000000000022",
        "alliances": [],
    },
    "channels": {
        "verify": "900000000000000031",
        "welcome": "900000000000000032",
        "tickets_category": "900000000000000033",
        "ticket_logs": "900000000000000034",
    },
    "ping_on_new_ticket": {"enabled": True, "role_ids": []},
    "tickets": {"delete_delay_seconds": 10, "reupload_attachments": True},
    "allow_administrators": False,
    "assign_unverified_on_startup": False,
    "database_path": "data/tickets.db",
    "messages": {"not_staff": "Only King, R4s, and Mods can do this.", "panel_title": "📝 King of Chaos Verification"},
}

# The old bot's schema (from bot/database.py).
OLD_SCHEMA = """
CREATE TABLE tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, channel_id INTEGER UNIQUE,
    user_id INTEGER NOT NULL, username TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
    governor_name TEXT, governor_id TEXT, power TEXT, alliance TEXT, extra TEXT, created_at TEXT NOT NULL,
    outcome TEXT, handled_by INTEGER, reason TEXT, alliance_role_id INTEGER, kicked INTEGER NOT NULL DEFAULT 0,
    notes TEXT, closed_at TEXT, logged INTEGER NOT NULL DEFAULT 0, channel_gone INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
"""


@pytest.fixture
def old_files(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps(OLD_CONFIG), encoding="utf-8")
    old_db = tmp_path / "tickets.db"
    conn = sqlite3.connect(old_db)
    conn.executescript(OLD_SCHEMA)
    rows = [
        # id, channel, user, status, outcome, created, closed
        (1, 5001, 11, "closed", "accepted", "2026-10-01T10:00:00+00:00", "2026-10-01T11:00:00+00:00"),
        (2, 5002, 12, "open", None, "2026-10-20T10:00:00+00:00", None),
        (3, None, 13, "open", None, "2026-10-21T10:00:00+00:00", None),  # half-created; dropped
    ]
    for tid, ch, uid, status, outcome, created, closed in rows:
        conn.execute(
            "INSERT INTO tickets (id, guild_id, channel_id, user_id, username, status, governor_name, governor_id,"
            " power, alliance, extra, created_at, outcome, closed_at, logged) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                tid,
                int(GUILD),
                ch,
                uid,
                f"user{uid}",
                status,
                "Gov",
                "12345",
                "45M",
                "KOC",
                None,
                created,
                outcome,
                closed,
                int(status == "closed"),
            ),
        )
    conn.execute("INSERT INTO settings VALUES ('verify_panel', '900000000000000031:777')")
    conn.commit()
    conn.close()
    return config, old_db


async def test_migration_seeds_settings_and_tickets(db, old_files):
    config, old_db = old_files
    summary = await migrate(db, config, old_db, guild_name="King of Chaos")
    assert summary.tickets_copied == 2 and summary.tickets_skipped == 1 and summary.open_tickets == 1

    async with db.session() as session:
        state = await load_module(session, int(GUILD), "tickets")
        general = await load_module(session, int(GUILD), "general")
        tickets = (await session.execute(select(Ticket).order_by(Ticket.id))).scalars().all()
        panel = await session.get(PanelMessage, (int(GUILD), "tickets"))
        clear = await session.get(BotMeta, "clear_guild_commands")

    s = state.config
    assert state.enabled is True
    assert s.staff_role_ids == [900000000000000011, 900000000000000012, 900000000000000013]
    assert s.allow_administrators is False
    assert s.pending_role_id == 900000000000000021 and s.accept_role_ids == [900000000000000022]
    assert s.panel_channel_id == 900000000000000031 and s.log_channel_id == 900000000000000034
    assert s.messages.panel_title == "📝 King of Chaos Verification"  # their custom text
    assert s.messages.not_staff == "Only King, R4s, and Mods can do this."
    assert [q.label for q in s.form_questions][0] == "Governor name"
    assert s.setup_problems() == []
    # Old placeholder names were converted to the new ones.
    assert "role_line" in placeholders_in(s.messages.welcome_dm)
    assert "{guild}" not in s.messages.panel_description
    assert general.config.embed_color == "#C9A227"

    assert [t.id for t in tickets] == [1, 2]  # same ticket numbers
    assert tickets[0].answers[0] == {"label": "Governor name", "value": "Gov"}
    assert tickets[1].status == "open" and tickets[1].channel_id == 5002
    assert (panel.channel_id, panel.message_id) == (900000000000000031, 777)
    assert clear.value == GUILD


async def test_migration_refuses_to_run_twice(db, old_files):
    config, old_db = old_files
    await migrate(db, config, old_db, guild_name="KOC")
    with pytest.raises(MigrationError):
        await migrate(db, config, old_db, guild_name="KOC")
    # --force is safe: tickets already copied are skipped, not duplicated.
    summary = await migrate(db, config, old_db, guild_name="KOC", force=True)
    assert summary.tickets_copied == 0


async def test_dry_run_saves_nothing(db, old_files):
    config, old_db = old_files
    summary = await migrate(db, config, old_db, guild_name="KOC", dry_run=True)
    assert summary.tickets_copied == 2
    async with db.session() as session:
        assert (await session.execute(select(Ticket))).first() is None
        assert (await load_module(session, int(GUILD), "tickets")).version == 0
