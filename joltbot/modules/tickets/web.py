"""Ticket module, dashboard side: GET shows the settings form, POST saves it."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import ValidationError

from ...core.discord_rest import GuildData
from ...core.guild_settings import StaleSettingsError, load_module, save_module
from ...core.issues import list_issues
from ...web.form_helpers import form_version, require_guild_data
from ...web.rendering import render
from ...web.routes.guild import guild_context
from ...web.security import CsrfProtected, GuildAccess, RequireGuild, get_db
from ..general.settings import MODULE as GENERAL
from .constants import ISSUE_PREFIX, MODULE
from .forms import (
    choice_rows,
    emoji_errors,
    foreign_id_errors,
    form_options,
    message_fields,
    padded_rows,
    parse_form,
    powerful_role_errors,
    setup_warnings,
    validation_errors,
)
from .settings import TicketSettings

router = APIRouter(prefix="/servers/{guild_id}/tickets")


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
    """Render the Tickets page from the given values: the saved ones, or what was typed if a save failed.

    The server's open ticket problems are shown above the form.
    """
    async with get_db(request).session() as session:
        issues = [i.message for i in await list_issues(session, access.guild_id) if i.key.startswith(ISSUE_PREFIX)]
    questions, extras = padded_rows(values)
    return await render(
        request,
        "tickets/settings.html",
        {
            **guild_context(access, MODULE),
            **form_options(data),
            "values": values,
            "enabled": enabled,
            "version": version,
            "questions": questions,
            "choices": choice_rows(values, data),
            "extras": extras,
            "messages": message_fields(values.get("messages") or {}),
            "errors": errors,
            "warnings": warnings,
            "issues": issues,
            "saved": saved,
        },
        status_code=status_code,
    )


@router.get("")
async def show(request: Request, access: RequireGuild, saved: bool = False) -> Response:
    """Render the tickets settings form for this guild.

    Args:
        request: FastAPI request.
        access: Anyone who may use this server's dashboard (``RequireGuild``).
        saved: Query flag to show the success banner after a redirect.

    Returns:
        HTML response for ``tickets/settings.html``.

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
    """Validate and save ticket settings; guild id comes only from the URL.

    Rejects foreign role/channel IDs and refuses to enable until
    ``setup_problems()`` is empty.

    Args:
        request: FastAPI request (form body + CSRF).
        access: Anyone who may use this server's dashboard (``RequireGuild``).

    Returns:
        Redirect to GET with ``?saved=1``, or the form with errors (400).

    Raises:
        HTTPException: 503 if Discord guild data cannot be loaded.
    """
    form = await request.form()
    values = parse_form(form)
    enabled = "enabled" in form
    version = form_version(form)
    data = await require_guild_data(request, access.guild_id, "roles and channels")

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
        settings = TicketSettings.model_validate(values)
    except ValidationError as e:
        return await fail(validation_errors(e))
    if errors := foreign_id_errors(settings, data) + emoji_errors(settings, data):
        return await fail(errors)
    async with get_db(request).session() as session:
        previous = (await load_module(session, access.guild_id, MODULE)).config
        dashboard_role_ids = (await load_module(session, access.guild_id, GENERAL)).config.dashboard_role_ids
    if errors := powerful_role_errors(
        settings,
        previous,
        data,
        actor_is_admin=access.user_guild.is_admin,
        dashboard_role_ids=dashboard_role_ids,
    ):
        return await fail(errors)
    if enabled and (problems := settings.setup_problems()):
        return await fail(["Can't switch tickets on yet:"] + problems)

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
        return await fail(
            [
                "Someone else saved these settings while you were editing. Reload the page to see "
                "their changes, then make yours again."
            ]
        )
    return RedirectResponse(f"/servers/{access.guild_id}/tickets?saved=1", status_code=303)
