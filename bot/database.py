"""SQLite persistence for tickets.

Ticket lifecycle (``status`` column):

    open     -> applicant and staff are talking in the ticket channel
    closing  -> a decision was made (or the applicant left); transcript/log/delete in progress
    closed   -> done

The partial unique index ``one_active_ticket_per_user`` is what really
prevents duplicate tickets: inserting a second open/closing row for the same
user fails atomically, even if they double-click the button.

``claim()`` is the only way out of ``open``. It's an atomic
``UPDATE ... WHERE status = 'open'``, so if two staff members click Accept and
Deny at the same moment, exactly one of them wins.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    channel_id       INTEGER UNIQUE,
    user_id          INTEGER NOT NULL,
    username         TEXT    NOT NULL,
    status           TEXT    NOT NULL DEFAULT 'open',
    governor_name    TEXT,
    governor_id      TEXT,
    power            TEXT,
    alliance         TEXT,
    extra            TEXT,
    created_at       TEXT    NOT NULL,
    outcome          TEXT,
    handled_by       INTEGER,
    reason           TEXT,
    alliance_role_id INTEGER,
    kicked           INTEGER NOT NULL DEFAULT 0,
    notes            TEXT,
    closed_at        TEXT,
    logged           INTEGER NOT NULL DEFAULT 0,
    channel_gone     INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_ticket_per_user
    ON tickets (guild_id, user_id) WHERE status IN ('open', 'closing');
CREATE INDEX IF NOT EXISTS tickets_status ON tickets (status);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Ticket:
    id: int
    guild_id: int
    channel_id: int | None
    user_id: int
    username: str
    status: str
    governor_name: str | None
    governor_id: str | None
    power: str | None
    alliance: str | None
    extra: str | None
    created_at: str
    outcome: str | None
    handled_by: int | None
    reason: str | None
    alliance_role_id: int | None
    kicked: bool
    notes: str | None
    closed_at: str | None
    logged: bool
    channel_gone: bool

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "Ticket":
        d = dict(row)
        d["kicked"] = bool(d["kicked"])
        d["logged"] = bool(d["logged"])
        d["channel_gone"] = bool(d["channel_gone"])
        return cls(**d)

    @property
    def created_dt(self) -> datetime:
        return datetime.fromisoformat(self.created_at)

    @property
    def closed_dt(self) -> datetime | None:
        return datetime.fromisoformat(self.closed_at) if self.closed_at else None

    @property
    def note_lines(self) -> list[str]:
        return [n for n in (self.notes or "").split("\n") if n]


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected yet")
        return self._conn

    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ---------- creation ----------

    async def reserve_ticket(
        self,
        *,
        guild_id: int,
        user_id: int,
        username: str,
        governor_name: str,
        governor_id: str,
        power: str,
        alliance: str,
        extra: str | None,
    ) -> int | None:
        """Insert an open ticket row (no channel yet). Returns None if the user already has one."""
        try:
            cur = await self.conn.execute(
                """INSERT INTO tickets (guild_id, user_id, username, governor_name, governor_id,
                                        power, alliance, extra, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (guild_id, user_id, username, governor_name, governor_id, power, alliance, extra, utcnow_iso()),
            )
            await self.conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            await self.conn.rollback()
            return None

    async def set_ticket_channel(self, ticket_id: int, channel_id: int) -> None:
        await self.conn.execute("UPDATE tickets SET channel_id = ? WHERE id = ?", (channel_id, ticket_id))
        await self.conn.commit()

    async def delete_ticket(self, ticket_id: int) -> None:
        """Only used to roll back a reservation whose channel couldn't be created."""
        await self.conn.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
        await self.conn.commit()

    # ---------- lookups ----------

    async def _one(self, sql: str, params: tuple) -> Ticket | None:
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return Ticket.from_row(row) if row else None

    async def get(self, ticket_id: int) -> Ticket | None:
        return await self._one("SELECT * FROM tickets WHERE id = ?", (ticket_id,))

    async def get_by_channel(self, channel_id: int) -> Ticket | None:
        return await self._one("SELECT * FROM tickets WHERE channel_id = ?", (channel_id,))

    async def get_active_by_user(self, guild_id: int, user_id: int) -> Ticket | None:
        return await self._one(
            "SELECT * FROM tickets WHERE guild_id = ? AND user_id = ? AND status IN ('open', 'closing')",
            (guild_id, user_id),
        )

    async def list_by_status(self, status: str) -> list[Ticket]:
        async with self.conn.execute("SELECT * FROM tickets WHERE status = ? ORDER BY id", (status,)) as cur:
            rows = await cur.fetchall()
        return [Ticket.from_row(r) for r in rows]

    async def list_closed_needing_delete(self) -> list[Ticket]:
        async with self.conn.execute(
            "SELECT * FROM tickets WHERE status = 'closed' AND logged = 1 AND channel_gone = 0 "
            "AND channel_id IS NOT NULL ORDER BY id"
        ) as cur:
            rows = await cur.fetchall()
        return [Ticket.from_row(r) for r in rows]

    async def open_user_ids(self, guild_id: int) -> set[int]:
        async with self.conn.execute(
            "SELECT user_id FROM tickets WHERE guild_id = ? AND status IN ('open', 'closing')", (guild_id,)
        ) as cur:
            return {r[0] for r in await cur.fetchall()}

    # ---------- state transitions ----------

    async def claim(
        self,
        ticket_id: int,
        *,
        outcome: str,
        handled_by: int | None,
        reason: str | None = None,
        alliance_role_id: int | None = None,
        kicked: bool = False,
    ) -> bool:
        """Atomically move open -> closing and record the decision. False if someone beat us to it."""
        cur = await self.conn.execute(
            """UPDATE tickets
                  SET status = 'closing', outcome = ?, handled_by = ?, reason = ?,
                      alliance_role_id = ?, kicked = ?
                WHERE id = ? AND status = 'open'""",
            (outcome, handled_by, reason, alliance_role_id, int(kicked), ticket_id),
        )
        await self.conn.commit()
        return cur.rowcount == 1

    async def release(self, ticket_id: int) -> None:
        """Undo a claim when the action failed (e.g. role hierarchy problem) so staff can retry."""
        await self.conn.execute(
            """UPDATE tickets
                  SET status = 'open', outcome = NULL, handled_by = NULL, reason = NULL,
                      alliance_role_id = NULL, kicked = 0
                WHERE id = ? AND status = 'closing'""",
            (ticket_id,),
        )
        await self.conn.commit()

    async def set_kicked(self, ticket_id: int, kicked: bool) -> None:
        await self.conn.execute("UPDATE tickets SET kicked = ? WHERE id = ?", (int(kicked), ticket_id))
        await self.conn.commit()

    async def add_note(self, ticket_id: int, note: str) -> None:
        await self.conn.execute(
            "UPDATE tickets SET notes = COALESCE(notes || char(10), '') || ? WHERE id = ?",
            (note, ticket_id),
        )
        await self.conn.commit()

    async def stamp_closed_at(self, ticket_id: int) -> None:
        await self.conn.execute(
            "UPDATE tickets SET closed_at = COALESCE(closed_at, ?) WHERE id = ?", (utcnow_iso(), ticket_id)
        )
        await self.conn.commit()

    async def mark_closed(self, ticket_id: int, *, logged: bool) -> None:
        await self.conn.execute(
            "UPDATE tickets SET status = 'closed', closed_at = COALESCE(closed_at, ?), logged = ? WHERE id = ?",
            (utcnow_iso(), int(logged), ticket_id),
        )
        await self.conn.commit()

    async def mark_channel_gone(self, ticket_id: int) -> None:
        await self.conn.execute("UPDATE tickets SET channel_gone = 1 WHERE id = ?", (ticket_id,))
        await self.conn.commit()

    # ---------- settings ----------

    async def get_setting(self, key: str) -> str | None:
        async with self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.conn.commit()
