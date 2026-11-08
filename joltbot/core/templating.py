"""Fills {placeholders} in admin-written messages, e.g. "Welcome {user} to {server}!".

The old bot used str.format_map, which raises an error on text like "{" or
"{0}". Admins type these messages in the dashboard, so a stray brace must
never crash the bot. This version only replaces {simple_names} and leaves
everything else exactly as typed.

The dashboard's live preview mirrors these rules in JavaScript.
"""
from __future__ import annotations

import re

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")

# Older names that still work. {guild} was used by the original bot.
ALIASES = {"guild": "server"}


def render(template: str, **values: object) -> str:
    """Replace known {names}; unknown ones are left untouched."""

    def replace(match: re.Match[str]) -> str:
        name = ALIASES.get(match.group(1), match.group(1))
        if name in values:
            return str(values[name])
        return match.group(0)

    return _PLACEHOLDER.sub(replace, template)


def placeholders_in(template: str) -> set[str]:
    """Names used in a template (after aliasing). Handy for validation."""
    return {ALIASES.get(name, name) for name in _PLACEHOLDER.findall(template)}
