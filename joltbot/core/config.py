"""Secrets and deployment settings, read from environment variables or .env.

This is the only global configuration. Everything about a particular Discord
server lives in the database, keyed by guild ID.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    # Field names map to env vars case-insensitively: discord_token <- DISCORD_TOKEN.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    discord_token: str = ""
    discord_client_id: str = ""
    discord_client_secret: str = ""
    session_secret: str = ""
    public_base_url: str = "http://localhost:8000"
    database_url: str = "sqlite+aiosqlite:///data/joltbot.db"

    @field_validator("public_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.strip().rstrip("/")

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
    return AppConfig()
