"""The button IDs of the two persistent ticket views.

Discord sends a button's ``custom_id`` back on every click, and that is how a
click on a message posted months ago finds its handler after a restart. The
IDs are therefore part of every panel and ticket message already posted:
never change them.
"""

from __future__ import annotations

PANEL_ID = "joltbot:tickets:open"
ACCEPT_ID, DENY_ID, CLOSE_ID = "joltbot:tickets:accept", "joltbot:tickets:deny", "joltbot:tickets:close"
# The IDs the old KOC Gatekeeper bot used for the same buttons.
LEGACY_IDS = {
    PANEL_ID: "rokverify:open_application",
    ACCEPT_ID: "rokticket:accept",
    DENY_ID: "rokticket:deny",
    CLOSE_ID: "rokticket:close",
}


def custom_id(new_id: str, legacy: bool) -> str:
    """The ID a button is registered under: JoltBot's own, or the old bot's for the same button."""
    return LEGACY_IDS[new_id] if legacy else new_id
