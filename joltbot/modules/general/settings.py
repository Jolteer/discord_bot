"""Server-wide settings that don't belong to a single feature module."""
from pydantic import Field

from ..base import ModuleSettingsModel

DEFAULT_EMBED_COLOR = "#FFB800"  # JoltBot yellow


class GeneralSettings(ModuleSettingsModel):
    # Colour used for JoltBot's embeds in this server, as #RRGGBB.
    embed_color: str = Field(DEFAULT_EMBED_COLOR, pattern=r"^#[0-9A-Fa-f]{6}$")

    @property
    def embed_color_int(self) -> int:
        return int(self.embed_color[1:], 16)
