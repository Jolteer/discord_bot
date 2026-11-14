"""One-time migration: King of Chaos (old KOC Gatekeeper bot) -> JoltBot database.

Reads the old bot's config.json and data/tickets.db and seeds the new JoltBot
database so the King of Chaos server keeps working exactly as before:
  - the ticket module, switched on, with the same roles, channels and options
  - the same Rise of Kingdoms wording (now stored as that server's own messages)
  - the governor application questions
  - every old ticket (open ones too, with the same ticket numbers)
  - where the verify panel was posted
  - a note telling the bot to remove the old per-server slash commands

Run it ONCE, on the VPS, with the old bot stopped and after `alembic upgrade head`:

    python -m scripts.migrate_koc --config ../rok-ticket-bot/config.json \
        --old-db ../rok-ticket-bot/data/tickets.db --dry-run
    (check the summary, then run again without --dry-run)

All the server IDs come from config.json; nothing is hard-coded here. The
old database is backed up next to itself before anything is read.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from joltbot.core.config import get_config
from joltbot.core.db import Database
from joltbot.core.guild_settings import Actor, load_module, save_module
from joltbot.core.guilds import GuildInfo, ensure_guild
from joltbot.core.models import CLEAR_GUILD_COMMANDS_KEY, AuditLog, BotMeta, PanelMessage, Ticket
from joltbot.core.schema import schema_problem
from joltbot.modules.general.settings import GeneralSettings
from joltbot.modules.tickets.settings import ExtraRoleChoice, FormQuestion, TicketMessages, TicketSettings

ACTOR = Actor(user_id=None, name="KOC migration", source="migration")
KOC_GOLD = "#C9A227"  # the old bot's embed colour

# The old bot's built-in (Rise of Kingdoms) wording. config.json "messages" override these.
OLD_DEFAULT_MESSAGES = {
    "not_staff": "Only King, R4s, and Mods can do this.",
    "already_verified": "You're already verified. Welcome back! 🎉",
    "panel_title": "📝 Kingdom Verification",
    "panel_description": (
        "Welcome to **{guild}**!\n\n"
        "To get access to the server, click **Open Application** below and fill in your "
        "governor details. A private ticket will open where you'll upload a screenshot of "
        "your governor profile. A member of leadership will review it shortly."
    ),
    "ticket_intro": (
        "Thanks for applying, {user}! 👋\n\n"
        "**Next step:** upload a screenshot of your **governor profile** in this channel "
        "(tap your avatar in-game → screenshot the profile page).\n"
        "Leadership will review your application here."
    ),
    "welcome_dm": (
        "✅ Your application to **{guild}** was accepted! Welcome aboard, governor.\n"
        "{alliance_line}"
        "You now have access to the server chats."
    ),
    "deny_dm": "❌ Your application to **{guild}** was not accepted.\n\n**Reason:** {reason}",
    "deny_dm_kicked_suffix": "\n\nYou have been removed from the server.",
    "deny_dm_kept_suffix": "\n\nYou can open a new application from the verify channel if your situation changes.",
}
# Texts the old bot had hard-coded in its code; they're per-server messages now.
OLD_HARDCODED_MESSAGES = {
    "panel_button_label": "Open Application",
    "form_title": "Kingdom Application",
    "ticket_opened": (
        "✅ Your application ticket is open: {channel}\nHead there and upload a screenshot of your governor profile."
    ),
    "deny_reason_hint": "e.g. Power below the kingdom's minimum for migration.",
}
# The old application form, as JoltBot form questions.
GOVERNOR_QUESTIONS = [
    FormQuestion(label="Governor name", max_length=64),
    FormQuestion(label="Governor ID", placeholder="Numbers only — shown on your governor profile", max_length=24),
    FormQuestion(label="Current power", placeholder="e.g. 45M or 45,000,000", max_length=32),
    FormQuestion(label="Alliance you're in or applying to", max_length=64),
    FormQuestion(
        label="Anything else? (optional)",
        placeholder="Migrating from another kingdom? Main/farm account? Anything leadership should know.",
        required=False,
        long=True,
        max_length=1000,
    ),
]
# Old ticket columns -> the labels shown in old ticket embeds.
OLD_ANSWER_COLUMNS = [
    ("governor_name", "Governor name"),
    ("governor_id", "Governor ID"),
    ("power", "Current power"),
    ("alliance", "Alliance (current / applying to)"),
    ("extra", "Anything else"),
]
PLACEHOLDER_RENAMES = {"{guild}": "{server}", "{alliance_line}": "{role_line}", "{alliance}": "{role}"}


class MigrationError(Exception):
    """The migration can't go ahead. The message says why, in plain words."""

    pass


@dataclass
class Summary:
    """What a run did (or, on a dry run, would have done), for printing."""

    guild_id: int = 0
    tickets_copied: int = 0
    tickets_skipped: int = 0
    open_tickets: int = 0
    panel: str = "none"
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        """The summary as aligned lines of text."""
        lines = [
            f"Guild:            {self.guild_id}",
            f"Tickets copied:   {self.tickets_copied} ({self.open_tickets} still open/closing)",
            f"Tickets skipped:  {self.tickets_skipped}",
            f"Panel message:    {self.panel}",
        ]
        return "\n".join(lines + [f"Note: {n}" for n in self.notes])


def _id(value: Any) -> int | None:
    """A Discord ID from the old config, or ``None`` for an empty or zero value."""
    if value in (None, "", 0, "0"):
        return None
    return int(str(value).strip())


def _rename_placeholders(text: str) -> str:
    """Rewrite the old bot's placeholder names as JoltBot's (``{guild}`` becomes ``{server}``, ...)."""
    for old, new in PLACEHOLDER_RENAMES.items():
        text = text.replace(old, new)
    return text


def build_settings(raw: dict[str, Any]) -> TicketSettings:
    """Turn the old config.json into JoltBot ticket settings (validated)."""
    roles = raw.get("roles") or {}
    channels = raw.get("channels") or {}
    ping = raw.get("ping_on_new_ticket") or {}
    tickets = raw.get("tickets") or {}

    messages = {**OLD_DEFAULT_MESSAGES}
    for key, value in (raw.get("messages") or {}).items():
        if key in messages and isinstance(value, str) and value.strip():
            messages[key] = value
    messages = {k: _rename_placeholders(v) for k, v in messages.items()}
    messages.update(OLD_HARDCODED_MESSAGES)

    member_role = _id(roles.get("member"))
    if member_role is None:
        raise MigrationError("config.json has no roles.member")
    return TicketSettings(
        staff_role_ids=[_id(v) for v in (roles.get("approval") or {}).values() if _id(v)],
        allow_administrators=bool(raw.get("allow_administrators", False)),
        panel_channel_id=_id(channels.get("verify")),
        welcome_channel_id=_id(channels.get("welcome")),
        ticket_category_id=_id(channels.get("tickets_category")),
        log_channel_id=_id(channels.get("ticket_logs")),
        pending_role_id=_id(roles.get("unverified")),
        accept_role_ids=[member_role],
        extra_role_choices=[
            ExtraRoleChoice(label=a["label"], role_id=_id(a["role_id"]), emoji=a.get("emoji") or None)
            for a in roles.get("alliances") or []
        ],
        ping_enabled=bool(ping.get("enabled", True)),
        ping_role_ids=[_id(v) for v in ping.get("role_ids") or [] if _id(v)],
        form_questions=GOVERNOR_QUESTIONS,
        require_attachment=True,  # the old bot always warned when no screenshot was uploaded
        delete_delay_seconds=int(tickets.get("delete_delay_seconds", 10)),
        reupload_attachments=bool(tickets.get("reupload_attachments", True)),
        assign_pending_on_startup=bool(raw.get("assign_unverified_on_startup", False)),
        messages=TicketMessages(**messages),
    )


def _parse_time(value: str | None) -> datetime | None:
    """Read a timestamp from the old database. One without a timezone was stored as UTC."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def read_old_db(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    """Returns (old ticket rows, old 'verify_panel' setting)."""
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM tickets ORDER BY id")]
        panel_row = conn.execute("SELECT value FROM settings WHERE key = 'verify_panel'").fetchone()
    finally:
        conn.close()
    return rows, panel_row[0] if panel_row else None


def backup_old_db(path: Path) -> Path:
    """Copy the old DB using SQLite's backup API (safe even with WAL files around)."""
    target = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    source = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    dest = sqlite3.connect(target)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()
    return target


def convert_ticket(row: dict[str, Any], guild_id: int) -> Ticket:
    """Turn one row of the old tickets table into a JoltBot ``Ticket``.

    Args:
        row: The old row, as a dict.
        guild_id: The server every old ticket belongs to (the old bot served one).

    Returns:
        An unsaved ``Ticket`` with the same number, answers and outcome.
    """
    answers = [{"label": label, "value": row.get(col) or ""} for col, label in OLD_ANSWER_COLUMNS if row.get(col)]
    return Ticket(
        id=row["id"],
        guild_id=guild_id,
        channel_id=row.get("channel_id"),
        user_id=row["user_id"],
        username=row["username"],
        status=row["status"],
        answers=answers,
        created_at=_parse_time(row["created_at"]),
        outcome=row.get("outcome"),
        handled_by=row.get("handled_by"),
        reason=row.get("reason"),
        extra_role_id=row.get("alliance_role_id"),
        kicked=bool(row.get("kicked")),
        notes=row.get("notes"),
        closed_at=_parse_time(row.get("closed_at")),
        claimed_at=None,
        logged=bool(row.get("logged")),
        channel_gone=bool(row.get("channel_gone")),
    )


async def migrate(
    db: Database,
    config_path: Path,
    old_db_path: Path | None,
    *,
    guild_name: str,
    dry_run: bool = False,
    force: bool = False,
) -> Summary:
    """Copy the old bot's settings and tickets into the JoltBot database, in one transaction.

    Args:
        db: The JoltBot database, already at the current schema.
        config_path: The old bot's ``config.json``.
        old_db_path: The old bot's ``tickets.db``, or ``None`` to bring over settings only.
        guild_name: Name stored for the server until the bot refreshes it.
        dry_run: True does everything, then rolls it all back.
        force: True runs again on a server that was already migrated.

    Returns:
        What was copied and skipped.

    Raises:
        MigrationError: The config is incomplete, the old database is missing, or
            the server was already migrated and ``force`` is False.
    """
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    guild_id = _id(raw.get("guild_id"))
    if guild_id is None:
        raise MigrationError("config.json has no guild_id")
    settings = build_settings(raw)
    summary = Summary(guild_id=guild_id)

    old_rows: list[dict[str, Any]] = []
    old_panel: str | None = None
    if old_db_path is not None:
        if not old_db_path.exists():
            raise MigrationError(f"Old database not found: {old_db_path}")
        old_rows, old_panel = read_old_db(old_db_path)

    session = db.session()
    try:
        async with session.begin():
            already = (
                await session.execute(
                    select(AuditLog.id).where(AuditLog.guild_id == guild_id, AuditLog.source == "migration")
                )
            ).first()
            if already and not force:
                raise MigrationError("This server was already migrated. Use --force to run it again.")

            await ensure_guild(session, GuildInfo(guild_id, guild_name))
            current = await load_module(session, guild_id, "tickets")
            await save_module(
                session,
                guild_id,
                "tickets",
                enabled=True,
                config=settings,
                expected_version=current.version,
                actor=ACTOR,
            )
            general = await load_module(session, guild_id, "general")
            await save_module(
                session,
                guild_id,
                "general",
                enabled=True,
                config=GeneralSettings(embed_color=KOC_GOLD),
                expected_version=general.version,
                actor=ACTOR,
            )
            # An audit row even if nothing changed (re-run), so "already migrated" is detected.
            session.add(
                AuditLog(
                    guild_id=guild_id,
                    actor_id=None,
                    actor_name=ACTOR.name,
                    source="migration",
                    action="migration.koc",
                    module=None,
                    changes={"tickets_copied": [None, len(old_rows)]},
                )
            )

            for row in old_rows:
                if row["status"] == "open" and row.get("channel_id") is None:
                    summary.tickets_skipped += 1  # half-created reservation; nothing to keep
                    continue
                ticket = convert_ticket(row, guild_id)
                existing = await session.get(Ticket, row["id"])
                if existing is not None:
                    if (
                        existing.guild_id == guild_id
                        and existing.user_id == ticket.user_id
                        and existing.created_at == ticket.created_at
                    ):
                        summary.tickets_skipped += 1  # copied by an earlier run
                        continue
                    # That number is already used by a ticket in another server: give it a new one.
                    ticket.id = None
                    summary.notes.append(f"Old ticket #{row['id']} got a new number (#{row['id']} was taken).")
                session.add(ticket)
                summary.tickets_copied += 1
                summary.open_tickets += row["status"] in ("open", "closing")

            if old_panel:
                channel_id, message_id = (int(x) for x in old_panel.split(":"))
                await session.merge(
                    PanelMessage(guild_id=guild_id, module="tickets", channel_id=channel_id, message_id=message_id)
                )
                summary.panel = f"channel {channel_id}, message {message_id}"

            # Ask the bot to remove the old per-server slash commands (/setup-verify etc.).
            meta = await session.get(BotMeta, CLEAR_GUILD_COMMANDS_KEY)
            ids = set(filter(None, meta.value.split(","))) if meta else set()
            ids.add(str(guild_id))
            await session.merge(BotMeta(key=CLEAR_GUILD_COMMANDS_KEY, value=",".join(sorted(ids))))

            if dry_run:
                summary.notes.append("Dry run: nothing was saved.")
                raise _DryRun()
    except _DryRun:
        pass
    finally:
        await session.close()
    return summary


class _DryRun(Exception):
    """Raised inside the transaction so it rolls back."""


async def _main(args: argparse.Namespace) -> int:
    """Check the schema, back up the old database, migrate, and print the summary.

    Returns:
        The process exit code: 0 on success, 1 if nothing was migrated.
    """
    config = get_config()
    db = Database(config.database_url)
    try:
        if problem := await schema_problem(db):
            print(f"❌ {problem}")
            return 1
        old_db = Path(args.old_db) if args.old_db else None
        if old_db and old_db.exists() and not args.dry_run:
            print(f"Backed up the old database to {backup_old_db(old_db)}")
        summary = await migrate(
            db, Path(args.config), old_db, guild_name=args.guild_name, dry_run=args.dry_run, force=args.force
        )
    except MigrationError as e:
        print(f"❌ {e}")
        return 1
    finally:
        await db.dispose()
    print(summary.render())
    if not args.dry_run:
        print("✅ Done. Start JoltBot; old panels and open tickets keep working.")
    return 0


def main() -> None:
    """CLI entry: parse the arguments and run the migration."""
    parser = argparse.ArgumentParser(description="Seed JoltBot with the King of Chaos server's old settings.")
    parser.add_argument("--config", required=True, help="path to the old bot's config.json")
    parser.add_argument("--old-db", help="path to the old bot's data/tickets.db (optional)")
    parser.add_argument("--guild-name", default="King of Chaos", help="name shown until the bot refreshes it")
    parser.add_argument("--dry-run", action="store_true", help="show what would happen, save nothing")
    parser.add_argument("--force", action="store_true", help="run again even if already migrated")
    sys.exit(asyncio.run(_main(parser.parse_args())))


if __name__ == "__main__":
    main()
