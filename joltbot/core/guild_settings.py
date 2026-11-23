"""Load and save a guild's module settings. Used by both the bot and the dashboard.

The bot calls load_module() on every event, so changes saved on the dashboard
take effect immediately without a restart or cache to invalidate.

Transactions: these functions never commit. The caller wraps them, e.g.

    async with db.session() as session, session.begin():
        await save_module(session, ...)

so the setting change and its audit-log entry are saved together or not at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..modules.base import ModuleSettingsModel
from ..modules.registry import MODULES, get_module
from .db import rows_changed
from .models import AuditLog, ModuleSetting
from .types import utcnow

log = logging.getLogger(__name__)

T = TypeVar("T", bound=ModuleSettingsModel)


@dataclass(frozen=True)
class Actor:
    """Who made a change, for the audit log."""

    user_id: int | None
    name: str
    source: str  # "dashboard" | "bot" | "migration" | "system"


SYSTEM_ACTOR = Actor(None, "JoltBot", "system")


@dataclass(frozen=True)
class ModuleState(Generic[T]):
    """One module's settings for one server, as loaded from the database.

    Attributes:
        module: The module's registry name.
        enabled: Whether it is switched on. Always True for a module that can't be toggled.
        config: The validated settings; defaults if nothing is saved yet.
        version: Bumped on every save; 0 means no row has been saved yet.
    """

    module: str
    enabled: bool
    config: T
    version: int


class StaleSettingsError(Exception):
    """Someone else saved these settings since you loaded them."""


def _state_from_row(module: str, row: ModuleSetting | None) -> ModuleState[Any]:
    """Turn a stored row, or the lack of one, into a ``ModuleState``.

    Args:
        module: The module's registry name.
        row: Its saved row for one server, or ``None`` if there is none.

    Returns:
        The state to act on. Missing or invalid settings give defaults with the
        module switched off, unless the module is one that is always on.
    """
    info = get_module(module)
    # Modules that can't be toggled (like "general") are always on.
    always_on = not info.toggleable
    if row is None:
        # Guild has never been configured: defaults, and switched off.
        return ModuleState(module, always_on, info.settings_model(), 0)
    try:
        config = info.settings_model.model_validate(row.config or {})
    except ValidationError:
        # Stored settings no longer pass validation (e.g. edited by hand). Fall
        # back to defaults AND treat the module as off, so the bot never acts on
        # half-valid settings. The dashboard shows the defaults to re-save.
        log.exception("Invalid stored settings for guild %s module %s; using defaults (off)", row.guild_id, module)
        return ModuleState(module, always_on, info.settings_model(), row.version)
    return ModuleState(module, always_on or row.enabled, config, row.version)


async def load_module(session: AsyncSession, guild_id: int, module: str) -> ModuleState[Any]:
    """Load one module's settings for a guild (defaults if no row yet).

    Args:
        session: Open async session.
        guild_id: Discord guild snowflake.
        module: Registry module name (e.g. ``"tickets"``).

    Returns:
        ``ModuleState`` with validated config (or defaults if invalid/missing).
    """
    row = await session.get(ModuleSetting, (guild_id, module), populate_existing=True)
    return _state_from_row(module, row)


async def load_all(session: AsyncSession, guild_id: int) -> dict[str, ModuleState[Any]]:
    """Load every registered module's settings for a guild.

    Args:
        session: Open async session.
        guild_id: Discord guild snowflake.

    Returns:
        Mapping of module name → ``ModuleState``.
    """
    result = await session.execute(select(ModuleSetting).where(ModuleSetting.guild_id == guild_id))
    rows = {row.module: row for row in result.scalars()}
    return {name: _state_from_row(name, rows.get(name)) for name in MODULES}


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """{"messages": {"a": 1}} -> {"messages.a": 1}. Lists stay as single values."""
    flat: dict[str, Any] = {}
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{path}."))
        else:
            flat[path] = value
    return flat


def diff_settings(old: ModuleState[Any], enabled: bool, new_config: ModuleSettingsModel) -> dict[str, list[Any]]:
    """Build an audit-friendly change map.

    Args:
        old: Previously loaded module state.
        enabled: New enabled flag.
        new_config: New validated settings model.

    Returns:
        ``{"field": [old, new], ...}`` for every value that changed (nested
        keys flattened as ``messages.a``).
    """
    changes: dict[str, list[Any]] = {}
    if old.enabled != enabled:
        changes["enabled"] = [old.enabled, enabled]
    before = _flatten(old.config.model_dump(mode="json"))
    after = _flatten(new_config.model_dump(mode="json"))
    for key in sorted(before.keys() | after.keys()):
        if before.get(key) != after.get(key):
            changes[key] = [before.get(key), after.get(key)]
    return changes


async def save_module(
    session: AsyncSession,
    guild_id: int,
    module: str,
    *,
    enabled: bool,
    config: ModuleSettingsModel,
    expected_version: int,
    actor: Actor,
) -> ModuleState[Any]:
    """Save settings and write an audit entry in the same transaction.

    Uses optimistic concurrency: the UPDATE only matches when the stored
    ``version`` equals ``expected_version``.

    Args:
        session: Open async session (caller commits).
        guild_id: Discord guild snowflake.
        module: Registry module name.
        enabled: Whether the module is on (forced True if not toggleable).
        config: Validated settings model for this module.
        expected_version: Version the editor loaded (0 = no row yet).
        actor: Who made the change (audit log).

    Returns:
        New ``ModuleState`` including the bumped version.

    Raises:
        TypeError: ``config`` is not this module's settings model.
        StaleSettingsError: Another save won the race (version mismatch).
    """
    info = get_module(module)
    if not isinstance(config, info.settings_model):
        raise TypeError(f"{module} settings must be {info.settings_model.__name__}")
    if not info.toggleable:
        enabled = True

    current = await load_module(session, guild_id, module)
    if current.version != expected_version:
        raise StaleSettingsError()
    changes = diff_settings(current, enabled, config)
    if not changes:
        return current

    stored = config.model_dump(mode="json")  # IDs become strings; safe for JSON
    now = utcnow()
    if expected_version == 0:
        session.add(
            ModuleSetting(
                guild_id=guild_id,
                module=module,
                enabled=enabled,
                config=stored,
                version=1,
                updated_at=now,
                updated_by=actor.user_id,
            )
        )
    else:
        # The "WHERE version = expected" makes this race-proof: if another save
        # slipped in after we loaded, no row matches and nothing is overwritten.
        result = await session.execute(
            update(ModuleSetting)
            .where(
                ModuleSetting.guild_id == guild_id,
                ModuleSetting.module == module,
                ModuleSetting.version == expected_version,
            )
            .values(
                enabled=enabled, config=stored, version=expected_version + 1, updated_at=now, updated_by=actor.user_id
            )
        )
        if rows_changed(result) != 1:
            raise StaleSettingsError()

    session.add(
        AuditLog(
            guild_id=guild_id,
            actor_id=actor.user_id,
            actor_name=actor.name[:100],
            source=actor.source,
            action="settings.update",
            module=module,
            changes=changes,
            created_at=now,
        )
    )
    await session.flush()
    return ModuleState(module, enabled, config, expected_version + 1)
