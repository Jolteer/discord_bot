"""Server-wide settings that don't belong to a single feature module."""

from pydantic import Field

from ...core.types import OptionalSnowflake, Snowflake
from ..base import ModuleSettingsModel

# This module's key in the registry and in module_settings. Stored in the database: never change it.
MODULE = "general"
DEFAULT_EMBED_COLOR = "#FFB800"  # JoltBot yellow
MAX_DASHBOARD_ROLES = 25


class GeneralSettings(ModuleSettingsModel):
    """Server-wide settings shared by feature modules.

    Attributes:
        embed_color: Colour used for JoltBot embeds in this server (``#RRGGBB``).
        notice_channel_id: Optional channel where JoltBot posts each new problem
            it runs into (the same ones shown as dashboard warnings).
        dashboard_role_ids: Roles whose members may use this server's dashboard
            without having Manage Server.
    """

    embed_color: str = Field(DEFAULT_EMBED_COLOR, pattern=r"^#[0-9A-Fa-f]{6}$")
    # Where JoltBot tells admins about problems in Discord itself. None = dashboard only.
    notice_channel_id: OptionalSnowflake = None
    # Members holding any of these roles may open this server's dashboard and change
    # its settings. Only changed on the "Dashboard access" page, which needs Manage
    # Server; the General form never touches it (see modules/general/web.py).
    dashboard_role_ids: list[Snowflake] = Field(default_factory=list, max_length=MAX_DASHBOARD_ROLES)

    @property
    def embed_color_int(self) -> int:
        """Integer form of ``embed_color`` for ``discord.Colour``.

        Returns:
            0xRRGGBB integer.
        """
        return int(self.embed_color[1:], 16)
