"""The list of JoltBot's feature modules.

To add a module (e.g. auto-roles): create joltbot/modules/<name>/ with a
settings.py (a ModuleSettingsModel subclass), optionally a bot cog (cog.py),
dashboard routes (web.py) and templates/<name>/, then add one ModuleInfo below. The bot and the
dashboard both read this list, so nothing else needs restructuring.
"""

from __future__ import annotations

from dataclasses import dataclass

from .base import ModuleSettingsModel
from .general.settings import GeneralSettings
from .tickets.settings import TicketSettings


@dataclass(frozen=True)
class ModuleInfo:
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
            name="general",
            title="General",
            description="Server-wide settings such as embed colour.",
            settings_model=GeneralSettings,
            toggleable=False,
            web="joltbot.modules.general.web",
        ),
        ModuleInfo(
            name="tickets",
            title="Tickets & verification",
            description="New members apply through a form; staff accept or deny in a private ticket.",
            settings_model=TicketSettings,
            cog="joltbot.modules.tickets.cog",
            web="joltbot.modules.tickets.web",
        ),
    )
}


class UnknownModule(KeyError):
    pass


def get_module(name: str) -> ModuleInfo:
    try:
        return MODULES[name]
    except KeyError:
        raise UnknownModule(name) from None
