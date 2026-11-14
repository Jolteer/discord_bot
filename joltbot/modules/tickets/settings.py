"""Per-server settings for the ticket / verification module.

Imported by both the bot and the dashboard, so it must not import discord.py
or FastAPI. Length limits match Discord's own limits, so anything an admin
saves here can actually be sent to Discord.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from pydantic import BaseModel, Field, field_validator, model_validator

from ...core.limits import (
    BUTTON_LABEL_MAX,
    EMBED_TITLE_MAX,
    MESSAGE_MAX,
    MODAL_MAX_INPUTS,
    MODAL_TITLE_MAX,
    SELECT_MAX_OPTIONS,
    SELECT_OPTION_LABEL_MAX,
    TEXT_INPUT_LABEL_MAX,
    TEXT_INPUT_PLACEHOLDER_MAX,
)
from ...core.types import OptionalSnowflake, Snowflake
from ..base import ModuleSettingsModel

# How much of each thing one server may set up. Most follow from Discord's limits.
MAX_FORM_QUESTIONS = MODAL_MAX_INPUTS
# Dropdown questions are shown in one Discord message, which has 5 rows; one row
# holds the Continue button.
MAX_CHOICE_QUESTIONS = 4
MAX_CHOICE_OPTIONS = 10
# The Accept prompt's dropdown also holds a "No extra role" entry.
MAX_EXTRA_ROLE_CHOICES = SELECT_MAX_OPTIONS - 1
MAX_STAFF_ROLES = 25
MAX_ACCEPT_ROLES = 10
MAX_PING_ROLES = 10
QUESTION_LABEL_MAX = TEXT_INPUT_LABEL_MAX
EMOJI_SETTING_MAX = 64  # room for Discord's full <a:name:id> form
DEFAULT_ANSWER_LENGTH = 200  # characters an applicant may type, unless the admin changes it
MAX_ANSWER_LENGTH = 1000
DEFAULT_DELETE_DELAY_SECONDS = 10  # how long a closed ticket channel stays before it is deleted
MAX_DELETE_DELAY_SECONDS = 3600


class FormQuestion(BaseModel):
    """One question in the application form (a Discord modal allows at most 5)."""

    label: str = Field(min_length=1, max_length=QUESTION_LABEL_MAX)
    placeholder: str = Field("", max_length=TEXT_INPUT_PLACEHOLDER_MAX)
    required: bool = True
    long: bool = False  # multi-line answer box
    max_length: int = Field(DEFAULT_ANSWER_LENGTH, ge=1, le=MAX_ANSWER_LENGTH)


class ExtraRoleChoice(BaseModel):
    """An optional role staff can pick when accepting someone (e.g. a team or alliance)."""

    label: str = Field(min_length=1, max_length=SELECT_OPTION_LABEL_MAX)
    role_id: Snowflake
    emoji: str | None = Field(None, max_length=EMOJI_SETTING_MAX)


_EMOJI_NAME = re.compile(r":?([A-Za-z0-9_]{2,32}):?")
_EMOJI_MARKUP = re.compile(r"<a?:[A-Za-z0-9_]{2,32}:(\d{15,20})>")


def custom_emoji_name(text: str | None) -> str | None:
    """The emoji's name, if ``text`` names one of the server's own emojis.

    Admins type the name the way Discord shows it, ``infantry`` or
    ``:infantry:``. The bot looks the emoji up in the server by that name when
    it builds a dropdown.

    Args:
        text: An option's ``emoji`` setting.

    Returns:
        The bare name, or ``None`` if ``text`` is empty, a normal emoji, or
        Discord's full ``<:name:id>`` form.
    """
    match = _EMOJI_NAME.fullmatch((text or "").strip())
    return match.group(1) if match else None


def custom_emoji_id(text: str | None) -> int | None:
    """The emoji's ID, if ``text`` is Discord's full ``<:name:id>`` form."""
    match = _EMOJI_MARKUP.fullmatch((text or "").strip())
    return int(match.group(1)) if match else None


class ChoiceOption(BaseModel):
    """One option of a dropdown question. Picking it can give a role when the ticket is accepted."""

    label: str = Field(min_length=1, max_length=SELECT_OPTION_LABEL_MAX)
    role_id: OptionalSnowflake = None  # None = just an answer, no role
    # Icon shown next to the option: a normal emoji, or the name of one of the server's own.
    emoji: str | None = Field(None, max_length=EMOJI_SETTING_MAX)

    @field_validator("emoji", mode="before")
    @classmethod
    def _blank_means_no_icon(cls, value: object) -> object:
        """An icon box left empty, or holding only spaces, means no icon."""
        return (value.strip() or None) if isinstance(value, str) else value

    @field_validator("emoji")
    @classmethod
    def _looks_like_an_emoji(cls, value: str | None) -> str | None:
        """Reject plain words as an icon."""
        # Plain words are not an emoji, and Discord rejects a whole message over a bad one.
        if value and value.isascii() and not (custom_emoji_name(value) or custom_emoji_id(value)):
            raise ValueError(
                "The icon must be a normal emoji, or the name of one of this server's emojis (like :infantry:)."
            )
        return value


class ChoiceQuestion(BaseModel):
    """A question the applicant answers by picking from a list, before the form.

    For anything with a fixed set of answers (a team, a region, a playstyle),
    where each answer can map to a role.
    """

    label: str = Field(min_length=1, max_length=QUESTION_LABEL_MAX)
    required: bool = True
    options: list[ChoiceOption] = Field(min_length=2, max_length=MAX_CHOICE_OPTIONS)

    @model_validator(mode="after")
    def _distinct_options(self) -> ChoiceQuestion:
        """Two options with the same label couldn't be told apart in the dropdown."""
        labels = [option.label.casefold() for option in self.options]
        if len(labels) != len(set(labels)):
            raise ValueError("Each option needs a different label.")
        return self


class AssignableRole(NamedTuple):
    """One role JoltBot hands out for this server, and where in the settings it comes from."""

    group: str  # the dashboard field, e.g. "Roles given on accept"
    description: str  # this one role, e.g. "Extra role 'Team A'"
    role_id: int


class TicketMessages(BaseModel):
    """Every member-facing text, customisable per server.

    Placeholders (see PLACEHOLDERS below): {user}, {user_name}, {server},
    {channel}, {reason}, {role}, {role_line}.
    """

    panel_title: str = Field("📝 Verification", min_length=1, max_length=EMBED_TITLE_MAX)
    panel_description: str = Field(
        "Welcome to **{server}**!\n\n"
        "Click **Open Application** below and answer a few quick questions. "
        "A private ticket will open where staff will review your application.",
        min_length=1,
        max_length=4000,
    )
    panel_button_label: str = Field("Open Application", min_length=1, max_length=BUTTON_LABEL_MAX)
    form_title: str = Field("Application", min_length=1, max_length=MODAL_TITLE_MAX)
    ticket_opened: str = Field("✅ Your application ticket is open: {channel}", min_length=1, max_length=MESSAGE_MAX)
    ticket_intro: str = Field(
        "Thanks for applying, {user}! 👋\n\nStaff will review your application here.",
        min_length=1,
        max_length=MESSAGE_MAX,
    )
    not_staff: str = Field("Only staff can do this.", min_length=1, max_length=MESSAGE_MAX)
    already_verified: str = Field("You're already verified. Welcome back! 🎉", min_length=1, max_length=MESSAGE_MAX)
    welcome_dm: str = Field(
        "✅ Your application to **{server}** was accepted! Welcome aboard.\n{role_line}"
        "You now have access to the server.",
        min_length=1,
        max_length=3000,
    )
    deny_dm: str = Field(
        "❌ Your application to **{server}** was not accepted.\n\n**Reason:** {reason}",
        min_length=1,
        max_length=3000,
    )
    deny_dm_kicked_suffix: str = Field("\n\nYou have been removed from the server.", max_length=500)
    deny_dm_kept_suffix: str = Field("\n\nYou can open a new application if your situation changes.", max_length=500)
    deny_reason_hint: str = Field("e.g. We couldn't verify your details.", max_length=TEXT_INPUT_PLACEHOLDER_MAX)


# Which placeholders each message understands. Used by the dashboard's preview and help text.
PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    "panel_title": ("server",),
    "panel_description": ("server",),
    "panel_button_label": (),
    "form_title": (),
    "ticket_opened": ("user", "user_name", "server", "channel"),
    "ticket_intro": ("user", "user_name", "server"),
    "not_staff": ("server",),
    "already_verified": ("user", "user_name", "server"),
    "welcome_dm": ("user", "user_name", "server", "role", "role_line"),
    "deny_dm": ("user", "user_name", "server", "reason"),
    "deny_dm_kicked_suffix": ("server",),
    "deny_dm_kept_suffix": ("server",),
    "deny_reason_hint": (),
}


def _default_questions() -> list[FormQuestion]:
    """The two questions a server starts with, until an admin writes their own."""
    return [
        FormQuestion(label="How did you find this server?", max_length=DEFAULT_ANSWER_LENGTH),
        FormQuestion(
            label="Anything else staff should know?",
            required=False,
            long=True,
            max_length=MAX_ANSWER_LENGTH,
        ),
    ]


class TicketSettings(ModuleSettingsModel):
    """Everything one server can set for tickets and verification.

    Stored as JSON in ``module_settings`` and edited on the dashboard's Tickets
    page. Every field has a default, so a server that never opened that page
    loads cleanly, with the module switched off.
    """

    # --- who can accept/deny/close tickets ---
    staff_role_ids: list[Snowflake] = Field(default_factory=list, max_length=MAX_STAFF_ROLES)
    allow_administrators: bool = True  # members with Administrator count as staff

    # --- channels ---
    panel_channel_id: OptionalSnowflake = None  # where the "Open Application" panel lives
    ticket_category_id: OptionalSnowflake = None  # new ticket channels are created here
    log_channel_id: OptionalSnowflake = None  # transcripts + decisions are posted here
    # An extra channel pending members are allowed to see (only used by the diagnostics check).
    welcome_channel_id: OptionalSnowflake = None

    # --- roles ---
    pending_role_id: OptionalSnowflake = None  # given on join, removed on accept (e.g. "Unverified")
    accept_role_ids: list[Snowflake] = Field(default_factory=list, max_length=MAX_ACCEPT_ROLES)  # given on accept
    extra_role_choices: list[ExtraRoleChoice] = Field(default_factory=list, max_length=MAX_EXTRA_ROLE_CHOICES)

    # --- new-ticket ping ---
    ping_enabled: bool = True
    ping_role_ids: list[Snowflake] = Field(default_factory=list, max_length=MAX_PING_ROLES)  # empty = ping staff roles

    # --- application form ---
    form_questions: list[FormQuestion] = Field(
        default_factory=_default_questions, min_length=1, max_length=MAX_FORM_QUESTIONS
    )
    require_attachment: bool = False  # warn staff on Accept if the applicant uploaded no file
    # Dropdowns the applicant fills in before the form (see MAX_CHOICE_QUESTIONS for why 4).
    choice_questions: list[ChoiceQuestion] = Field(default_factory=list, max_length=MAX_CHOICE_QUESTIONS)

    # --- behaviour ---
    delete_delay_seconds: int = Field(DEFAULT_DELETE_DELAY_SECONDS, ge=0, le=MAX_DELETE_DELAY_SECONDS)
    reupload_attachments: bool = True  # copy files to the log channel before the ticket is deleted
    assign_pending_on_startup: bool = False  # give the pending role to people who joined while offline

    messages: TicketMessages = Field(default_factory=TicketMessages)

    @field_validator("staff_role_ids", "accept_role_ids", "ping_role_ids")
    @classmethod
    def _no_duplicates(cls, ids: list[int]) -> list[int]:
        """Drop repeated role IDs, keeping the order they were picked in."""
        return list(dict.fromkeys(ids))

    @model_validator(mode="after")
    def _check_roles(self) -> TicketSettings:
        """Rules between fields: the pending role is never also given, and extra roles don't repeat."""
        if self.pending_role_id is not None and self.pending_role_id in self.accept_role_ids:
            raise ValueError("The pending role can't also be a role given on accept.")
        extra_ids = [c.role_id for c in self.extra_role_choices]
        if len(extra_ids) != len(set(extra_ids)):
            raise ValueError("Each extra role choice must use a different role.")
        if self.pending_role_id is not None and self.pending_role_id in self.choice_role_ids:
            raise ValueError("The pending role can't also be given by a dropdown option.")
        return self

    @property
    def choice_role_ids(self) -> set[int]:
        """Every role a dropdown option gives."""
        return {o.role_id for q in self.choice_questions for o in q.options if o.role_id is not None}

    def picked_roles(self, answers: list[dict[str, str]] | None) -> tuple[list[tuple[str, int]], list[str]]:
        """Which roles an application's dropdown picks give, under the settings as they are now.

        The role ID stored with an answer is only what the applicant asked for
        when they applied. It is given only if it is still the role of some
        dropdown option, so removing a role from the options on the dashboard
        also stops it for tickets that are already open.

        Args:
            answers: The ticket's stored answers.

        Returns:
            ``(granted, dropped)``: ``granted`` holds ``("Question: Option", role_id)``
            for each pick that still gives a role; ``dropped`` names the picks
            whose role is no longer set up.
        """
        allowed = self.choice_role_ids
        granted: list[tuple[str, int]] = []
        dropped: list[str] = []
        for answer in answers or []:
            raw = str(answer.get("role_id") or "")
            if not raw:
                continue
            name = f"{answer.get('label', '?')}: {answer.get('value', '?')}"
            if raw.isdigit() and int(raw) in allowed:
                granted.append((name, int(raw)))
            else:
                dropped.append(name)
        return granted, dropped

    def assignable_roles(self) -> list[AssignableRole]:
        """Every role JoltBot gives or takes away in this server, in dashboard order.

        This is the one list behind the role-order checks, the diagnostics and
        the dashboard's "powerful role" rule. A new way of handing out a role
        must be added here, so that it gets all of those checks.

        Returns:
            One entry per role. Staff and ping roles are not included: JoltBot
            reads those but never assigns them.
        """
        roles = [AssignableRole("Roles given on accept", "Role given on accept", rid) for rid in self.accept_role_ids]
        if self.pending_role_id is not None:
            roles.append(AssignableRole("Pending role", "Pending role", self.pending_role_id))
        roles += [
            AssignableRole("Extra role choices", f"Extra role '{c.label}'", c.role_id) for c in self.extra_role_choices
        ]
        roles += [
            AssignableRole("Dropdown questions", f"Dropdown role '{q.label}: {o.label}'", o.role_id)
            for q in self.choice_questions
            for o in q.options
            if o.role_id is not None
        ]
        return roles

    @property
    def effective_ping_role_ids(self) -> list[int]:
        """Roles to ping on a new ticket. No ping roles set = ping the staff roles."""
        if not self.ping_enabled:
            return []
        return self.ping_role_ids or self.staff_role_ids

    def setup_problems(self) -> list[str]:
        """What is still missing before tickets can be switched on, as sentences for the admin."""
        problems = []
        if not self.panel_channel_id:
            problems.append("Pick the channel where the application panel is posted.")
        if not self.ticket_category_id:
            problems.append("Pick a category for ticket channels.")
        if not self.log_channel_id:
            problems.append("Pick a log channel for transcripts.")
        if not self.accept_role_ids:
            problems.append("Pick at least one role to give accepted members.")
        if not self.staff_role_ids and not self.allow_administrators:
            problems.append("Pick at least one staff role, or let Administrators act as staff.")
        return problems
