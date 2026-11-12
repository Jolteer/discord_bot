"""General settings page: embed colour, the notices channel, and switching each module on or off."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from ...core.discord_rest import DiscordError, GuildData
from ...core.guild_settings import StaleSettingsError, load_all, save_module
from ...web.rendering import render
from ...web.routes.guild import guild_context
from ...web.security import CsrfProtected, GuildAccess, RequireGuild, get_db, get_discord
from ..registry import MODULES
from .settings import GeneralSettings

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


def _channel_options(data: GuildData | None) -> list[dict[str, Any]] | None:
    """Text channels in the shape the ``channel_select`` macro needs (IDs as strings)."""
    if data is None:
        return None
    categories = {c.id: c.name for c in data.categories()}
    return [
        {"id": str(c.id), "name": c.name, "category": categories.get(c.parent_id or 0, "")}
        for c in data.text_channels()
    ]


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
):
    modules = [
        {"name": m.name, "title": m.title, "description": m.description, "enabled": toggles.get(m.name, False)}
        for m in MODULES.values()
        if m.toggleable
    ]
    return await render(
        request,
        "general/settings.html",
        {
            **guild_context(access, "general"),
            "embed_color": color,
            "notice_channel_id": notice_channel_id,
            "text_channels": _channel_options(data),  # None = Discord couldn't be reached
            "modules": modules,
            "versions": versions,
            "errors": errors,
            "saved": saved,
        },
        status_code=status_code,
    )


@router.get("")
async def show(request: Request, access: RequireGuild, saved: bool = False):
    """Render the general settings page (embed colour, notices channel, module toggles).

    Args:
        request: FastAPI request.
        access: Authenticated guild manager (``RequireGuild``).
        saved: Query flag to show the success banner after a redirect.

    Returns:
        HTML response for ``general/settings.html``.
    """
    data = await _guild_data(request, access)
    async with get_db(request).session() as session:
        states = await load_all(session, access.guild_id)
    general: GeneralSettings = states["general"].config
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


@router.post("", dependencies=[CsrfProtected])
async def save(request: Request, access: RequireGuild):
    """Persist general settings and module enable flags with optimistic concurrency.

    Args:
        request: FastAPI request (form body + CSRF).
        access: Authenticated guild manager.

    Returns:
        Redirect to GET with ``?saved=1``, or the form with errors (400).

    Raises:
        Does not raise ``StaleSettingsError`` to the client; rendered as a form error.
    """
    form = await request.form()
    color = str(form.get("embed_color", "")).strip()
    notice_channel_id = str(form.get("notice_channel_id", "")).strip()
    toggles = {name: f"enabled_{name}" in form for name, m in MODULES.items() if m.toggleable}
    versions = {}
    for name in MODULES:
        try:
            versions[name] = int(form.get(f"version_{name}", "-1"))
        except ValueError:
            versions[name] = -1
    data = await _guild_data(request, access)  # before the transaction: never hold one across a Discord call

    async def fail(errors: list[str]):
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
            errors = []
            if error := _notice_channel_error(general, states["general"].config, data):
                errors.append(error)
            for name, enabled in toggles.items():
                if enabled and not states[name].enabled and (problems := states[name].config.setup_problems()):
                    errors.append(f"{MODULES[name].title} isn't set up yet: " + " ".join(problems))
            if errors:
                return await fail(errors)
            await save_module(
                session,
                access.guild_id,
                "general",
                enabled=True,
                config=general,
                expected_version=versions["general"],
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
        return await fail(["Someone else saved settings while you were editing. Reload the page and try again."])
    return RedirectResponse(f"/servers/{access.guild_id}/general?saved=1", status_code=303)
