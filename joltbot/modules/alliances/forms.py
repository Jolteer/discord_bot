"""Turns the dashboard's Alliances form into settings, and checks them against
the server's real roles and channels.

No FastAPI or HTML here: these are plain functions, so a future JSON API can
reuse them unchanged.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from pydantic import ValidationError

from ...core.discord_rest import GuildData
from ...core.permissions import ADMINISTRATOR, BOT_PERMISSIONS, elevated_permission_names
from ..tickets.settings import TicketSettings
from .settings import DEFAULT_REJECT_COOLDOWN_HOURS, MAX_ALLIANCES, MAX_REJECT_COOLDOWN_HOURS, AllianceSettings

TICKETS_TITLE = "Tickets & verification"
FIELD_LABELS = {
    "alliances": "Alliances",
    "override_role_ids": "Override roles",
    "panel_channel_id": "Panel channel",
    "log_channel_id": "Log channel",
    "reject_cooldown_hours": "Wait after a rejection",
    "panel_title": "Panel title",
    "panel_description": "Panel text",
}
# What is missing from an alliance block, by the field that failed.
ALLIANCE_FIELD_ERRORS = {
    "name": "needs a name",
    "member_role_id": "pick its member role",
    "reviewer_role_ids": "pick at least one reviewer role",
    "request_channel_id": "pick its request channel",
}


def _text(form: Mapping[str, Any], name: str) -> str:
    """One form field as text, with line endings normalised and the ends trimmed.

    A missing field, or a file where text was expected, gives ``""``.
    """
    value = form.get(name)
    return value.replace("\r\n", "\n").strip() if isinstance(value, str) else ""


def _ids(form: Any, name: str) -> list[str]:
    """Every non-empty value of a field sent more than once (a tick list sends one per ticked box)."""
    return [v for v in form.getlist(name) if isinstance(v, str) and v.strip()]


def parse_form(form: Any) -> dict[str, Any]:
    """Map HTML form fields to a dict shaped like ``AllianceSettings`` (IDs as strings).

    Each alliance is a block of fields: ``a_name_<n>``, ``a_role_<n>``,
    ``a_channel_<n>`` and the tick list ``a_reviewers_<n>``. A block whose name
    was cleared is dropped (that is how an alliance is removed).

    Args:
        form: Starlette form data (supports ``get`` / ``getlist``).

    Returns:
        Unvalidated settings dict ready for ``AllianceSettings.model_validate``.
    """
    alliances = [
        {
            "name": name,
            "member_role_id": _text(form, f"a_role_{i}"),
            "reviewer_role_ids": _ids(form, f"a_reviewers_{i}"),
            "request_channel_id": _text(form, f"a_channel_{i}"),
        }
        for i in range(MAX_ALLIANCES)
        if (name := _text(form, f"a_name_{i}"))
    ]
    return {
        "alliances": alliances,
        "override_role_ids": _ids(form, "override_role_ids"),
        "panel_channel_id": _text(form, "panel_channel_id") or None,
        "log_channel_id": _text(form, "log_channel_id") or None,
        "reject_cooldown_hours": _text(form, "reject_cooldown_hours") or str(DEFAULT_REJECT_COOLDOWN_HOURS),
        "panel_title": _text(form, "panel_title"),
        "panel_description": _text(form, "panel_description"),
    }


def validation_errors(exc: ValidationError) -> list[str]:
    """Turn Pydantic errors into short sentences an admin can act on.

    Args:
        exc: Validation failure from ``AllianceSettings.model_validate``.

    Returns:
        Human-readable error lines for the settings form, without repeats.
    """
    errors = []
    for err in exc.errors():
        loc = [str(part) for part in err["loc"]]
        message = err["msg"].removeprefix("Value error, ")
        if loc[:1] == ["alliances"] and len(loc) > 2 and loc[1].isdigit():
            errors.append(f"Alliance {int(loc[1]) + 1}: {ALLIANCE_FIELD_ERRORS.get(loc[2], message)}.")
        elif loc[:1] == ["reject_cooldown_hours"]:
            errors.append(
                f"Wait after a rejection: enter a whole number of hours from 0 to {MAX_REJECT_COOLDOWN_HOURS}."
            )
        elif loc:
            errors.append(f"{FIELD_LABELS.get(loc[0], loc[0])}: {message}")
        else:
            errors.append(message)  # a rule between fields; its text already names them
    return list(dict.fromkeys(errors))


def foreign_id_errors(settings: AllianceSettings, data: GuildData) -> list[str]:
    """Ensure every role/channel ID belongs to this server.

    Without this check an admin could paste IDs from another server into the
    form, and e.g. JoltBot would post this server's join requests there.

    Args:
        settings: Candidate alliance settings from the form.
        data: Live roles/channels for this server.

    Returns:
        Error lines naming what is foreign, missing, or can't be assigned.
    """
    roles = {r.id: r for r in data.roles}
    text_ids = {c.id for c in data.text_channels()}
    not_here = "isn't in this server. Refresh the page and pick again."
    errors = []
    for alliance in settings.alliances:
        role = roles.get(alliance.member_role_id)
        if role is None:
            errors.append(f"{alliance.name}: its member role {not_here}")
        elif role.managed:
            errors.append(
                f"{alliance.name}: {role.name} is managed by an integration, so JoltBot can't give it to members."
            )
        if any(role_id not in roles for role_id in alliance.reviewer_role_ids):
            errors.append(f"{alliance.name}: a reviewer role {not_here}")
        if alliance.request_channel_id not in text_ids:
            errors.append(f"{alliance.name}: its request channel {not_here}")
    if any(role_id not in roles for role_id in settings.override_role_ids):
        errors.append(f"Override roles: a selected role {not_here}")
    for label, channel_id in (("Panel channel", settings.panel_channel_id), ("Log channel", settings.log_channel_id)):
        if channel_id is not None and channel_id not in text_ids:
            errors.append(f"{label}: that channel {not_here}")
    return errors


def powerful_role_errors(
    settings: AllianceSettings,
    previous: AllianceSettings,
    data: GuildData,
    *,
    actor_is_admin: bool,
    dashboard_role_ids: Collection[int] = (),
) -> list[str]:
    """Stop Manage Server alone from making JoltBot hand out a powerful role.

    The same rule as on the Tickets page (``tickets.forms.powerful_role_errors``):
    in Discord, Manage Server doesn't let you assign roles, so a dashboard user
    must not be able to set, say, a moderator role as an alliance's member role
    and then get it through a join request. A role that opens this dashboard
    counts as powerful too.

    Only *newly added* member roles are checked. One an admin already saved
    stays, so other managers can keep editing the page. Reviewer and override
    roles are not checked: JoltBot reads those but never gives them to anyone.

    Args:
        settings: Candidate settings from the form.
        previous: Settings currently saved for this guild.
        data: Live roles for the guild (permission bits come from here).
        actor_is_admin: True if the saving user is the owner or an Administrator.
        dashboard_role_ids: This server's dashboard access roles (General settings).

    Returns:
        Error lines; empty if the save is allowed.
    """
    if actor_is_admin:
        return []
    roles = {r.id: r for r in data.roles}
    only_admins = "Only the server owner or an Administrator can make JoltBot give out a role like that."
    errors = []
    for alliance in settings.alliances:
        role = roles.get(alliance.member_role_id)
        if role is None or role.id in previous.member_role_ids:
            continue
        if names := elevated_permission_names(role.permissions):
            errors.append(f"{alliance.name}: {role.name} has powerful permissions ({', '.join(names)}). {only_admins}")
        elif role.id in dashboard_role_ids:
            errors.append(f"{alliance.name}: {role.name} gives access to this dashboard. {only_admins}")
    return errors


# Server-wide permissions the alliance module needs, as (bit, name). Bits come from the invite list.
NEEDED_PERMISSIONS = [
    (BOT_PERMISSIONS[name], name) for name in ("Manage Roles", "View Channels", "Send Messages", "Embed Links")
]


def setup_warnings(settings: AllianceSettings, data: GuildData, tickets: TicketSettings) -> list[str]:
    """Soft problems visible from the dashboard (perms, role order, deleted roles and channels).

    Args:
        settings: Current alliance settings.
        data: Live Discord guild snapshot.
        tickets: The server's ticket settings, to spot an alliance role that tickets also hand out.

    Returns:
        Warning lines shown above the form (non-blocking).
    """
    warnings = []
    perms = data.bot_permissions()
    if not perms & ADMINISTRATOR and (missing := [name for bit, name in NEEDED_PERMISSIONS if not perms & bit]):
        warnings.append(
            f"JoltBot's role is missing these permissions: {', '.join(missing)}. Add them in Server Settings → Roles."
        )

    roles = {r.id: r for r in data.roles}
    top = data.bot_top_position()
    member_roles = [roles[a.member_role_id] for a in settings.alliances if a.member_role_id in roles]
    if too_high := sorted(r.name for r in member_roles if r.position >= top):
        warnings.append(
            f"JoltBot's role must be above {', '.join(too_high)} or it can't give or remove "
            "them. Drag JoltBot's role higher in Server Settings → Roles."
        )
    for role in member_roles:
        if names := elevated_permission_names(role.permissions):
            warnings.append(
                f"JoltBot gives out {role.name}, which has powerful permissions "
                f"({', '.join(names)}). Make sure that's intended."
            )

    # A role tickets also give out can be had without this alliance's leadership saying yes.
    by_role = {a.member_role_id: a for a in settings.alliances}
    for given in tickets.assignable_roles():
        if given.role_id in by_role:
            name = roles[given.role_id].name if given.role_id in roles else by_role[given.role_id].name
            warnings.append(
                f"{name} is also given out by {TICKETS_TITLE} ({given.description}), so ticket staff can hand out "
                f"that alliance's access without its leadership. Remove it on the Tickets page."
            )

    channel_ids = {c.id for c in data.channels}
    for alliance in settings.alliances:
        if alliance.member_role_id not in roles:
            warnings.append(f"The member role of {alliance.name} was deleted. Pick a new one.")
        if any(role_id not in roles for role_id in alliance.reviewer_role_ids):
            warnings.append(f"One of the reviewer roles of {alliance.name} was deleted. Review them below and save.")
        if alliance.request_channel_id not in channel_ids:
            warnings.append(f"The request channel of {alliance.name} was deleted. Pick a new one.")
    if any(role_id not in roles for role_id in settings.override_role_ids):
        warnings.append("One of the override roles was deleted. Review the roles below and save.")
    for label, channel_id in (("panel channel", settings.panel_channel_id), ("log channel", settings.log_channel_id)):
        if channel_id and channel_id not in channel_ids:
            warnings.append(f"The {label} you picked was deleted. Pick a new one.")
    return warnings


def alliance_rows(values: dict[str, Any]) -> list[dict[str, Any]]:
    """Alliance blocks for the form: the saved ones, then one empty block to add another.

    Args:
        values: Settings values (saved, or as typed when a save failed).

    Returns:
        At most ``MAX_ALLIANCES`` blocks of ``{"name", "member_role_id", "reviewer_role_ids", "request_channel_id"}``.
    """
    blocks = [
        {
            "name": alliance.get("name", ""),
            "member_role_id": alliance.get("member_role_id") or "",
            "reviewer_role_ids": list(alliance.get("reviewer_role_ids") or []),
            "request_channel_id": alliance.get("request_channel_id") or "",
        }
        for alliance in list(values.get("alliances") or [])[:MAX_ALLIANCES]
    ]
    if len(blocks) < MAX_ALLIANCES:
        blocks.append({"name": "", "member_role_id": "", "reviewer_role_ids": [], "request_channel_id": ""})
    return blocks
