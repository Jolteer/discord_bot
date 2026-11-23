"""Mod log module, dashboard side: GET shows the settings form, POST saves it."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import ValidationError

from ...core.discord_rest import GuildData
from ...core.guild_settings import StaleSettingsError, load_module, save_module
from ...core.issues import list_issues
from ...web.form_helpers import STALE_SETTINGS, form_version, require_guild_data
from ...web.rendering import render
from ...web.routes.guild import guild_context
from ...web.security import CsrfProtected, GuildAccess, RequireGuild, get_db
from .constants import ISSUE_PREFIX, MODULE
from .forms import event_groups, foreign_id_errors, form_options, parse_form, setup_warnings, validation_errors
from .settings import MAX_IGNORED, ModLogSettings

router = APIRouter(prefix=f"/servers/{{guild_id}}/{MODULE}")


async def _render_form(
    request: Request,
    access: GuildAccess,
    data: GuildData,
    *,
    values: dict[str, Any],
    enabled: bool,
    version: int,
    errors: list[str],
    warnings: list[str],
    saved: bool = False,
    status_code: int = 200,
) -> Response:
    """Render the Logging page from the given values: the saved ones, or what was typed if a save failed.

    The server's open mod log problems are shown above the form.
    """
    async with get_db(request).session() as session:
        issues = [i.message for i in await list_issues(session, access.guild_id) if i.key.startswith(ISSUE_PREFIX)]
    return await render(
        request,
        "modlog/settings.html",
        {
            **guild_context(access, MODULE),
            **form_options(data),
            "values": values,
            "enabled": enabled,
            "version": version,
            "groups": event_groups(values),
            "max_ignored": MAX_IGNORED,
            "errors": errors,
            "warnings": warnings,
            "issues": issues,
            "saved": saved,
        },
        status_code=status_code,
    )


@router.get("")
async def show(request: Request, access: RequireGuild, saved: bool = False) -> Response:
    """Render the mod log settings form for this guild.

    Args:
        request: FastAPI request.
        access: Anyone who may use this server's dashboard (``RequireGuild``).
        saved: Query flag to show the success banner after a redirect.

    Returns:
        HTML response for ``modlog/settings.html``.

    Raises:
        HTTPException: 503 if Discord roles/channels cannot be loaded.
    """
    data = await require_guild_data(request, access.guild_id, "roles and channels")
    async with get_db(request).session() as session:
        state = await load_module(session, access.guild_id, MODULE)
    return await _render_form(
        request,
        access,
        data,
        values=state.config.model_dump(mode="json"),
        enabled=state.enabled,
        version=state.version,
        errors=[],
        warnings=setup_warnings(state.config, data),
        saved=saved,
    )


@router.post("", dependencies=[CsrfProtected])
async def save(request: Request, access: RequireGuild) -> Response:
    """Validate and save mod log settings; guild id comes only from the URL.

    Rejects channel and role IDs from other servers, and refuses to switch
    logging on until ``setup_problems()`` is empty.

    Args:
        request: FastAPI request (form body + CSRF).
        access: Anyone who may use this server's dashboard (``RequireGuild``).

    Returns:
        Redirect to GET with ``?saved=1``, or the form with errors (400).

    Raises:
        HTTPException: 503 if Discord guild data cannot be loaded.
    """
    form = await request.form()
    enabled = "enabled" in form
    version = form_version(form)
    data = await require_guild_data(request, access.guild_id, "roles and channels")
    async with get_db(request).session() as session:
        previous = (await load_module(session, access.guild_id, MODULE)).config
    # Start from what is saved, so a setting this page doesn't show keeps its value.
    values = {**previous.model_dump(mode="json"), **parse_form(form)}

    async def fail(errors: list[str]) -> Response:
        """Show the form again with what was typed and why it wasn't saved (HTTP 400)."""
        return await _render_form(
            request,
            access,
            data,
            values=values,
            enabled=enabled,
            version=version,
            errors=errors,
            warnings=[],
            status_code=400,
        )

    try:
        settings = ModLogSettings.model_validate(values)
    except ValidationError as e:
        return await fail(validation_errors(e))
    if errors := foreign_id_errors(settings, data):
        return await fail(errors)
    if enabled and (problems := settings.setup_problems()):
        return await fail(["Can't switch logging on yet:"] + problems)

    try:
        async with get_db(request).session() as session, session.begin():
            # access.guild_id comes from the checked URL, never from the form.
            await save_module(
                session,
                access.guild_id,
                MODULE,
                enabled=enabled,
                config=settings,
                expected_version=version,
                actor=access.actor,
            )
    except StaleSettingsError:
        return await fail([STALE_SETTINGS])
    return RedirectResponse(f"/servers/{access.guild_id}/{MODULE}?saved=1", status_code=303)
