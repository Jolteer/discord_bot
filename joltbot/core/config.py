"""Secrets and deployment settings, read from environment variables or .env.

This is the only global configuration. Everything about a particular Discord
server lives in the database, keyed by guild ID.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class AppConfig(BaseSettings):
    """Every setting JoltBot reads from the environment. See .env.example."""

    # Field names map to env vars case-insensitively: discord_token <- DISCORD_TOKEN.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    discord_token: str = ""
    discord_client_id: str = ""
    discord_client_secret: str = ""
    session_secret: str = ""
    public_base_url: str = "http://localhost:8000"
    database_url: str = "sqlite+aiosqlite:///data/joltbot.db"

    # Where the dashboard listens. Inside Docker HOST must be 0.0.0.0 so other containers can reach it.
    host: str = "127.0.0.1"
    port: int = Field(8000, ge=1, le=65535)
    # Proxies whose X-Forwarded-* headers are trusted: comma-separated IPs, or "*".
    forwarded_allow_ips: str = "127.0.0.1"

    log_level: LogLevel = "INFO"
    # Folder for rotating log files. Empty = console only (Docker collects the console output).
    log_dir: str = "logs"

    @field_validator("public_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @property
    def oauth_redirect_uri(self) -> str:
        return f"{self.public_base_url}/auth/callback"

    @property
    def uses_https(self) -> bool:
        return self.public_base_url.startswith("https://")

    def missing(self, *names: str) -> list[str]:
        """Names (as env vars) of required settings that are empty."""
        return [name.upper() for name in names if not getattr(self, name)]


@lru_cache
def get_config() -> AppConfig:
    """The settings, read once per process."""
    return AppConfig()
