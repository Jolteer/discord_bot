"""Builds the FastAPI app. Tests call create_app() with a fake Discord client."""

from __future__ import annotations

import importlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..core.config import AppConfig, get_config
from ..core.db import Database
from ..core.discord_rest import DiscordClient
from ..core.schema import schema_problem
from ..modules.registry import MODULES
from .rendering import STATIC_DIR, css_built, render
from .routes import auth, guild, health, pages
from .security import LoginRequired

log = logging.getLogger(__name__)

# Content-Security-Policy: the browser may only run scripts and load styles from our own site.
# Tailwind's CDN build is allowed only while app.css hasn't been built (local development).
TAILWIND_CDN = "https://cdn.jsdelivr.net"


def content_security_policy(allow_cdn: bool) -> str:
    """The CSP header value; allow_cdn adds the Tailwind CDN to script-src."""
    scripts = f"'self' {TAILWIND_CDN}" if allow_cdn else "'self'"
    # fonts.googleapis.com serves the CSS; fonts.gstatic.com serves the font files.
    styles = "'self' 'unsafe-inline' https://fonts.googleapis.com"
    return (
        "default-src 'self'; "
        f"script-src {scripts}; "
        f"style-src {styles}; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https://cdn.discordapp.com; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )


def create_app(
    config: AppConfig | None = None,
    db: Database | None = None,
    discord: DiscordClient | None = None,
    *,
    check_schema: bool = True,
) -> FastAPI:
    """Build the dashboard app. Anything not passed in is created from the environment."""
    config = config or get_config()
    db = db or Database(config.database_url)
    discord = discord or DiscordClient(
        client_id=config.discord_client_id,
        client_secret=config.discord_client_secret,
        bot_token=config.discord_token,
        redirect_uri=config.oauth_redirect_uri,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if missing := config.missing("discord_token", "discord_client_id", "discord_client_secret", "session_secret"):
            raise RuntimeError(f"Missing in .env: {', '.join(missing)} (see .env.example).")
        if check_schema and (problem := await schema_problem(db)):
            raise RuntimeError(problem)
        log.info("Dashboard ready at %s", config.public_base_url)
        yield
        await discord.aclose()
        await db.dispose()

    # docs/openapi off: there's no public JSON API to document, and it would list every route.
    app = FastAPI(title="JoltBot", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.config, app.state.db, app.state.discord = config, db, discord

    @app.middleware("http")
    async def security_headers(request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", content_security_policy(not css_built()))
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if config.uses_https:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(health.router)
    app.include_router(pages.router)
    app.include_router(auth.router)
    app.include_router(guild.router)
    for module in MODULES.values():  # each module adds its own dashboard pages
        if module.web:
            app.include_router(importlib.import_module(module.web).router)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        if request.headers.get("hx-request"):
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        titles = {403: "Not allowed", 404: "Not found", 503: "Discord is unavailable"}
        message = exc.detail if exc.status_code != 404 or exc.detail != "Not Found" else "That page doesn't exist."
        return await render(
            request,
            "error.html",
            {"title": titles.get(exc.status_code, "Something went wrong"), "message": message},
            status_code=exc.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def _bad_url(request: Request, exc: RequestValidationError):
        # e.g. /servers/not-a-number
        return await render(
            request, "error.html", {"title": "Not found", "message": "That page doesn't exist."}, status_code=404
        )

    return app
