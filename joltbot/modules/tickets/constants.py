"""Names for the fixed strings the ticket module stores.

Every value here is saved in the database (ticket status and outcome, dashboard
issue keys, the module's own name), so a value must never change: old rows
would stop matching. Imported by both the bot and the dashboard, so this file
must not import discord.py or FastAPI.
"""

from __future__ import annotations

# This module's key in the registry, in module_settings and in panel_messages.
MODULE = "tickets"

# Ticket.status. A ticket only ever moves forward: open -> closing -> closed
# (apart from a claim that is released again, see store.release).
STATUS_OPEN = "open"
STATUS_CLOSING = "closing"  # decided; the transcript, log post and channel delete are under way
STATUS_CLOSED = "closed"
ACTIVE_STATUSES = (STATUS_OPEN, STATUS_CLOSING)

# Ticket.outcome: why the ticket was closed.
OUTCOME_ACCEPTED = "accepted"
OUTCOME_DENIED = "denied"
OUTCOME_CLOSED = "closed"  # staff closed it without a decision
OUTCOME_LEFT = "left"  # the applicant left the server
OUTCOME_CHANNEL_DELETED = "channel_deleted"  # someone deleted the ticket channel by hand

# GuildIssue.key for each problem the module reports to a server's admins.
ISSUE_PREFIX = "tickets."
ISSUE_PENDING_ROLE = "tickets.pending_role"
ISSUE_STARTUP_SWEEP = "tickets.startup_sweep"
ISSUE_CATEGORY = "tickets.category"
ISSUE_CATEGORY_FULL = "tickets.category_full"
ISSUE_CREATE_CHANNEL = "tickets.create_channel"
ISSUE_DELETE_CHANNEL = "tickets.delete_channel"
ISSUE_LOG_CHANNEL = "tickets.log_channel"
ISSUE_CHOICE_EMOJI = "tickets.choice_emoji"
