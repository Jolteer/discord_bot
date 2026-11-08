"""Loads config.json (server layout) + .env (secrets) and validates them.

Every problem is collected and reported at once, in plain language, so you
don't have to fix-restart-fix-restart.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


class ConfigError(Exception):
    """Raised when config.json / .env is missing or invalid."""


DEFAULT_MESSAGES: dict[str, str] = {
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
    "deny_dm": (
        "❌ Your application to **{guild}** was not accepted.\n\n"
        "**Reason:** {reason}"
    ),
    "deny_dm_kicked_suffix": "\n\nYou have been removed from the server.",
    "deny_dm_kept_suffix": "\n\nYou can open a new application from the verify channel if your situation changes.",
}


@dataclass(frozen=True)
class AllianceRole:
    label: str
    role_id: int
    emoji: str | None = None


@dataclass(frozen=True)
class Config:
    token: str
    guild_id: int

    # role name shown to people -> role ID. These roles can accept/deny/close.
    approval_roles: dict[str, int]
    unverified_role_id: int
    member_role_id: int
    alliance_roles: list[AllianceRole]

    verify_channel_id: int
    welcome_channel_id: int | None
    tickets_category_id: int
    ticket_logs_channel_id: int

    ping_enabled: bool
    ping_role_ids: list[int]

    allow_administrators: bool
    assign_unverified_on_startup: bool
    delete_delay_seconds: int
    reupload_attachments: bool
    database_path: str
    messages: dict[str, str] = field(default_factory=dict)

    @property
    def approval_role_ids(self) -> set[int]:
        return set(self.approval_roles.values())

    @property
    def effective_ping_role_ids(self) -> list[int]:
        """Roles to ping on a new ticket. Empty list in config = ping the approval roles."""
        if not self.ping_enabled:
            return []
        return self.ping_role_ids or list(self.approval_roles.values())


def _parse_id(value: Any, where: str, errors: list[str], *, optional: bool = False) -> int | None:
    if value in (None, "", 0, "0"):
        if optional:
            return None
        errors.append(f"{where}: missing. Paste the ID (Discord Developer Mode → right-click → Copy ID).")
        return None
    try:
        parsed = int(str(value).strip())
    except ValueError:
        errors.append(f"{where}: '{value}' is not a Discord ID (should be a long number like 1234567890123456789).")
        return None
    if parsed <= 0:
        errors.append(f"{where}: '{value}' is not a valid Discord ID.")
        return None
    return parsed


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    load_dotenv()
    cfg_path = Path(path or os.getenv("CONFIG_PATH", "config.json"))
    if not cfg_path.exists():
        raise ConfigError(
            f"Config file not found: {cfg_path.resolve()}\n"
            "Copy config.example.json to config.json and fill in your server's IDs."
        )
    try:
        raw: dict[str, Any] = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(
            f"{cfg_path} is not valid JSON (line {e.lineno}, column {e.colno}): {e.msg}. "
            "Common causes: a trailing comma, or a missing quote."
        ) from e

    token = os.getenv("DISCORD_TOKEN", "").strip()
    errors: list[str] = []
    if not token:
        errors.append("DISCORD_TOKEN is missing. Put it in a .env file next to run.py (see .env.example).")

    guild_id = _parse_id(raw.get("guild_id"), "guild_id", errors)

    roles = raw.get("roles") or {}
    approval_raw = roles.get("approval") or {}
    if not isinstance(approval_raw, dict) or not approval_raw:
        errors.append('roles.approval: add at least one role, e.g. {"King": "...", "R4": "...", "Mod": "..."}.')
        approval_raw = {}
    approval_roles: dict[str, int] = {}
    for name, rid in approval_raw.items():
        parsed = _parse_id(rid, f"roles.approval.{name}", errors)
        if parsed:
            approval_roles[str(name)] = parsed

    unverified = _parse_id(roles.get("unverified"), "roles.unverified", errors)
    member = _parse_id(roles.get("member"), "roles.member", errors)

    alliance_roles: list[AllianceRole] = []
    for i, entry in enumerate(roles.get("alliances") or []):
        where = f"roles.alliances[{i}]"
        if not isinstance(entry, dict) or not entry.get("label"):
            errors.append(f'{where}: each alliance needs a "label" and a "role_id".')
            continue
        rid = _parse_id(entry.get("role_id"), f"{where}.role_id", errors)
        if rid:
            alliance_roles.append(AllianceRole(str(entry["label"])[:100], rid, entry.get("emoji") or None))
    if len(alliance_roles) > 24:
        errors.append("roles.alliances: Discord dropdowns allow at most 24 alliance roles (plus 'No alliance').")

    channels = raw.get("channels") or {}
    verify_ch = _parse_id(channels.get("verify"), "channels.verify", errors)
    welcome_ch = _parse_id(channels.get("welcome"), "channels.welcome", errors, optional=True)
    tickets_cat = _parse_id(channels.get("tickets_category"), "channels.tickets_category", errors)
    logs_ch = _parse_id(channels.get("ticket_logs"), "channels.ticket_logs", errors)

    ping = raw.get("ping_on_new_ticket") or {}
    ping_ids: list[int] = []
    for i, rid in enumerate(ping.get("role_ids") or []):
        parsed = _parse_id(rid, f"ping_on_new_ticket.role_ids[{i}]", errors)
        if parsed:
            ping_ids.append(parsed)

    tickets = raw.get("tickets") or {}
    try:
        delay = int(tickets.get("delete_delay_seconds", 10))
        if delay < 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("tickets.delete_delay_seconds must be a whole number of seconds (0 or more).")
        delay = 10

    messages = dict(DEFAULT_MESSAGES)
    for key, value in (raw.get("messages") or {}).items():
        if key in DEFAULT_MESSAGES and isinstance(value, str) and value.strip():
            messages[key] = value

    if errors:
        raise ConfigError("Fix these problems in config.json / .env:\n  - " + "\n  - ".join(errors))

    return Config(
        token=token,
        guild_id=guild_id,  # type: ignore[arg-type]
        approval_roles=approval_roles,
        unverified_role_id=unverified,  # type: ignore[arg-type]
        member_role_id=member,  # type: ignore[arg-type]
        alliance_roles=alliance_roles,
        verify_channel_id=verify_ch,  # type: ignore[arg-type]
        welcome_channel_id=welcome_ch,
        tickets_category_id=tickets_cat,  # type: ignore[arg-type]
        ticket_logs_channel_id=logs_ch,  # type: ignore[arg-type]
        ping_enabled=bool(ping.get("enabled", True)),
        ping_role_ids=ping_ids,
        allow_administrators=bool(raw.get("allow_administrators", False)),
        assign_unverified_on_startup=bool(raw.get("assign_unverified_on_startup", False)),
        delete_delay_seconds=delay,
        reupload_attachments=bool(tickets.get("reupload_attachments", True)),
        database_path=str(raw.get("database_path") or "data/tickets.db"),
        messages=messages,
    )
