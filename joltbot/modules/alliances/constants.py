"""Names for the fixed strings the alliance module stores.

Every value here is saved in the database (request status, dashboard issue
keys, the module's own name), so a value must never change: old rows would
stop matching. Imported by both the bot and the dashboard, so this file must
not import discord.py or FastAPI.
"""

from __future__ import annotations

# This module's key in the registry, in module_settings and in panel_messages.
MODULE = "alliances"

# AllianceRequest.status.
STATUS_PENDING = "pending"  # waiting for the alliance's leadership
STATUS_DECIDING = "deciding"  # Accept was clicked; the role change is under way
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"
STATUS_CLOSED = "closed"  # the member left the server before a decision
STATUS_CANCELLED = "cancelled"  # an admin cancelled it with /alliances cancel
# A member can have one request in these states at a time (see the index in core/models.py).
ACTIVE_STATUSES = (STATUS_PENDING, STATUS_DECIDING)

# GuildIssue.key for each problem the module reports to a server's admins.
ISSUE_PREFIX = "alliances."
ISSUE_REQUEST_CHANNEL = "alliances.request_channel"
ISSUE_LOG_CHANNEL = "alliances.log_channel"
