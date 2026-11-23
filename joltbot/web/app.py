"""Builds the FastAPI app. Tests call create_app() with a fake Discord client."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import RequestResponseEndpoint

from ..core.config import AppConfig, get_config
from ..core.db import Database
from ..core.discord_rest import DiscordClient
from ..core.schema import schema_problem
from ..modules.registry import MODULES
from .rendering import STATIC_DIR, css_built, render
from .routes import auth, guild, health, pages
from .security import LoginRequired
from .sessions import purge_expired_forever
from .watchdog import BotWatchdog

log = logging.getLogger(__name__)

# Content-Security-Policy: the browser may only run scripts and load styles from our own site.
# Tailwind's CDN build is allowed only while app.css hasn't been built (local development).
TAILWIND_CDN = "https://cdn.jsdelivr.net"
# How long browsers remember to use HTTPS only for this site: one year.
HSTS_MAX_AGE_SECONDS = 31536000
# Page titles for the error page, by HTTP status.
ERROR_TITLES = {403: "Not allowed", 404: "Not found", 503: "Discord is unavailable"}


def content_security_policy(allow_cdn: bool) -> str:
    """Build the Content-Security-Policy header value.

    Args:
        allow_cdn: When True (no built ``app.css``), allow the pinned Tailwind CDN
            in ``script-src``. Production builds drop the CDN entirely.

    Returns:
        CSP header string applied by middleware on every response.
    """
    scripts = f"'self' {TAILWIND_CDN}" if allow_cdn else "'self'"
    return (
        "default-src 'self'; "
        f"script-src {scripts}; "
        # 'unsafe-inline': templates set a few colours with style="..." attributes.
        "style-src 'self' 'unsafe-inline'; "
        # Both fonts are served from /static/fonts, so no font host is allowed.
        "font-src 'self'; "
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
    """Build the dashboard app (lifespan, routers, static, security headers).

    Args:
        config: Optional prebuilt settings (tests inject fakes).
        db: Optional database (defaults from ``DATABASE_URL``).
        discord: Optional Discord REST client (tests inject fakes).
        check_schema: When True, refuse to start if Alembic is not at head.

    Returns:
        Configured ``FastAPI`` application.

    Raises:
        RuntimeError: Missing required env vars or schema mismatch (at lifespan start).
    """
    config = config or get_config()
    db = db or Database(config.database_url)
    discord = discord or DiscordClient(
        client_id=config.discord_client_id,
        client_secret=config.discord_client_secret,
        bot_token=config.discord_token,
        redirect_uri=config.oauth_redirect_uri,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Start-up and shutdown of the dashboard process.

        Refuses to start with a required setting missing or the database out of
        date, so the problem is named once, at start, not on some later request.
        """
        if missing := config.missing("discord_token", "discord_client_id", "discord_client_secret", "session_secret"):
            raise RuntimeError(f"Missing in .env: {', '.join(missing)} (see .env.example).")
        if check_schema and (problem := await schema_problem(db)):
            raise RuntimeError(problem)
        log.info("Dashboard ready at %s", config.public_base_url)
        # Raises the alarm (one ERROR log line) when the bot stops reporting in.
        watchdog = asyncio.create_task(BotWatchdog(db).run())
        # Deletes login sessions whose time is up, now and then once an hour.
        session_purge = asyncio.create_task(purge_expired_forever(db))
        yield
        for task in (watchdog, session_purge):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await discord.aclose()
        await db.dispose()

    # docs/openapi off: there's no public JSON API to document, and it would list every route.
    app = FastAPI(title="JoltBot", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.config, app.state.db, app.state.discord = config, db, discord

    @app.middleware("http")
    async def security_headers(request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Add the browser security headers to every response."""
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", content_security_policy(not css_built()))
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if config.uses_https:
            response.headers.setdefault("Strict-Transport-Security", f"max-age={HSTS_MAX_AGE_SECONDS}")
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
    async def _login_required(request: Request, exc: LoginRequired) -> Response:
        """Send an anonymous visitor to the login page."""
        # htmx follows a normal redirect itself and would paste the login page into the
        # current one. HX-Redirect tells it to send the whole browser tab there instead.
        if request.headers.get("hx-request"):
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        """Show an HTTP error (403, 404, 503 ...) as a page in the site's own design."""
        message = exc.detail if exc.status_code != 404 or exc.detail != "Not Found" else "That page doesn't exist."
        return await render(
            request,
            "error.html",
            {"title": ERROR_TITLES.get(exc.status_code, "Something went wrong"), "message": message},
            status_code=exc.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def _bad_url(request: Request, exc: RequestValidationError) -> Response:
        """Treat a malformed URL like a page that doesn't exist."""
        # e.g. /servers/not-a-number
        return await render(
            request, "error.html", {"title": "Not found", "message": "That page doesn't exist."}, status_code=404
        )

    return app
