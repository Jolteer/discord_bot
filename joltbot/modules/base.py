"""Base class for every module's settings."""

from pydantic import BaseModel, ConfigDict


class ModuleSettingsModel(BaseModel):
    """A module's per-server settings.

    Every field must have a default. That is what lets a server with nothing
    configured yet load cleanly: missing keys simply take their defaults.
    """

    # extra="ignore": keys left over from an older version of the code are dropped
    # instead of causing an error when the settings are loaded.
    model_config = ConfigDict(extra="ignore")

    def setup_problems(self) -> list[str]:
        """Plain-language reasons the module can't be turned on yet. Empty = ready."""
        return []
