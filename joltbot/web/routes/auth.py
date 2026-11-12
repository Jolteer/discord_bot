"""Login with Discord (OAuth2) and logout.

Flow: /login stores a random `state` in a short-lived cookie and sends the
user to Discord. Discord sends them back to /auth/callback with ?code and the
same ?state. We only continue if the state matches the cookie: that proves
this login was started by this browser, not by an attacker's link.
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from ...core.discord_rest import DiscordError
from ..rendering import render
from ..security import (
    STATE_COOKIE,
    CsrfProtected,
    RequireSession,
    get_config,
    get_db,
    get_discord,
    session_cookie_name,
)
from ..sessions import SESSION_LIFETIME, create_session, delete_session

router = APIRouter()
STATE_MAX_AGE = 600  # seconds the user has to finish logging in on Discord


@router.get("/login")
async def login(request: Request):
    """Start Discord OAuth: set ``state`` cookie and redirect to Discord.

    Args:
        request: Incoming HTTP request.

    Returns:
        303 redirect to Discord's authorize URL.
    """
    config = get_config(request)
    state = secrets.token_urlsafe(32)
    response = RedirectResponse(get_discord(request).authorize_url(state), status_code=303)
    response.set_cookie(
        STATE_COOKIE,
        state,
        max_age=STATE_MAX_AGE,
        path="/auth",
        httponly=True,
        secure=config.uses_https,
        samesite="lax",
    )
    return response


async def _login_failed(request: Request, message: str):
    response = await render(
        request, "error.html", {"title": "Login failed", "message": message, "retry_url": "/login"}, status_code=400
    )
    response.delete_cookie(STATE_COOKIE, path="/auth")
    return response


@router.get("/auth/callback")
async def callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    """Finish Discord OAuth: validate ``state``, exchange ``code``, create session.

    Args:
        request: Incoming HTTP request (includes state cookie).
        code: Authorization code from Discord, or None on failure.
        state: Must match the short-lived state cookie.
        error: Discord error query param when the user cancels / consent fails.

    Returns:
        Redirect to ``/servers`` on success, or an error page (400).
    """
    config = get_config(request)
    expected = request.cookies.get(STATE_COOKIE)
    if error:
        return await _login_failed(request, "Login was cancelled on Discord.")
    # compare_digest: constant-time comparison, so timing can't leak the expected value.
    if not code or not state or not expected or not hmac.compare_digest(state, expected):
        return await _login_failed(request, "This login link expired or wasn't started here. Please try again.")

    discord = get_discord(request)
    try:
        token = await discord.exchange_code(code)
        granted = set(token.get("scope", "").split())
        if not {"identify", "guilds"} <= granted:
            return await _login_failed(request, "JoltBot needs to see your username and server list to work.")
        user = await discord.get_user(token["access_token"])
        guilds = await discord.get_user_guilds(token["access_token"])
    except DiscordError:
        return await _login_failed(request, "Discord didn't accept the login. Please try again.")

    raw = await create_session(
        get_db(request),
        config.session_secret,
        user=user,
        access_token=token["access_token"],
        expires_in=int(token.get("expires_in", 604800)),
        guilds=guilds,
    )
    response = RedirectResponse("/servers", status_code=303)
    response.delete_cookie(STATE_COOKIE, path="/auth")  # single use
    response.set_cookie(
        session_cookie_name(config),
        raw,
        max_age=int(SESSION_LIFETIME.total_seconds()),
        path="/",
        httponly=True,
        secure=config.uses_https,
        samesite="lax",
    )
    return response


@router.post("/logout", dependencies=[CsrfProtected])
async def logout(request: Request, ws: RequireSession):
    """End the dashboard session and clear the session cookie.

    Args:
        request: Incoming HTTP request.
        ws: Current logged-in session (deleted from the DB).

    Returns:
        Redirect to the landing page.
    """
    await delete_session(get_db(request), ws.id)
    config = get_config(request)
    response = RedirectResponse("/", status_code=303)
    # Same attributes as when the cookie was set. Browsers ignore any Set-Cookie for a
    # __Host- name that lacks Secure, including the one that is meant to remove it.
    response.delete_cookie(
        session_cookie_name(config), path="/", secure=config.uses_https, httponly=True, samesite="lax"
    )
    return response
