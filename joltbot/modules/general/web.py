"""General settings page: embed colour, the notices channel, and switching each module on or off."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import ValidationError

from ...core.discord_rest import DiscordError, GuildData
from ...core.guild_settings import ModuleState, StaleSettingsError, load_all, save_module
from ...web.form_helpers import STALE_SETTINGS, form_version
from ...web.options import text_channel_options
from ...web.rendering import render
from ...web.routes.guild import guild_context
from ...web.security import CsrfProtected, GuildAccess, RequireGuild, get_db, get_discord
from ..registry import MODULES
from .settings import MODULE, GeneralSettings

router = APIRouter(prefix="/servers/{guild_id}/general")


async def _guild_data(request: Request, access: GuildAccess) -> GuildData | None:
    """This server's channels from Discord, or ``None`` if Discord can't be reached.

    Unlike the tickets page, this page stays usable without Discord: the colour
    and module switches don't need it.
    """
    try:
        return await get_discord(request).guild_data(access.guild_id)
    except DiscordError:
        return None


async def _render(
    request: Request,
    access: GuildAccess,
    data: GuildData | None,
    *,
    color: str,
    notice_channel_id: str,
    toggles: dict[str, bool],
    versions: dict[str, int],
    errors: list[str],
    saved: bool = False,
    status_code: int = 200,
) -> Response:
    """Render the General page from the given values: the saved ones, or what was typed if a save failed."""
    modules = [
        {"name": m.name, "title": m.title, "description": m.description, "enabled": toggles.get(m.name, False)}
        for m in MODULES.values()
        if m.toggleable
    ]
    return await render(
        request,
        "general/settings.html",
        {
            **guild_context(access, MODULE),
            "embed_color": color,
            "notice_channel_id": notice_channel_id,
            # None = Discord couldn't be reached.
            "text_channels": text_channel_options(data) if data is not None else None,
            "modules": modules,
            "versions": versions,
            "errors": errors,
            "saved": saved,
        },
        status_code=status_code,
    )


@router.get("")
async def show(request: Request, access: RequireGuild, saved: bool = False) -> Response:
    """Render the general settings page (embed colour, notices channel, module toggles).

    Args:
        request: FastAPI request.
        access: Anyone who may use this server's dashboard (``RequireGuild``).
        saved: Query flag to show the success banner after a redirect.

    Returns:
        HTML response for ``general/settings.html``.
    """
    data = await _guild_data(request, access)
    async with get_db(request).session() as session:
        states = await load_all(session, access.guild_id)
    general: GeneralSettings = states[MODULE].config
    return await _render(
        request,
        access,
        data,
        color=general.embed_color,
        notice_channel_id=str(general.notice_channel_id or ""),
        toggles={n: s.enabled for n, s in states.items()},
        versions={n: s.version for n, s in states.items()},
        errors=[],
        saved=saved,
    )


def _validation_errors(exc: ValidationError) -> list[str]:
    """Turn a Pydantic validation error into the sentences shown above the form."""
    fields = {str(err["loc"][0]) for err in exc.errors() if err["loc"]}
    errors = []
    if "embed_color" in fields:
        errors.append("Embed colour must look like #FFB800.")
    if "notice_channel_id" in fields:
        errors.append("Notices channel: pick a channel from the list.")
    return errors or ["Those settings aren't valid."]


def _notice_channel_error(new: GeneralSettings, old: GeneralSettings, data: GuildData | None) -> str | None:
    """Make sure a newly picked notices channel really belongs to this server.

    Without this check an admin could paste a channel ID from another server
    into the form, and JoltBot would try to post this server's problems there.
    A channel that was already saved is not re-checked, so the rest of the page
    can still be saved while Discord is unreachable.
    """
    if new.notice_channel_id is None or new.notice_channel_id == old.notice_channel_id:
        return None
    if data is None:
        return "Notices channel: couldn't check that channel with Discord right now. Try again in a minute."
    if new.notice_channel_id not in {c.id for c in data.text_channels()}:
        return "Notices channel: that channel isn't in this server. Refresh the page and pick again."
    return None


def _save_errors(
    general: GeneralSettings, states: dict[str, ModuleState[Any]], toggles: dict[str, bool], data: GuildData | None
) -> list[str]:
    """Why this save can't go through, checked against what is stored right now.

    Args:
        general: The general settings from the form.
        states: Every module's saved settings for this server.
        toggles: The on/off switch for each module, as submitted.
        data: The server's channels from Discord, or ``None`` if Discord can't be reached.

    Returns:
        Error lines for the form; empty if the save may go ahead. A module can
        only be switched on once its own setup is complete.
    """
    errors = []
    if error := _notice_channel_error(general, states[MODULE].config, data):
        errors.append(error)
    for name, enabled in toggles.items():
        if enabled and not states[name].enabled and (problems := states[name].config.setup_problems()):
            errors.append(f"{MODULES[name].title} isn't set up yet: " + " ".join(problems))
    return errors


@router.post("", dependencies=[CsrfProtected])
async def save(request: Request, access: RequireGuild) -> Response:
    """Persist general settings and module enable flags with optimistic concurrency.

    Args:
        request: FastAPI request (form body + CSRF).
        access: Anyone who may use this server's dashboard (``RequireGuild``).

    Returns:
        Redirect to GET with ``?saved=1``, or the form with errors (400).

    Raises:
        Does not raise ``StaleSettingsError`` to the client; rendered as a form error.
    """
    form = await request.form()
    color = str(form.get("embed_color", "")).strip()
    notice_channel_id = str(form.get("notice_channel_id", "")).strip()
    toggles = {name: f"enabled_{name}" in form for name, m in MODULES.items() if m.toggleable}
    versions = {name: form_version(form, f"version_{name}") for name in MODULES}
    data = await _guild_data(request, access)  # before the transaction: never hold one across a Discord call

    async def fail(errors: list[str]) -> Response:
        """Show the form again with what was typed and why it wasn't saved (HTTP 400)."""
        return await _render(
            request,
            access,
            data,
            color=color,
            notice_channel_id=notice_channel_id,
            toggles=toggles,
            versions=versions,
            errors=errors,
            status_code=400,
        )

    try:
        general = GeneralSettings(embed_color=color, notice_channel_id=notice_channel_id)
    except ValidationError as e:
        return await fail(_validation_errors(e))

    try:
        async with get_db(request).session() as session, session.begin():
            states = await load_all(session, access.guild_id)
            if errors := _save_errors(general, states, toggles, data):
                return await fail(errors)
            # This form has no field for who may use the dashboard. Keep what is stored,
            # so saving General can never change it (people let in by a role can save here).
            general = general.model_copy(update={"dashboard_role_ids": states[MODULE].config.dashboard_role_ids})
            await save_module(
                session,
                access.guild_id,
                MODULE,
                enabled=True,
                config=general,
                expected_version=versions[MODULE],
                actor=access.actor,
            )
            for name, enabled in toggles.items():
                await save_module(
                    session,
                    access.guild_id,
                    name,
                    enabled=enabled,
                    config=states[name].config,
                    expected_version=versions[name],
                    actor=access.actor,
                )
    except StaleSettingsError:
        return await fail([STALE_SETTINGS])
    return RedirectResponse(f"/servers/{access.guild_id}/general?saved=1", status_code=303)
