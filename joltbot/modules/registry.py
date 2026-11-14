"""The list of JoltBot's feature modules.

To add a module (e.g. auto-roles): create joltbot/modules/<name>/ with a
settings.py (a ModuleSettingsModel subclass), optionally a bot cog (cog.py),
dashboard routes (web.py) and templates/<name>/, then add one ModuleInfo below. The bot and the
dashboard both read this list, so nothing else needs restructuring.
"""

from __future__ import annotations

from dataclasses import dataclass

from .base import ModuleSettingsModel
from .general.settings import MODULE as GENERAL
from .general.settings import GeneralSettings
from .tickets.constants import MODULE as TICKETS
from .tickets.settings import TicketSettings


@dataclass(frozen=True)
class ModuleInfo:
    """One feature module known to both the bot and the dashboard.

    Attributes:
        name: Database key; never rename once released.
        title: Human label for the sidebar / overview.
        description: Short blurb for the overview page.
        settings_model: Pydantic model with defaults for every field.
        toggleable: False = always on (e.g. general settings).
        cog: Dotted bot extension path, or None if dashboard-only.
        web: Dotted module exporting FastAPI ``router``, or None if bot-only.
    """

    name: str  # stored in the database; never rename once released
    title: str
    description: str
    settings_model: type[ModuleSettingsModel]
    toggleable: bool = True  # False = always on (e.g. general settings)
    cog: str | None = None  # dotted path of the bot extension, loaded only by the bot process
    web: str | None = None  # dotted path of a module with a FastAPI `router`, loaded only by the dashboard


MODULES: dict[str, ModuleInfo] = {
    m.name: m
    for m in (
        ModuleInfo(
            name=GENERAL,
            title="General",
            description="Server-wide settings such as embed colour.",
            settings_model=GeneralSettings,
            toggleable=False,
            web="joltbot.modules.general.web",
        ),
        ModuleInfo(
            name=TICKETS,
            title="Tickets & verification",
            description="New members apply through a form; staff accept or deny in a private ticket.",
            settings_model=TicketSettings,
            cog="joltbot.modules.tickets.cog",
            web="joltbot.modules.tickets.web",
        ),
    )
}


class UnknownModule(KeyError):
    """Raised when ``get_module`` is called with a name not in ``MODULES``."""


def get_module(name: str) -> ModuleInfo:
    """Look up a registered module by name.

    Args:
        name: Registry key (e.g. ``"tickets"``).

    Returns:
        The matching ``ModuleInfo``.

    Raises:
        UnknownModule: ``name`` is not registered.
    """
    try:
        return MODULES[name]
    except KeyError:
        raise UnknownModule(name) from None
