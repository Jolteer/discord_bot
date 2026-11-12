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
LogFormat = Literal["text", "json"]


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
    # "text" = readable lines; "json" = one JSON object per line, for log tools.
    log_format: LogFormat = "text"
    # Discord webhook that receives ERROR-level log lines (core/alerts.py). Empty = no alerts.
    # A secret: anyone who has the URL can post to that channel.
    alert_webhook_url: str = ""

    @field_validator("public_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @field_validator("alert_webhook_url")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("log_format", mode="before")
    @classmethod
    def _lower(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @property
    def oauth_redirect_uri(self) -> str:
        """OAuth2 redirect registered in the Discord Developer Portal.

        Returns:
            ``PUBLIC_BASE_URL`` + ``/auth/callback``.
        """
        return f"{self.public_base_url}/auth/callback"

    @property
    def uses_https(self) -> bool:
        """Whether the dashboard is served over HTTPS (secure cookies / HSTS).

        Returns:
            True when ``PUBLIC_BASE_URL`` starts with ``https://``.
        """
        return self.public_base_url.startswith("https://")

    def missing(self, *names: str) -> list[str]:
        """Return env var names for required settings that are empty.

        Args:
            *names: Attribute names on this config (e.g. ``"discord_token"``).

        Returns:
            Uppercase env names that are missing (e.g. ``["DISCORD_TOKEN"]``).
        """
        return [name.upper() for name in names if not getattr(self, name)]


@lru_cache
def get_config() -> AppConfig:
    """Load settings once per process from the environment / ``.env``.

    Returns:
        Shared ``AppConfig`` instance (cached).
    """
    return AppConfig()
