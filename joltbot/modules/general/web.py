"""General settings page: embed colour and switching each module on or off."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from ...core.guild_settings import StaleSettingsError, load_all, save_module
from ...web.rendering import render
from ...web.routes.guild import guild_context
from ...web.security import CsrfProtected, GuildAccess, RequireGuild, get_db
from ..registry import MODULES
from .settings import GeneralSettings

router = APIRouter(prefix="/servers/{guild_id}/general")


async def _render(
    request: Request,
    access: GuildAccess,
    *,
    color: str,
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
            "modules": modules,
            "versions": versions,
            "errors": errors,
            "saved": saved,
        },
        status_code=status_code,
    )


@router.get("")
async def show(request: Request, access: RequireGuild, saved: bool = False):
    async with get_db(request).session() as session:
        states = await load_all(session, access.guild_id)
    return await _render(
        request,
        access,
        color=states["general"].config.embed_color,
        toggles={n: s.enabled for n, s in states.items()},
        versions={n: s.version for n, s in states.items()},
        errors=[],
        saved=saved,
    )


@router.post("", dependencies=[CsrfProtected])
async def save(request: Request, access: RequireGuild):
    form = await request.form()
    color = str(form.get("embed_color", "")).strip()
    toggles = {name: f"enabled_{name}" in form for name, m in MODULES.items() if m.toggleable}
    versions = {}
    for name in MODULES:
        try:
            versions[name] = int(form.get(f"version_{name}", "-1"))
        except ValueError:
            versions[name] = -1

    async def fail(errors: list[str]):
        return await _render(
            request, access, color=color, toggles=toggles, versions=versions, errors=errors, status_code=400
        )

    try:
        general = GeneralSettings(embed_color=color)
    except ValidationError:
        return await fail(["Embed colour must look like #FFB800."])

    try:
        async with get_db(request).session() as session, session.begin():
            states = await load_all(session, access.guild_id)
            errors = []
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
