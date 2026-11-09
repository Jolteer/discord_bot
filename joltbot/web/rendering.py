"""Jinja2 templates. Routes build plain data and call render(); templates only display it.

Keeping logic out of templates means a JSON API (and a React frontend) could
later reuse the same route logic and services without rewriting them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape

from ..core.permissions import bot_invite_url
from ..modules.registry import MODULES
from .security import get_config, optional_session

WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"
MODULES_DIR = WEB_DIR.parent / "modules"

# Shared templates first, then each module's own templates/ folder.
_template_dirs = [WEB_DIR / "templates"] + [
    MODULES_DIR / name / "templates" for name in MODULES if (MODULES_DIR / name / "templates").is_dir()
]
_env = Environment(
    loader=ChoiceLoader([FileSystemLoader(str(d)) for d in _template_dirs]),
    autoescape=select_autoescape(["html"]),  # escapes user/Discord text so it can't inject HTML
)
templates = Jinja2Templates(env=_env)


def css_built() -> bool:
    """True once the Tailwind stylesheet has been built (always true in the Docker image)."""
    return (STATIC_DIR / "app.css").exists()


def nav_modules() -> list[dict[str, str]]:
    """Sidebar entries for every registered module.

    Returns:
        List of ``{"name", "title"}`` dicts for guild layout links.
    """
    return [{"name": m.name, "title": m.title} for m in MODULES.values()]


async def render(request: Request, template: str, context: dict[str, Any] | None = None, status_code: int = 200):
    """Render a Jinja template with shared base context (user, CSRF, invite URL).

    Args:
        request: Incoming HTTP request.
        template: Template path relative to the ChoiceLoader roots.
        context: Page-specific variables.
        status_code: HTTP status for the response.

    Returns:
        HTML ``TemplateResponse``.
    """
    ws = await optional_session(request)
    config = get_config(request)
    base = {
        "user": ws,
        "csrf_token": ws.csrf_token if ws else "",
        # Built Tailwind CSS (production) vs the Tailwind CDN script (development fallback).
        "css_built": css_built(),
        "htmx_available": (STATIC_DIR / "vendor" / "htmx.min.js").exists(),
        "invite_url": bot_invite_url(config.discord_client_id) if config.discord_client_id else None,
        "nav_modules": nav_modules(),
    }
    return templates.TemplateResponse(request, template, {**base, **(context or {})}, status_code=status_code)
