"""Buttons, forms and prompts for the ticket module.

PanelView and TicketControlView are *persistent*: they're registered once at
startup with fixed custom_ids and timeout=None, so their buttons keep working
on every message ever posted, even after a restart. Neither stores per-guild
data; each click looks up the guild's settings (and the ticket, by channel ID).

Messages posted by the old KOC Gatekeeper bot use the IDs "rokverify:..." and
"rokticket:...". We register those too (legacy=True), so old panels and open
tickets keep working. Once every server has re-posted its panel with
/tickets panel and old tickets are closed, the legacy registration can go.

Where things are:
- applicant.py: the panel button, the dropdown step and the application form.
- staff.py: the Accept / Deny / Close buttons in a ticket, and the prompts they open.
- common.py: the button IDs.
"""

from .applicant import NOT_SET_UP, ApplicationModal, ChoiceStepView, PanelView, option_emoji
from .common import ACCEPT_ID, CLOSE_ID, DENY_ID, LEGACY_IDS, PANEL_ID
from .staff import (
    AcceptView,
    CloseConfirmView,
    DenyActionView,
    DenyReasonModal,
    TicketControlView,
    staff_check,
)

__all__ = [
    "ACCEPT_ID",
    "CLOSE_ID",
    "DENY_ID",
    "LEGACY_IDS",
    "NOT_SET_UP",
    "PANEL_ID",
    "AcceptView",
    "ApplicationModal",
    "ChoiceStepView",
    "CloseConfirmView",
    "DenyActionView",
    "DenyReasonModal",
    "PanelView",
    "TicketControlView",
    "option_emoji",
    "staff_check",
]
