"""Turns the dashboard's ticket settings form into settings, and checks them
against the server's real roles and channels.

No FastAPI or HTML here: these are plain functions, so a future JSON API can
reuse them unchanged.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from pydantic import ValidationError

from ...core.discord_rest import GuildData
from ...core.permissions import ADMINISTRATOR, BOT_PERMISSIONS, elevated_permission_names
from ...web.options import role_option, text_channel_options
from .settings import (
    DEFAULT_ANSWER_LENGTH,
    DEFAULT_DELETE_DELAY_SECONDS,
    MAX_CHOICE_OPTIONS,
    MAX_CHOICE_QUESTIONS,
    MAX_EXTRA_ROLE_CHOICES,
    MAX_FORM_QUESTIONS,
    PLACEHOLDERS,
    TicketMessages,
    TicketSettings,
    custom_emoji_id,
    custom_emoji_name,
)

BLANK_EXTRA_ROWS = 3  # empty rows shown so admins can add extra role choices
BLANK_OPTION_ROWS = 3  # empty option rows shown under each dropdown question
# Extra-role rows are numbered from 0 and read until one is missing. This stops a crafted
# form from making that loop long; it is well above the number the page ever shows.
MAX_EXTRA_ROWS_READ = 40

FIELD_LABELS = {
    "staff_role_ids": "Staff roles",
    "panel_channel_id": "Panel channel",
    "ticket_category_id": "Ticket category",
    "log_channel_id": "Log channel",
    "welcome_channel_id": "Welcome channel",
    "pending_role_id": "Pending role",
    "accept_role_ids": "Roles given on accept",
    "extra_role_choices": "Extra role choices",
    "ping_role_ids": "Ping roles",
    "form_questions": "Application questions",
    "choice_questions": "Dropdown questions",
    "delete_delay_seconds": "Delete delay",
    "messages": "Messages",
}

MESSAGE_INFO: dict[str, tuple[str, str]] = {
    # key: (label, help text)
    "panel_title": ("Panel title", "Title of the panel with the Open Application button."),
    "panel_description": ("Panel text", "Shown on the panel. Supports **bold**."),
    "panel_button_label": ("Button label", "Text on the panel's button."),
    "form_title": ("Form title", "Title of the application form (max 45 characters)."),
    "ticket_opened": ("Ticket opened (private reply)", "Shown only to the applicant after they submit."),
    "ticket_intro": ("Welcome message in the ticket", "Posted in the new ticket channel."),
    "welcome_dm": ("Accepted DM", "Sent to the member when they're accepted."),
    "deny_dm": ("Denied DM", "Sent to the member when they're denied."),
    "deny_dm_kicked_suffix": ("Denied DM: kicked ending", "Added to the denied DM when they're kicked."),
    "deny_dm_kept_suffix": ("Denied DM: not kicked ending", "Added to the denied DM when they stay."),
    "not_staff": ("Not staff reply", "Shown when a non-staff member clicks a staff button."),
    "already_verified": ("Already verified reply", "Shown when an accepted member clicks the panel."),
    "deny_reason_hint": ("Deny reason hint", "Grey hint text in the deny reason box."),
}
SINGLE_LINE_MESSAGES = {"panel_title", "panel_button_label", "form_title", "deny_reason_hint", "not_staff"}


def _text(form: Mapping[str, Any], name: str) -> str:
    """One form field as text, with line endings normalised and the ends trimmed.

    A missing field, or a file where text was expected, gives ``""``.
    """
    value = form.get(name)
    return value.replace("\r\n", "\n").strip() if isinstance(value, str) else ""


def _ids(form: Any, name: str) -> list[str]:
    """Every non-empty value of a field sent more than once (a tick list sends one per ticked box)."""
    return [v for v in form.getlist(name) if isinstance(v, str) and v.strip()]


def _parse_questions(form: Any) -> list[dict[str, Any]]:
    """The typed questions: fields ``q_label_<n>``, ``q_placeholder_<n>``, ``q_required_<n>``,
    ``q_long_<n>`` and ``q_max_<n>``. A row with no label is skipped (that is how one is removed)."""
    questions = []
    for i in range(MAX_FORM_QUESTIONS):
        label = _text(form, f"q_label_{i}")
        if label:
            questions.append(
                {
                    "label": label,
                    "placeholder": _text(form, f"q_placeholder_{i}"),
                    "required": f"q_required_{i}" in form,
                    "long": f"q_long_{i}" in form,
                    "max_length": _text(form, f"q_max_{i}") or str(DEFAULT_ANSWER_LENGTH),
                }
            )
    return questions


def _parse_choices(form: Any) -> list[dict[str, Any]]:
    """The dropdown questions: ``c_label_<question>``, ``c_required_<question>``, and per option
    ``c_opt_label_<question>_<option>``, ``c_opt_role_...`` and ``c_opt_emoji_...``.

    A question whose name was cleared is dropped, options and all (that is how one is removed).
    """
    choices = []
    for q in range(MAX_CHOICE_QUESTIONS):
        label = _text(form, f"c_label_{q}")
        if not label:
            continue
        options = []
        for o in range(MAX_CHOICE_OPTIONS):
            option_label, role_id = _text(form, f"c_opt_label_{q}_{o}"), _text(form, f"c_opt_role_{q}_{o}")
            emoji = _text(form, f"c_opt_emoji_{q}_{o}")
            if option_label or role_id or emoji:
                options.append({"label": option_label, "role_id": role_id or None, "emoji": emoji or None})
        choices.append({"label": label, "required": f"c_required_{q}" in form, "options": options})
    return choices


def _parse_extras(form: Any) -> list[dict[str, Any]]:
    """The extra role choices: ``extra_label_<n>``, ``extra_role_<n>`` and ``extra_emoji_<n>``.

    A row with neither a label nor a role is skipped.
    """
    extras = []
    i = 0
    while f"extra_label_{i}" in form and i < MAX_EXTRA_ROWS_READ:
        label, role_id, emoji = (
            _text(form, f"extra_label_{i}"),
            _text(form, f"extra_role_{i}"),
            _text(form, f"extra_emoji_{i}"),
        )
        if label or role_id:
            extras.append({"label": label, "role_id": role_id, "emoji": emoji or None})
        i += 1
    return extras


def parse_form(form: Any) -> dict[str, Any]:
    """Map HTML form fields to a dict shaped like ``TicketSettings`` (IDs as strings).

    Checkboxes that aren't ticked aren't sent by the browser at all, hence
    ``name in form``.

    Args:
        form: Starlette form data (supports ``get`` / ``getlist``).

    Returns:
        Unvalidated settings dict ready for ``TicketSettings.model_validate``.
    """
    return {
        "staff_role_ids": _ids(form, "staff_role_ids"),
        "allow_administrators": "allow_administrators" in form,
        "panel_channel_id": _text(form, "panel_channel_id") or None,
        "ticket_category_id": _text(form, "ticket_category_id") or None,
        "log_channel_id": _text(form, "log_channel_id") or None,
        "welcome_channel_id": _text(form, "welcome_channel_id") or None,
        "pending_role_id": _text(form, "pending_role_id") or None,
        "accept_role_ids": _ids(form, "accept_role_ids"),
        "extra_role_choices": _parse_extras(form),
        "ping_enabled": "ping_enabled" in form,
        "ping_role_ids": _ids(form, "ping_role_ids"),
        "form_questions": _parse_questions(form),
        "choice_questions": _parse_choices(form),
        "require_attachment": "require_attachment" in form,
        "delete_delay_seconds": _text(form, "delete_delay_seconds") or str(DEFAULT_DELETE_DELAY_SECONDS),
        "reupload_attachments": "reupload_attachments" in form,
        "assign_pending_on_startup": "assign_pending_on_startup" in form,
        # Messages are not stripped of inner newlines; only the ends are trimmed.
        "messages": {key: _text(form, f"msg_{key}") for key in TicketMessages.model_fields},
    }


def validation_errors(exc: ValidationError) -> list[str]:
    """Turn Pydantic errors into short sentences an admin can act on.

    Args:
        exc: Validation failure from ``TicketSettings.model_validate``.

    Returns:
        Human-readable error lines for the settings form.
    """
    errors = []
    for err in exc.errors():
        loc = [str(part) for part in err["loc"]]
        where = FIELD_LABELS.get(loc[0], loc[0]) if loc else "Settings"
        if loc[:1] == ["messages"] and len(loc) > 1:
            where = MESSAGE_INFO.get(loc[1], (loc[1], ""))[0]
        elif loc[:1] == ["form_questions"] and len(loc) > 1 and loc[1].isdigit():
            where = f"Question {int(loc[1]) + 1}"
        elif loc[:1] == ["extra_role_choices"] and len(loc) > 1 and loc[1].isdigit():
            where = f"Extra role choice {int(loc[1]) + 1}"
        elif loc[:1] == ["choice_questions"] and len(loc) > 1 and loc[1].isdigit():
            where = f"Dropdown question {int(loc[1]) + 1}"
            if loc[2:3] == ["options"] and len(loc) > 3 and loc[3].isdigit():
                where += f", option {int(loc[3]) + 1}"
        message = err["msg"].removeprefix("Value error, ")
        if loc[:1] == ["choice_questions"] and loc[2:] == ["options"] and err["type"] == "too_short":
            message = "needs at least 2 options"
        errors.append(f"{where}: {message}")
    return errors


def foreign_id_errors(settings: TicketSettings, data: GuildData) -> list[str]:
    """Ensure every role/channel ID belongs to this server.

    Without this check an admin could paste IDs from another server into the
    form, and e.g. JoltBot would post this server's transcripts there.

    Args:
        settings: Candidate ticket settings from the form.
        data: Live roles/channels for ``access.guild_id``.

    Returns:
        Error lines naming fields whose IDs are foreign or missing.
    """
    errors = []
    roles = {r.id: r for r in data.roles}
    text_ids = {c.id for c in data.text_channels()}
    category_ids = {c.id for c in data.categories()}

    role_fields = [
        ("Staff roles", settings.staff_role_ids, False),
        ("Ping roles", settings.ping_role_ids, False),
        *((label, ids, True) for label, ids in _granted_roles(settings)),
    ]
    for label, ids, assigned in role_fields:
        if any(i not in roles for i in ids):
            errors.append(f"{label}: a selected role isn't in this server. Refresh the page and pick again.")
        elif assigned and (managed := [roles[i].name for i in ids if roles[i].managed]):
            errors.append(f"{label}: {', '.join(managed)} is managed by an integration, so JoltBot can't assign it.")

    for label, channel_id, allowed in (
        ("Panel channel", settings.panel_channel_id, text_ids),
        ("Log channel", settings.log_channel_id, text_ids),
        ("Welcome channel", settings.welcome_channel_id, text_ids),
        ("Ticket category", settings.ticket_category_id, category_ids),
    ):
        if channel_id is not None and channel_id not in allowed:
            errors.append(f"{label}: that channel isn't in this server. Refresh the page and pick again.")
    return errors


def emoji_errors(settings: TicketSettings, data: GuildData) -> list[str]:
    """Check that every dropdown icon given by name is one of this server's emojis.

    A misspelt name would otherwise just show no icon, and the admin would not
    know why.

    Args:
        settings: Candidate ticket settings from the form.
        data: Live roles/channels/emojis for the guild.

    Returns:
        Error lines naming the question and option; empty if every name exists.
    """
    known = {emoji.name for emoji in data.emojis}
    errors = []
    for q, question in enumerate(settings.choice_questions, 1):
        for o, option in enumerate(question.options, 1):
            name = custom_emoji_name(option.emoji)
            if name and name not in known:
                errors.append(
                    f"Dropdown question {q}, option {o}: this server has no emoji called :{name}:. "
                    "Check the spelling (capital letters matter), or paste a normal emoji instead."
                )
    return errors


def _granted_roles(settings: TicketSettings) -> list[tuple[str, list[int]]]:
    """Every way JoltBot hands out a role, as (form label, role IDs). Unused ways are left out."""
    groups: dict[str, list[int]] = {}
    for role in settings.assignable_roles():
        groups.setdefault(role.group, []).append(role.role_id)
    return list(groups.items())


def powerful_role_errors(
    settings: TicketSettings,
    previous: TicketSettings,
    data: GuildData,
    *,
    actor_is_admin: bool,
    dashboard_role_ids: Collection[int] = (),
) -> list[str]:
    """Stop Manage Server alone from making JoltBot hand out a powerful role.

    In Discord, Manage Server doesn't let you assign roles. Without this check
    a dashboard user could set, say, a moderator role as the role given on
    accept and then obtain it through an application.

    A role that lets its holders use this dashboard (``dashboard_role_ids``)
    counts as powerful too, whatever its Discord permissions. Otherwise someone
    let in by such a role could have JoltBot hand the same access to anyone.

    Only *newly added* roles are checked, per field. A powerful role an admin
    already saved stays, so other managers can keep editing the page, but they
    can't move it to a wider use (e.g. from an extra choice to given-to-everyone).

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
    errors = []
    already_saved = dict(_granted_roles(previous))
    for label, new_ids in _granted_roles(settings):
        old_ids = already_saved.get(label, [])
        for role_id in dict.fromkeys(new_ids):
            if role_id in old_ids or role_id not in roles:
                continue
            if names := elevated_permission_names(roles[role_id].permissions):
                errors.append(
                    f"{label}: {roles[role_id].name} has powerful permissions ({', '.join(names)}). Only the "
                    "server owner or an Administrator can make JoltBot give out a role like that."
                )
            elif role_id in dashboard_role_ids:
                errors.append(
                    f"{label}: {roles[role_id].name} gives access to this dashboard. Only the "
                    "server owner or an Administrator can make JoltBot give out a role like that."
                )
    return errors


# Server-wide permissions the ticket module needs, as (bit, name). Bits come from the invite list.
NEEDED_PERMISSIONS = [
    (BOT_PERMISSIONS[name], name)
    for name in (
        "Manage Roles",
        "Manage Channels",
        "View Channels",
        "Send Messages",
        "Embed Links",
        "Attach Files",
        "Read Message History",
    )
]


def setup_warnings(settings: TicketSettings, data: GuildData) -> list[str]:
    """Soft problems visible from the dashboard (perms, hierarchy, deleted channels).

    Args:
        settings: Current or candidate ticket settings.
        data: Live Discord guild snapshot.

    Returns:
        Warning lines shown above the form (non-blocking).
    """
    warnings = []
    perms = data.bot_permissions()
    if not perms & ADMINISTRATOR:
        missing = [name for bit, name in NEEDED_PERMISSIONS if not perms & bit]
        if missing:
            warnings.append(
                f"JoltBot's role is missing these permissions: {', '.join(missing)}. "
                "Add them in Server Settings → Roles."
            )

    roles = {r.id: r for r in data.roles}
    top = data.bot_top_position()
    given = dict.fromkeys(r.role_id for r in settings.assignable_roles())  # each role once, in order
    too_high = [roles[i].name for i in given if i in roles and roles[i].position >= top]
    if too_high:
        warnings.append(
            f"JoltBot's role must be above {', '.join(too_high)} or it can't give or remove "
            "them. Drag JoltBot's role higher in Server Settings → Roles."
        )

    for label, ids in _granted_roles(settings):
        for role_id in dict.fromkeys(ids):
            if role_id in roles and (names := elevated_permission_names(roles[role_id].permissions)):
                warnings.append(
                    f"{label}: JoltBot gives out {roles[role_id].name}, which has powerful permissions "
                    f"({', '.join(names)}). Make sure that's intended."
                )

    channel_ids = {c.id for c in data.channels}
    for label, channel_id in (
        ("panel channel", settings.panel_channel_id),
        ("log channel", settings.log_channel_id),
        ("ticket category", settings.ticket_category_id),
    ):
        if channel_id and channel_id not in channel_ids:
            warnings.append(f"The {label} you picked was deleted. Pick a new one.")
    for label, ids in (("staff", settings.staff_role_ids), ("accept", settings.accept_role_ids)):
        if any(i not in roles for i in ids):
            warnings.append(f"One of the {label} roles was deleted. Review the roles below and save.")
    return warnings


def form_options(data: GuildData) -> dict[str, Any]:
    """Roles and channels in the shape the template needs (IDs as strings)."""
    top = data.bot_top_position()
    return {
        # "assignable": JoltBot can only give a role that sits below its own highest one.
        "roles": [{**role_option(r), "assignable": not r.managed and r.position < top} for r in data.roles],
        "text_channels": text_channel_options(data),
        "categories": [{"id": str(c.id), "name": c.name} for c in data.categories()],
    }


def message_fields(values: dict[str, Any]) -> list[dict[str, Any]]:
    """The message boxes for the template, one row per ``TicketMessages`` field.

    Args:
        values: The current message texts, by field name.

    Returns:
        One row per message: its label, help text, the placeholders it
        understands, whether it is multi-line, and its length limit.
    """
    limits = {key: field.metadata for key, field in TicketMessages.model_fields.items()}
    out = []
    for key in TicketMessages.model_fields:
        label, help_text = MESSAGE_INFO.get(key, (key, ""))
        max_length = next((m.max_length for m in limits[key] if hasattr(m, "max_length")), None)
        out.append(
            {
                "key": key,
                "label": label,
                "help": help_text,
                "value": values.get(key, ""),
                "placeholders": PLACEHOLDERS.get(key, ()),
                "multiline": key not in SINGLE_LINE_MESSAGES,
                "max_length": max_length,
            }
        )
    return out


def padded_rows(values: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Question rows padded to 5, and extra-role rows plus a few blank ones to fill in."""
    questions = list(values.get("form_questions") or [])
    blank_question = {
        "label": "",
        "placeholder": "",
        "required": True,
        "long": False,
        "max_length": DEFAULT_ANSWER_LENGTH,
    }
    questions += [blank_question] * (MAX_FORM_QUESTIONS - len(questions))
    extras = list(values.get("extra_role_choices") or [])
    extras += [{"label": "", "role_id": "", "emoji": ""}] * min(BLANK_EXTRA_ROWS, MAX_EXTRA_ROLE_CHOICES - len(extras))
    return questions[:MAX_FORM_QUESTIONS], extras


def _emoji_preview_url(emoji: str | None, data: GuildData) -> str | None:
    """Image address for an option icon that is one of the server's own emojis, to show beside the box."""
    if name := custom_emoji_name(emoji):
        return next((e.url for e in data.emojis if e.name == name), None)
    if emoji_id := custom_emoji_id(emoji):
        return next((e.url for e in data.emojis if e.id == emoji_id), None)
    return None  # a normal emoji is already visible in the box itself


def choice_rows(values: dict[str, Any], data: GuildData) -> list[dict[str, Any]]:
    """Dropdown questions for the form: the saved ones, then one empty block to add another.

    Each question gets a few empty option rows as well. Saving makes more
    appear, up to the limits, the same way extra role choices work.

    Args:
        values: Settings values (saved, or as typed when a save failed).
        data: Live guild snapshot, for previewing the server's own emojis.

    Returns:
        At most ``MAX_CHOICE_QUESTIONS`` blocks of ``{"label", "required", "options"}``.
    """
    blank_option = {"label": "", "role_id": "", "emoji": "", "emoji_url": None}
    blocks = []
    for question in list(values.get("choice_questions") or [])[:MAX_CHOICE_QUESTIONS]:
        options = [
            {**option, "emoji_url": _emoji_preview_url(option.get("emoji"), data)}
            for option in list(question.get("options") or [])[:MAX_CHOICE_OPTIONS]
        ]
        options += [blank_option] * min(BLANK_OPTION_ROWS, MAX_CHOICE_OPTIONS - len(options))
        required = question.get("required", True)
        blocks.append({"label": question.get("label", ""), "required": required, "options": options})
    if len(blocks) < MAX_CHOICE_QUESTIONS:
        blocks.append({"label": "", "required": True, "options": [blank_option] * BLANK_OPTION_ROWS})
    return blocks
