"""Per-server settings for the example module. Every field needs a default."""

from pydantic import Field

from joltbot.modules.base import ModuleSettingsModel


class HelloSettings(ModuleSettingsModel):
    # {user} and {server} are filled in when the message is sent.
    greeting: str = Field("Hello {user}, welcome to {server}!", min_length=1, max_length=200)
