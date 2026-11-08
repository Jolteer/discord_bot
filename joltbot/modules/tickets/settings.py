"""Per-server settings for the ticket / verification module.

Imported by both the bot and the dashboard, so it must not import discord.py
or FastAPI. Length limits match Discord's own limits, so anything an admin
saves here can actually be sent to Discord.
"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from ...core.types import OptionalSnowflake, Snowflake
from ..base import ModuleSettingsModel


class FormQuestion(BaseModel):
    """One question in the application form (a Discord modal allows at most 5)."""

    label: str = Field(min_length=1, max_length=45)
    placeholder: str = Field("", max_length=100)
    required: bool = True
    long: bool = False  # multi-line answer box
    max_length: int = Field(200, ge=1, le=1000)


class ExtraRoleChoice(BaseModel):
    """An optional role staff can pick when accepting someone (e.g. a team or alliance)."""

    label: str = Field(min_length=1, max_length=100)
    role_id: Snowflake
    emoji: str | None = Field(None, max_length=64)


class TicketMessages(BaseModel):
    """Every member-facing text, customisable per server.

    Placeholders (see PLACEHOLDERS below): {user}, {user_name}, {server},
    {channel}, {reason}, {role}, {role_line}.
    """

    panel_title: str = Field("📝 Verification", min_length=1, max_length=256)
    panel_description: str = Field(
        "Welcome to **{server}**!\n\n"
        "Click **Open Application** below and answer a few quick questions. "
        "A private ticket will open where staff will review your application.",
        min_length=1,
        max_length=4000,
    )
    panel_button_label: str = Field("Open Application", min_length=1, max_length=80)
    form_title: str = Field("Application", min_length=1, max_length=45)
    ticket_opened: str = Field("✅ Your application ticket is open: {channel}", min_length=1, max_length=2000)
    ticket_intro: str = Field(
        "Thanks for applying, {user}! 👋\n\nStaff will review your application here.",
        min_length=1,
        max_length=2000,
    )
    not_staff: str = Field("Only staff can do this.", min_length=1, max_length=2000)
    already_verified: str = Field("You're already verified. Welcome back! 🎉", min_length=1, max_length=2000)
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
    deny_dm_kept_suffix: str = Field(
        "\n\nYou can open a new application if your situation changes.", max_length=500
    )
    deny_reason_hint: str = Field("e.g. We couldn't verify your details.", max_length=100)


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
    return [
        FormQuestion(label="How did you find this server?", max_length=200),
        FormQuestion(
            label="Anything else staff should know?",
            required=False,
            long=True,
            max_length=1000,
        ),
    ]


class TicketSettings(ModuleSettingsModel):
    # --- who can accept/deny/close tickets ---
    staff_role_ids: list[Snowflake] = Field(default_factory=list, max_length=25)
    allow_administrators: bool = True  # members with Administrator count as staff

    # --- channels ---
    panel_channel_id: OptionalSnowflake = None  # where the "Open Application" panel lives
    ticket_category_id: OptionalSnowflake = None  # new ticket channels are created here
    log_channel_id: OptionalSnowflake = None  # transcripts + decisions are posted here
    # An extra channel pending members are allowed to see (only used by the diagnostics check).
    welcome_channel_id: OptionalSnowflake = None

    # --- roles ---
    pending_role_id: OptionalSnowflake = None  # given on join, removed on accept (e.g. "Unverified")
    accept_role_ids: list[Snowflake] = Field(default_factory=list, max_length=10)  # given on accept
    extra_role_choices: list[ExtraRoleChoice] = Field(default_factory=list, max_length=24)

    # --- new-ticket ping ---
    ping_enabled: bool = True
    ping_role_ids: list[Snowflake] = Field(default_factory=list, max_length=10)  # empty = ping staff roles

    # --- application form ---
    form_questions: list[FormQuestion] = Field(default_factory=_default_questions, min_length=1, max_length=5)
    require_attachment: bool = False  # warn staff on Accept if the applicant uploaded no file

    # --- behaviour ---
    delete_delay_seconds: int = Field(10, ge=0, le=3600)
    reupload_attachments: bool = True  # copy files to the log channel before the ticket is deleted
    assign_pending_on_startup: bool = False  # give the pending role to people who joined while offline

    messages: TicketMessages = Field(default_factory=TicketMessages)

    @field_validator("staff_role_ids", "accept_role_ids", "ping_role_ids")
    @classmethod
    def _no_duplicates(cls, ids: list[int]) -> list[int]:
        return list(dict.fromkeys(ids))  # keeps order, drops repeats

    @model_validator(mode="after")
    def _check_roles(self) -> "TicketSettings":
        if self.pending_role_id is not None and self.pending_role_id in self.accept_role_ids:
            raise ValueError("The pending role can't also be a role given on accept.")
        extra_ids = [c.role_id for c in self.extra_role_choices]
        if len(extra_ids) != len(set(extra_ids)):
            raise ValueError("Each extra role choice must use a different role.")
        return self

    @property
    def effective_ping_role_ids(self) -> list[int]:
        """Roles to ping on a new ticket. No ping roles set = ping the staff roles."""
        if not self.ping_enabled:
            return []
        return self.ping_role_ids or self.staff_role_ids

    def setup_problems(self) -> list[str]:
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
