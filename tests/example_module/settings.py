"""Per-server settings for the example module. Every field needs a default."""

from pydantic import Field

from joltbot.modules.base import ModuleSettingsModel


class HelloSettings(ModuleSettingsModel):
    """What one server can set for the hello module."""

    # {user} and {server} are filled in when the message is sent.
    greeting: str = Field("Hello {user}, welcome to {server}!", min_length=1, max_length=200)
