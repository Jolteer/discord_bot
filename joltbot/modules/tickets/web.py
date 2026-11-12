"""Ticket module, dashboard side: GET shows the settings form, POST saves it."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from ...core.discord_rest import DiscordError, GuildData
from ...core.guild_settings import StaleSettingsError, load_module, save_module
from ...core.issues import list_issues
from ...web.rendering import render
from ...web.routes.guild import guild_context
from ...web.security import CsrfProtected, GuildAccess, RequireGuild, get_db, get_discord
from .forms import (
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


async def _guild_data(request: Request, access: GuildAccess) -> GuildData:
    try:
        return await get_discord(request).guild_data(access.guild_id)
    except DiscordError as e:
        raise HTTPException(503, f"Couldn't load this server's roles and channels from Discord. {e}") from None


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
):
    async with get_db(request).session() as session:
        issues = [i.message for i in await list_issues(session, access.guild_id) if i.key.startswith("tickets.")]
    questions, extras = padded_rows(values)
    return await render(
        request,
        "tickets/settings.html",
        {
            **guild_context(access, "tickets"),
            **form_options(data),
            "values": values,
            "enabled": enabled,
            "version": version,
            "questions": questions,
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
async def show(request: Request, access: RequireGuild, saved: bool = False):
    """Render the tickets settings form for this guild.

    Args:
        request: FastAPI request.
        access: Authenticated guild manager (``RequireGuild``).
        saved: Query flag to show the success banner after a redirect.

    Returns:
        HTML response for ``tickets/settings.html``.

    Raises:
        HTTPException: 503 if Discord roles/channels cannot be loaded.
    """
    data = await _guild_data(request, access)
    async with get_db(request).session() as session:
        state = await load_module(session, access.guild_id, "tickets")
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
async def save(request: Request, access: RequireGuild):
    """Validate and save ticket settings; guild id comes only from the URL.

    Rejects foreign role/channel IDs and refuses to enable until
    ``setup_problems()`` is empty.

    Args:
        request: FastAPI request (form body + CSRF).
        access: Authenticated guild manager.

    Returns:
        Redirect to GET with ``?saved=1``, or the form with errors (400).

    Raises:
        HTTPException: 503 if Discord guild data cannot be loaded.
    """
    form = await request.form()
    values = parse_form(form)
    enabled = "enabled" in form
    try:
        version = int(form.get("version", "-1"))
    except ValueError:
        version = -1
    data = await _guild_data(request, access)

    async def fail(errors: list[str]):
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
    if errors := foreign_id_errors(settings, data):
        return await fail(errors)
    async with get_db(request).session() as session:
        previous = (await load_module(session, access.guild_id, "tickets")).config
    if errors := powerful_role_errors(settings, previous, data, actor_is_admin=access.user_guild.is_admin):
        return await fail(errors)
    if enabled and (problems := settings.setup_problems()):
        return await fail(["Can't switch tickets on yet:"] + problems)

    try:
        async with get_db(request).session() as session, session.begin():
            # access.guild_id comes from the checked URL, never from the form.
            await save_module(
                session,
                access.guild_id,
                "tickets",
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
