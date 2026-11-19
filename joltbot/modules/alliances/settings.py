"""Per-server settings for alliance join requests.

Imported by both the bot and the dashboard, so it must not import discord.py
or FastAPI. Nothing about a server's alliances is in the code: their names,
roles and channels all come from here.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from ...core.limits import EMBED_TITLE_MAX, SELECT_OPTION_LABEL_MAX
from ...core.types import OptionalSnowflake, Snowflake
from ..base import ModuleSettingsModel

# AssignableRole is the shape the dashboard's role rules read (see TicketSettings.assignable_roles).
from ..tickets.settings import AssignableRole

MAX_ALLIANCES = 10
MAX_REVIEWER_ROLES = 10
MAX_OVERRIDE_ROLES = 10
DEFAULT_REJECT_COOLDOWN_HOURS = 24
MAX_REJECT_COOLDOWN_HOURS = 24 * 30
PANEL_DESCRIPTION_MAX = 2000


class Alliance(BaseModel):
    """One alliance members can ask to join.

    Its member role doubles as its identity: requests store that role's ID, so
    renaming the alliance keeps its waiting requests.
    """

    name: str = Field(min_length=1, max_length=SELECT_OPTION_LABEL_MAX)
    member_role_id: Snowflake  # given when a request is accepted
    # Who may accept or reject this alliance's requests (e.g. its R5 and R4 roles).
    reviewer_role_ids: list[Snowflake] = Field(min_length=1, max_length=MAX_REVIEWER_ROLES)
    request_channel_id: Snowflake  # where its requests are posted, with the Accept / Reject buttons

    @field_validator("name")
    @classmethod
    def _trimmed(cls, name: str) -> str:
        """Drop spaces around the name; a name of only spaces is refused."""
        name = name.strip()
        if not name:
            raise ValueError("needs a name.")
        return name

    @field_validator("reviewer_role_ids")
    @classmethod
    def _no_duplicates(cls, ids: list[int]) -> list[int]:
        """Drop repeated role IDs, keeping the order they were picked in."""
        return list(dict.fromkeys(ids))


class AllianceSettings(ModuleSettingsModel):
    """Everything one server can set for alliance join requests.

    Stored as JSON in ``module_settings`` and edited on the dashboard's
    Alliances page. Every field has a default, so a server that never opened
    that page loads cleanly, with the module switched off.
    """

    alliances: list[Alliance] = Field(default_factory=list, max_length=MAX_ALLIANCES)
    # Roles that may decide any alliance's requests and run the /alliances commands (e.g. King, moderators).
    override_role_ids: list[Snowflake] = Field(default_factory=list, max_length=MAX_OVERRIDE_ROLES)

    panel_channel_id: OptionalSnowflake = None  # where /alliances panel posts the "choose your alliance" menu
    log_channel_id: OptionalSnowflake = None  # every decision is posted here

    # How long a rejected member waits before asking the same alliance again. 0 = no wait.
    reject_cooldown_hours: int = Field(DEFAULT_REJECT_COOLDOWN_HOURS, ge=0, le=MAX_REJECT_COOLDOWN_HOURS)

    # The panel's text. {server} is filled in when it is posted.
    panel_title: str = Field("⚔️ Choose your alliance", min_length=1, max_length=EMBED_TITLE_MAX)
    panel_description: str = Field(
        "Pick the alliance you are in from the menu below, then enter your in-game name and governor ID.\n\n"
        "That alliance's leadership checks you against the in-game roster. "
        "Once they accept, you get access to its channels.",
        min_length=1,
        max_length=PANEL_DESCRIPTION_MAX,
    )

    @field_validator("override_role_ids")
    @classmethod
    def _no_duplicates(cls, ids: list[int]) -> list[int]:
        """Drop repeated role IDs, keeping the order they were picked in."""
        return list(dict.fromkeys(ids))

    @model_validator(mode="after")
    def _check_alliances(self) -> AllianceSettings:
        """Rules between alliances: each is told apart by name and role, and no member role can decide requests."""
        names = [alliance.name.casefold() for alliance in self.alliances]
        if len(names) != len(set(names)):
            raise ValueError("Each alliance needs a different name.")
        if len(self.member_role_ids) != len(self.alliances):
            raise ValueError("Each alliance needs a different member role.")
        # A member role that could also decide requests would let every member of
        # that alliance approve the next one, which is self-service with one extra step.
        deciding = set(self.override_role_ids).union(*(a.reviewer_role_ids for a in self.alliances))
        for alliance in self.alliances:
            if alliance.member_role_id in deciding:
                raise ValueError(f"The member role of {alliance.name} can't also be a reviewer or override role.")
        return self

    @property
    def member_role_ids(self) -> set[int]:
        """The member role of every alliance. A member should hold at most one of these."""
        return {alliance.member_role_id for alliance in self.alliances}

    def alliance_for(self, member_role_id: int) -> Alliance | None:
        """The alliance whose member role this is, under the settings as they are now.

        Args:
            member_role_id: The role ID stored with a request or sent by the panel's menu.

        Returns:
            The alliance, or ``None`` if it was removed (or given another role) since.
        """
        return next((a for a in self.alliances if a.member_role_id == member_role_id), None)

    def assignable_roles(self) -> list[AssignableRole]:
        """Every role JoltBot gives or takes away for this module: the alliances' member roles.

        The dashboard reads this list for its role-order warning, its "powerful
        role" rule and its "can't be a dashboard access role" rule.
        """
        return [
            AssignableRole("Alliance member roles", f"Member role of {alliance.name}", alliance.member_role_id)
            for alliance in self.alliances
        ]

    def setup_problems(self) -> list[str]:
        """What is still missing before alliance requests can be switched on, as sentences for the admin."""
        problems = []
        if not self.alliances:
            problems.append("Add at least one alliance.")
        if not self.panel_channel_id:
            problems.append("Pick the channel where the alliance menu is posted.")
        if not self.log_channel_id:
            problems.append("Pick a log channel for decisions.")
        return problems
